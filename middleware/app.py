from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
import json
from typing import Dict, Any, AsyncGenerator, List, Optional
from openai import AsyncOpenAI
import struct
import google_crc32c
import zlib

app = FastAPI()

# Since we're in the same container, we can use localhost
LITELLM_ENDPOINT = "http://localhost:4000"
LITELLM_CHAT = f"{LITELLM_ENDPOINT}/v1/chat/completions"


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

    # Build headers block
    headers_bytes = (
        struct.pack("B", header_name_length)
        + header_name
        + b"\x07"  # string type
        + struct.pack(">H", event_name_length)
        + event_name_bytes
    )

    headers_length = len(headers_bytes)
    payload_length = len(payload)
    total_length = (
        payload_length + headers_length + 16
    )  # 16 bytes = prelude(8) + message_crc(4) + prelude_crc(4)

    prelude = struct.pack(">I", total_length) + struct.pack(">I", headers_length)
    prelude_crc = struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)

    message_parts = prelude + prelude_crc + headers_bytes + payload
    message_crc = struct.pack(">I", zlib.crc32(message_parts) & 0xFFFFFFFF)

    return message_parts + message_crc


def convert_messages_to_openai(
    bedrock_messages: List[Dict[str, Any]],
    system: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Convert Bedrock message format to OpenAI format."""
    openai_messages = []

    # Add system message if present
    if system:
        system_text = " ".join(item.get("text", "") for item in system)
        if system_text:
            openai_messages.append({"role": "system", "content": system_text})

    # Convert Bedrock messages to OpenAI format
    for msg in bedrock_messages:
        role = msg.get("role")
        content = ""

        # Extract text from content array
        if "content" in msg:
            for content_item in msg["content"]:
                if "text" in content_item:
                    content += content_item["text"]

        openai_messages.append({"role": role, "content": content})

    return openai_messages


async def convert_bedrock_to_openai(
    model_id: str, bedrock_request: Dict[str, Any]
) -> Dict[str, Any]:
    """Convert Bedrock Converse API format to OpenAI format."""
    completion_params = {"model": model_id}

    # Convert messages
    messages = convert_messages_to_openai(
        bedrock_request.get("messages", []), bedrock_request.get("system", [])
    )
    completion_params["messages"] = messages

    # Map inference configuration
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

    # Add any additional model fields
    if "additionalModelRequestFields" in bedrock_request:
        completion_params.update(bedrock_request["additionalModelRequestFields"])

    return completion_params


async def convert_bedrock_to_openai_streaming(
    model_id: str, bedrock_request: Dict[str, Any]
) -> Dict[str, Any]:
    completion_params = {
        "model": model_id,
        "stream": True,
    }
    messages = convert_messages_to_openai(
        bedrock_request.get("messages", []), bedrock_request.get("system", [])
    )
    completion_params["messages"] = messages

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
        completion_params.update(bedrock_request["additionalModelRequestFields"])

    return completion_params


async def convert_openai_to_bedrock(openai_response: Dict[str, Any]) -> Dict[str, Any]:
    """Convert OpenAI format to Bedrock Converse API format."""
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

    # Add stop reason if present
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
    message_started = False
    content_block_index = 0

    async for chunk in openai_stream:
        print(f"chunk: {chunk}")
        # chunk looks like:
        # {
        #   "id": "...",
        #   "object": "chat.completion.chunk",
        #   "created": 1681234567,
        #   "choices": [
        #       {
        #         "delta": {"role": "assistant"} or {"content": "partial text"},
        #         "index":0,
        #         "finish_reason": null or "stop"
        #       }
        #    ]
        # }
        delta = chunk.choices[0].delta
        finish_reason = chunk.choices[0].finish_reason

        # When role=assistant appears, start message
        if delta.role and not message_started:
            # Just return the role payload
            event_payload = json.dumps({"role": delta.role}).encode("utf-8")
            yield create_event_message(event_payload, "messageStart")
            message_started = True

        if delta.content:
            # Provide contentBlockDelta fields directly, not nested under "contentBlockDelta"
            event_payload = json.dumps(
                {
                    "contentBlockIndex": content_block_index,
                    "delta": {"text": delta.content},
                }
            ).encode("utf-8")
            yield create_event_message(event_payload, "contentBlockDelta")

        if finish_reason == "stop":
            # Just the stopReason field at top-level
            event_payload = json.dumps({"stopReason": "end_turn"}).encode("utf-8")
            yield create_event_message(event_payload, "messageStop")


def convert_bedrock_to_openai_streaming(model_id: str, bedrock_body: dict):
    # Convert Bedrock request structure to OpenAI request
    # Example: Extract user messages from bedrock_body['messages'] and map to OpenAI.
    # Bedrock: messages=[{"role": "user", "content": [{"text": "Hello"}]}]
    # OpenAI: messages=[{"role":"user", "content":"Hello"}]
    openai_messages = []
    for msg in bedrock_body.get("messages", []):
        role = msg.get("role", "user")
        # Combine all content blocks into one text (if multiple)
        text_parts = [c.get("text", "") for c in msg.get("content", [])]
        text = " ".join(text_parts)
        openai_messages.append({"role": role, "content": text})

    openai_params = {
        "model": model_id,  # or any model ID you have
        "messages": openai_messages,
        "stream": True,
        # Add other parameters as needed (temperature, top_p, etc.)
    }
    return openai_params


@app.post("/bedrock/model/{model_id}/converse-stream")
async def handle_bedrock_streaming_request(model_id: str, request: Request):
    try:
        body = await request.json()

        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            api_key = auth_header[len("Bearer ") :]
        else:
            print(f"Missing or invalid Authorization header")
            return JSONResponse(
                status_code=401,
                content={"error": "Missing or invalid Authorization header"},
            )

        openai_params = convert_bedrock_to_openai_streaming(model_id, body)

        # Create the OpenAI client with the provided API key
        client = AsyncOpenAI(api_key=api_key, base_url=LITELLM_ENDPOINT)

        # Create the streaming completion
        # Note: The exact method name/path (`client.chat.completions.create`) may vary depending on the library version.
        stream = await client.chat.completions.create(**openai_params)

        return StreamingResponse(
            openai_stream_to_bedrock_chunks(stream),
            media_type="application/vnd.amazon.eventstream",  # Changed media type
        )
    except Exception as e:
        print(f"Exception: {e}")
        return JSONResponse(
            status_code=500, content={"error": f"Internal server error: {str(e)}"}
        )

    except Exception as e:
        print(f"Exception: {e}")
        return JSONResponse(
            status_code=500, content={"error": f"Internal server error: {str(e)}"}
        )


@app.get("/bedrock/health/liveliness")
async def health_check():
    print(f"reached health check")
    """Health check endpoint."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{LITELLM_ENDPOINT}/health/liveliness", timeout=5.0
            )
            print(f"health check status code: {response.status_code}")
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


@app.post("/bedrock/model/{model_id}/converse")
async def handle_bedrock_request(model_id: str, request: Request):
    """Handle Bedrock Converse API requests."""
    print("reached converse api")
    try:
        body = await request.json()

        openai_format = await convert_bedrock_to_openai(model_id, body)

        auth_header = request.headers.get("Authorization")
        headers = {"Content-Type": "application/json"}
        if auth_header:
            headers["Authorization"] = auth_header

        async with httpx.AsyncClient() as client:
            response = await client.post(
                LITELLM_CHAT,
                json=openai_format,
                headers=headers,
                timeout=30.0,
            )

            if response.status_code != 200:
                return JSONResponse(
                    status_code=response.status_code,
                    content={"error": f"Error from LiteLLM endpoint: {response.text}"},
                )

            bedrock_response = await convert_openai_to_bedrock(response.json())
            print(
                f"converse api success returning bedrock_response: {bedrock_response}"
            )
            return JSONResponse(content=bedrock_response)

    except Exception as e:
        print(f"converse api errror e: {e}")
        return JSONResponse(
            status_code=500, content={"error": f"Internal server error: {str(e)}"}
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=3000)
