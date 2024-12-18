from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, Response
import httpx
import json
from typing import Dict, Any, AsyncGenerator, List, Optional
from openai import AsyncOpenAI
import struct
import zlib
import boto3
import re
import os
import uuid
from sqlalchemy import create_engine, MetaData, Table, Column, String, Text, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import select, insert, update

app = FastAPI()

LITELLM_ENDPOINT = "http://localhost:4000"
LITELLM_CHAT = f"{LITELLM_ENDPOINT}/v1/chat/completions"

bedrock_client = boto3.client("bedrock-agent")

db_engine = None
metadata = MetaData()
chat_sessions = None


def setup_database():
    try:
        database_url = os.environ.get("DATABASE_MIDDLEWARE_URL")
        if not database_url:
            raise ValueError("DATABASE_MIDDLEWARE_URL environment variable not set")

        engine = create_engine(database_url)
        metadata_obj = MetaData()

        inspector = inspect(engine)
        if "chat_sessions" not in inspector.get_table_names():
            chat_sessions_table = Table(
                "chat_sessions",
                metadata_obj,
                Column("session_id", String, primary_key=True),
                Column("chat_history", Text),
            )
            metadata_obj.create_all(engine)
            print("Created chat_sessions table")
        else:
            chat_sessions_table = Table(
                "chat_sessions", metadata_obj, autoload_with=engine
            )
            print("chat_sessions table already exists")

        return engine, chat_sessions_table
    except SQLAlchemyError as e:
        print(f"Database setup error: {str(e)}")
        raise


@app.on_event("startup")
async def startup_event():
    global db_engine, chat_sessions
    db_engine, chat_sessions = setup_database()


def get_chat_history(session_id: str) -> Optional[List[Dict[str, str]]]:
    with db_engine.connect() as conn:
        stmt = select(chat_sessions.c.chat_history).where(
            chat_sessions.c.session_id == session_id
        )
        result = conn.execute(stmt).fetchone()
        if result and result[0]:
            return json.loads(result[0])
    return None


def create_chat_history(session_id: str, chat_history: List[Dict[str, str]]):
    with db_engine.connect() as conn:
        stmt = insert(chat_sessions).values(
            session_id=session_id, chat_history=json.dumps(chat_history)
        )
        conn.execute(stmt)
        conn.commit()


def update_chat_history(session_id: str, chat_history: List[Dict[str, str]]):
    with db_engine.connect() as conn:
        stmt = (
            update(chat_sessions)
            .where(chat_sessions.c.session_id == session_id)
            .values(chat_history=json.dumps(chat_history))
        )
        conn.execute(stmt)
        conn.commit()


class CustomEventStream:
    def __init__(self, messages):
        self.messages = messages
        self.position = 0

    def stream(self):
        while self.position < len(self.messages):
            yield self.messages[self.position]
            self.position += 1


def create_event_message(payload, event_type_name):
    header_name = b":event-type"
    header_name_length = len(header_name)
    event_name_bytes = event_type_name.encode("utf-8")
    event_name_length = len(event_name_bytes)

    headers_bytes = (
        struct.pack("B", header_name_length)
        + header_name
        + b"\x07"
        + struct.pack(">H", event_name_length)
        + event_name_bytes
    )

    headers_length = len(headers_bytes)
    payload_length = len(payload)
    total_length = payload_length + headers_length + 16

    prelude = struct.pack(">I", total_length) + struct.pack(">I", headers_length)
    prelude_crc = struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)

    message_parts = prelude + prelude_crc + headers_bytes + payload
    message_crc = struct.pack(">I", zlib.crc32(message_parts) & 0xFFFFFFFF)

    return message_parts + message_crc


def convert_messages_to_openai(
    bedrock_messages: List[Dict[str, Any]],
    system: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    openai_messages = []

    if system:
        system_text = " ".join(item.get("text", "") for item in system)
        if system_text:
            openai_messages.append({"role": "system", "content": system_text})

    for msg in bedrock_messages:
        role = msg.get("role")
        content = ""
        if "content" in msg:
            for content_item in msg["content"]:
                if "text" in content_item:
                    content += content_item["text"]

        openai_messages.append({"role": role, "content": content})

    return openai_messages


async def convert_bedrock_to_openai(
    model_id: str, bedrock_request: Dict[str, Any], streaming: bool
) -> Dict[str, Any]:
    prompt_variables = bedrock_request.get("promptVariables", {})
    final_prompt_text = None
    if model_id.startswith("arn:aws:bedrock:"):
        prompt_id, prompt_version = parse_prompt_arn(model_id)
        if prompt_id:
            if prompt_version:
                prompt = bedrock_client.get_prompt(
                    promptIdentifier=prompt_id, promptVersion=prompt_version
                )
            else:
                prompt = bedrock_client.get_prompt(promptIdentifier=prompt_id)

            variants = prompt.get("variants", [])
            variant = variants[0]
            template_text = variant["templateConfiguration"]["text"]["text"]

            validate_prompt_variables(template_text, prompt_variables)
            final_prompt_text = construct_prompt_text_from_variables(
                template_text, prompt_variables
            )
            model_id = variant["modelId"]

    completion_params = {"model": model_id}

    if final_prompt_text:
        final_prompt_messages = [
            {"role": "user", "content": [{"text": final_prompt_text}]}
        ]
        messages = convert_messages_to_openai(final_prompt_messages, [])
    else:
        messages = convert_messages_to_openai(
            bedrock_request.get("messages", []), bedrock_request.get("system", [])
        )

    completion_params["messages"] = messages
    if streaming:
        completion_params["stream"] = True

    if "inferenceConfig" in bedrock_request:
        config = bedrock_request["inferenceConfig"]
        if "temperature" in config:
            completion_params["temperature"] = config["temperature"]
        if "maxTokens" in config:
            completion_params["max_tokens"] = config["maxTokens"]
        if "stopSequences" in config:
            completion_params["stop"] = config["stopSequences"]
        if "topP" in config:
            completion_params["top_p"] = config["topP"]

    if "additionalModelRequestFields" in bedrock_request:
        # Exclude "session_id" from being added to completion_params
        additional_fields = {
            key: value
            for key, value in bedrock_request["additionalModelRequestFields"].items()
            if key != "session_id"
        }
        completion_params.update(additional_fields)

    return completion_params


async def convert_openai_to_bedrock(openai_response: Dict[str, Any]) -> Dict[str, Any]:
    bedrock_response = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"text": openai_response["choices"][0]["message"]["content"]}
                ],
            }
        },
        "usage": {
            "inputTokens": openai_response["usage"]["prompt_tokens"],
            "outputTokens": openai_response["usage"]["completion_tokens"],
            "totalTokens": openai_response["usage"]["total_tokens"],
        },
    }

    if "finish_reason" in openai_response["choices"][0]:
        stop_reason_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "content_filtered",
        }
        finish_reason = openai_response["choices"][0]["finish_reason"]
        bedrock_response["stopReason"] = stop_reason_map.get(finish_reason, "end_turn")

    return bedrock_response


async def openai_stream_to_bedrock_chunks(openai_stream):
    async for chunk in openai_stream:
        delta = chunk.choices[0].delta
        finish_reason = chunk.choices[0].finish_reason

        if delta.role:
            event_payload = json.dumps({"role": delta.role}).encode("utf-8")
            yield create_event_message(event_payload, "messageStart")

        if delta.content:
            event_payload = json.dumps(
                {
                    "contentBlockIndex": 0,
                    "delta": {"text": delta.content},
                }
            ).encode("utf-8")
            yield create_event_message(event_payload, "contentBlockDelta")

        if finish_reason == "stop":
            event_payload = json.dumps({"stopReason": "end_turn"}).encode("utf-8")
            yield create_event_message(event_payload, "messageStop")


def parse_prompt_arn(arn: str):
    if "prompt/" not in arn:
        return None, None

    after_prompt = arn.split("prompt/", 1)[1]

    if ":" in after_prompt:
        prompt_id, prompt_version = after_prompt.split(":", 1)
        return prompt_id, prompt_version
    else:
        return after_prompt, None


def validate_prompt_variables(template_text: str, variables: Dict[str, Any]):
    found_placeholders = re.findall(r"{{\s*(\w+)\s*}}", template_text)
    placeholders_set = set(found_placeholders)
    variables_set = set(variables.keys())

    if placeholders_set != variables_set:
        detail_message = {
            "message": f"Prompt variable mismatch. Template placeholders: {placeholders_set}. Provided variables: {variables_set}."
        }
        raise HTTPException(status_code=400, detail=detail_message)


def construct_prompt_text_from_variables(template_text: str, variables: dict) -> str:
    for var_name, var_value in variables.items():
        value = var_value.get("text", "")
        template_text = template_text.replace(f"{{{{{var_name}}}}}", value)
    return template_text


@app.get("/bedrock/health/liveliness")
async def health_check():
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{LITELLM_ENDPOINT}/health/liveliness", timeout=5.0
            )
            if response.status_code == 200:
                return JSONResponse(
                    content={"status": "healthy", "litellm": "connected"}
                )
            else:
                return JSONResponse(
                    status_code=503, content={"status": "unhealthy", "litellm": "error"}
                )
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "litellm": "disconnected", "error": str(e)},
        )


async def process_chat_request(
    model_id: str, request: Request
) -> (Dict[str, Any], str):
    body = await request.json()
    session_id = body.get("additionalModelRequestFields", {}).get("session_id", None)
    print(f"session_id: {session_id}")
    if session_id is not None:
        chat_history = get_chat_history(session_id)
        print(f"chat_history: {chat_history}")
        if chat_history is None:
            chat_history = []
            create_chat_history(session_id, chat_history)
    else:
        session_id = str(uuid.uuid4())
        chat_history = []
        create_chat_history(session_id, chat_history)

    openai_format = await convert_bedrock_to_openai(model_id, body, False)
    print(f"openai_format: {openai_format}")

    # Append the last user message to chat_history
    user_messages_this_round = [
        m for m in openai_format["messages"] if m["role"] == "user"
    ]
    if user_messages_this_round:
        chat_history.append(user_messages_this_round[-1])

    print(f"chat_history: {chat_history}")

    # If we have a session (existing or new), we want to pass the full history to the LLM
    # Replace openai_format["messages"] with the full chat_history (which now includes the latest user message)
    openai_format["messages"] = chat_history

    print(f"openai_format: {openai_format}")

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        api_key = auth_header[len("Bearer ") :]
    else:
        raise HTTPException(
            status_code=401, detail={"error": "Missing or invalid Authorization header"}
        )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            LITELLM_CHAT,
            json=openai_format,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        print(f"response: {response}")

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail={"error": f"Error from LiteLLM endpoint: {response.text}"},
            )

        openai_response = response.json()
        print(f"openai_response: {openai_response}")
        bedrock_response = await convert_openai_to_bedrock(openai_response)
        print(f"bedrock_response: {bedrock_response}")

    # Append assistant's response to history
    assistant_message = openai_response["choices"][0]["message"]
    print(f"assistant_message: {assistant_message}")
    chat_history.append({"role": "assistant", "content": assistant_message["content"]})
    update_chat_history(session_id, chat_history)
    print(f"chat_history: {chat_history}")

    bedrock_response["session_id"] = session_id
    return bedrock_response, session_id


async def process_streaming_chat_request(
    model_id: str, request: Request
) -> (AsyncGenerator, str, List[Dict[str, str]], List[str]):
    body = await request.json()

    session_id = body.get("additionalModelRequestFields", {}).get("session_id", None)
    print(f"session_id: {session_id}")
    if session_id is not None:
        chat_history = get_chat_history(session_id)
        if chat_history is None:
            chat_history = []
            create_chat_history(session_id, chat_history)
    else:
        session_id = str(uuid.uuid4())
        chat_history = []
        create_chat_history(session_id, chat_history)

    print(f"chat_history: {chat_history}")

    openai_params = await convert_bedrock_to_openai(model_id, body, True)
    print(f"openai_params: {openai_params}")

    # Append the user message to chat_history
    user_messages_this_round = [
        m for m in openai_params["messages"] if m["role"] == "user"
    ]
    if user_messages_this_round:
        chat_history.append(user_messages_this_round[-1])
    print(f"chat_history: {chat_history}")

    # Pass the entire chat_history to the LLM
    openai_params["messages"] = chat_history
    print(f"openai_params: {openai_params}")

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        api_key = auth_header[len("Bearer ") :]
    else:
        raise HTTPException(
            status_code=401, detail={"error": "Missing or invalid Authorization header"}
        )

    client = AsyncOpenAI(api_key=api_key, base_url=LITELLM_ENDPOINT)
    stream = await client.chat.completions.create(**openai_params)

    assistant_content_parts = []

    async def stream_wrapper():
        message_started = False
        content_block_index = 0
        async for chunk in stream:
            delta = chunk.choices[0].delta
            finish_reason = chunk.choices[0].finish_reason

            if delta.role and not message_started:
                event_payload = json.dumps({"role": delta.role}).encode("utf-8")
                yield create_event_message(event_payload, "messageStart")
                message_started = True

            if delta.content:
                assistant_content_parts.append(delta.content)
                event_payload = json.dumps(
                    {
                        "contentBlockIndex": content_block_index,
                        "delta": {"text": delta.content},
                    }
                ).encode("utf-8")
                yield create_event_message(event_payload, "contentBlockDelta")

            if finish_reason == "stop":
                event_payload = json.dumps({"stopReason": "end_turn"}).encode("utf-8")
                yield create_event_message(event_payload, "messageStop")

    return stream_wrapper(), session_id, chat_history, assistant_content_parts


async def finalize_streaming_chat_history(
    session_id: str,
    chat_history: List[Dict[str, str]],
    assistant_content_parts: List[str],
):
    assistant_message = {
        "role": "assistant",
        "content": "".join(assistant_content_parts),
    }
    chat_history.append(assistant_message)
    update_chat_history(session_id, chat_history)


@app.post("/bedrock/model/{prompt_arn_prefix}/{prompt_id}/converse-stream")
async def handle_bedrock_streaming_request_prompts(
    prompt_arn_prefix: str, prompt_id: str, request: Request
):
    full_arn = prompt_arn_prefix + "/" + prompt_id
    return await handle_bedrock_streaming_request(full_arn, request)


@app.post("/bedrock/model/{model_id}/converse-stream")
async def handle_bedrock_streaming_request(model_id: str, request: Request):
    try:
        stream_wrapper, session_id, chat_history, assistant_content_parts = (
            await process_streaming_chat_request(model_id, request)
        )

        async def finalizing_stream():
            async for event in stream_wrapper:
                yield event
            await finalize_streaming_chat_history(
                session_id, chat_history, assistant_content_parts
            )

        response = StreamingResponse(
            finalizing_stream(), media_type="application/vnd.amazon.eventstream"
        )
        response.headers["X-Session-Id"] = session_id
        return response
    except HTTPException as he:
        return JSONResponse(
            status_code=400,
            content=he.detail,
        )
    except Exception as e:
        return JSONResponse(status_code=500, content=f"Internal server error: {str(e)}")


@app.post("/bedrock/model/{prompt_arn_prefix}/{prompt_id}/converse")
async def handle_bedrock_request_prompts(
    prompt_arn_prefix: str, prompt_id: str, request: Request
):
    full_arn = prompt_arn_prefix + "/" + prompt_id
    return await handle_bedrock_request(full_arn, request)


@app.post("/bedrock/model/{model_id}/converse")
async def handle_bedrock_request(model_id: str, request: Request):
    try:
        bedrock_response, session_id = await process_chat_request(model_id, request)
        return JSONResponse(
            content=bedrock_response, headers={"X-Session-Id": session_id}
        )
    except HTTPException as he:
        print(f"exception: {he}")
        return JSONResponse(
            status_code=400,
            content=he.detail,
        )
    except Exception as e:
        print(f"exception: {e}")
        return JSONResponse(status_code=500, content=f"Internal server error: {str(e)}")


async def forward_openai_stream(stream) -> AsyncGenerator[bytes, None]:
    """
    Forward the streaming response from OpenAI client.
    """
    try:
        async for chunk in stream:
            # Convert the chunk to the same format as the API response
            yield f"data: {json.dumps(chunk.model_dump())}\n\n".encode("utf-8")
    except Exception as e:
        print(f"Streaming error: {e}")
        raise


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def proxy_request(request: Request):
    # Read raw request data
    body = await request.body()

    try:
        # Parse request data
        data = json.loads(body)
        is_streaming = data.get("stream", False)

        # Extract session_id if provided
        session_id = data.pop("session_id", None)

        if session_id is not None:
            chat_history = get_chat_history(session_id)
            if chat_history is None:
                chat_history = []
                create_chat_history(session_id, chat_history)
        else:
            # No session_id provided, create one
            session_id = str(uuid.uuid4())
            chat_history = []
            create_chat_history(session_id, chat_history)

        # Merge incoming user messages into chat history
        # data["messages"] are in OpenAI format: [{"role": "user"|"assistant"|"system", "content": "..."}]
        user_messages_this_round = [
            m for m in data.get("messages", []) if m["role"] == "user"
        ]
        if user_messages_this_round:
            # Append only the last user message to the chat history
            chat_history.append(user_messages_this_round[-1])

        # Replace data["messages"] with the full chat_history
        data["messages"] = chat_history

        # Get API key from headers
        api_key = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not api_key:
            raise HTTPException(
                status_code=401,
                detail={"error": "Missing or invalid Authorization header"},
            )

        # Check if model is a prompt ARN and handle prompt logic as before
        model_id = data.get("model")
        prompt_variables = data.pop("promptVariables", {})
        final_prompt_text = None
        if model_id and model_id.startswith("arn:aws:bedrock:"):
            prompt_id, prompt_version = parse_prompt_arn(model_id)
            if prompt_id:
                # Fetch the prompt
                if prompt_version:
                    prompt = bedrock_client.get_prompt(
                        promptIdentifier=prompt_id, promptVersion=prompt_version
                    )
                else:
                    prompt = bedrock_client.get_prompt(promptIdentifier=prompt_id)

                variants = prompt.get("variants", [])
                variant = variants[0]
                template_text = variant["templateConfiguration"]["text"]["text"]

                # Validate and construct the final prompt text
                validate_prompt_variables(template_text, prompt_variables)
                final_prompt_text = construct_prompt_text_from_variables(
                    template_text, prompt_variables
                )

                # If we have a model inside the variant, use that
                if "modelId" in variant:
                    data["model"] = variant["modelId"]

        # If we got a final_prompt_text, replace data["messages"] entirely with a single user message
        if final_prompt_text:
            data["messages"] = [{"role": "user", "content": final_prompt_text}]

        # Initialize OpenAI client
        client = AsyncOpenAI(api_key=api_key, base_url=LITELLM_ENDPOINT)

        if is_streaming:
            # Handle streaming request
            stream = await client.chat.completions.create(**data)

            assistant_content_parts = []

            async def stream_wrapper():
                first_chunk = True
                async for chunk in stream:
                    chunk_dict = chunk.model_dump()
                    print(f"chunk_dict before: {chunk_dict}")
                    if first_chunk:
                        chunk_dict["session_id"] = session_id
                        first_chunk = False
                    # Write out the chunk immediately
                    yield f"data: {json.dumps(chunk_dict)}\n\n".encode("utf-8")

                    # Extract assistant delta content if available
                    choice = chunk_dict["choices"][0]
                    delta = choice.get("delta", {})
                    finish_reason = choice.get("finish_reason")
                    print(f"chunk_dict after: {chunk_dict}")
                    if "content" in delta and delta["content"]:
                        assistant_content_parts.append(delta["content"])

                    if finish_reason == "stop":
                        # Once we hit stop, we can finalize the assistant message
                        break

            async def finalizing_stream():
                # Stream the response back to the client
                async for event in stream_wrapper():
                    yield event

                # After streaming is done, append the assistant message to chat history
                if assistant_content_parts:
                    print(f"assistant_content_parts: {assistant_content_parts}")
                    assistant_message = {
                        "role": "assistant",
                        "content": "".join(assistant_content_parts),
                    }
                    chat_history.append(assistant_message)
                    update_chat_history(session_id, chat_history)

            response = StreamingResponse(
                finalizing_stream(), media_type="text/event-stream"
            )
            return response

        else:
            # Handle non-streaming request
            response = await client.chat.completions.create(**data)
            response_dict = response.model_dump()

            # Append assistant's response to chat history
            if response_dict.get("choices"):
                assistant_message = response_dict["choices"][0]["message"]
                chat_history.append(
                    {"role": "assistant", "content": assistant_message["content"]}
                )
                update_chat_history(session_id, chat_history)

            response_dict["session_id"] = session_id
            return Response(
                content=json.dumps(response_dict),
                media_type="application/json",
            )

    except json.JSONDecodeError:
        return Response(
            content=json.dumps({"error": "Invalid JSON"}),
            status_code=400,
            media_type="application/json",
        )
    except HTTPException as he:
        return JSONResponse(status_code=he.status_code, content=he.detail)
    except Exception as e:
        print(f"Exception in proxy_request: {e}")
        return Response(
            content=json.dumps({"error": str(e)}),
            status_code=500,
            media_type="application/json",
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=3000)
