from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
import json
from typing import Dict, Any, AsyncGenerator, List, Optional
from openai import AsyncOpenAI
import struct
import google_crc32c

app = FastAPI()

# Since we're in the same container, we can use localhost
LITELLM_ENDPOINT = "http://localhost:4000"
LITELLM_CHAT = f"{LITELLM_ENDPOINT}/v1/chat/completions"


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
            "stop": "stop_sequence",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "content_filtered",
        }
        finish_reason = openai_response["choices"][0]["finish_reason"]
        bedrock_response["stopReason"] = stop_reason_map.get(finish_reason, "end_turn")

    return bedrock_response


def map_finish_reason_to_bedrock(finish_reason: str) -> str:
    stop_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "function_call": "tool_use",  # OpenAI calls these 'functions', could map to tool use
        "content_filter": "content_filtered",
    }
    return stop_reason_map.get(finish_reason, "end_turn")


def encode_event_stream_message(payload: dict, event_type: str) -> bytes:
    # Convert payload to bytes
    payload_bytes = json.dumps(payload).encode("utf-8")

    def encode_header(name: bytes, value: bytes):
        # Header format:
        # 1 byte: length of name
        # name bytes
        # 1 byte: type (7 for string)
        # 2 bytes: length of value
        # value bytes
        return (
            struct.pack("!B", len(name))
            + name
            + struct.pack("!B", 7)  # string type
            + struct.pack("!H", len(value))
            + value
        )

    # Encode the required headers in the correct order
    headers = (
        encode_header(b":event-type", event_type.encode("utf-8"))
        + encode_header(b":content-type", b"application/json")
        + encode_header(b":message-type", b"event")
    )

    headers_length = len(headers)
    # Total length = 4 (total_length) + 4 (headers_length) + 4 (prelude_crc) + headers_length + payload_length + 4 (message_crc)
    total_length = 4 + 4 + 4 + headers_length + len(payload_bytes) + 4

    # Write the prelude: total_length (4 bytes) + headers_length (4 bytes)
    prelude = struct.pack("!I", total_length) + struct.pack("!I", headers_length)

    # Compute prelude CRC
    prelude_crc = google_crc32c.value(prelude)

    # Construct the message without the final message CRC
    message_without_crc = (
        prelude + struct.pack("!I", prelude_crc) + headers + payload_bytes
    )

    # Compute the message CRC over everything so far
    message_crc = google_crc32c.value(message_without_crc)

    # Final message
    message = message_without_crc + struct.pack("!I", message_crc)
    return message


async def openai_stream_to_bedrock_chunks(stream) -> AsyncGenerator[bytes, None]:
    """
    Convert an OpenAI streaming response (ChatCompletion) into AWS Bedrock-compatible event stream messages.
    """

    content_block_index = 0
    message_start_sent = False

    async for chunk in stream:
        # Each 'chunk' is expected to be an OpenAI partial response
        if not chunk or not hasattr(chunk, "choices") or len(chunk.choices) == 0:
            continue

        choice = chunk.choices[0]
        delta = choice.delta
        finish_reason = choice.finish_reason
        role = delta.role if hasattr(delta, "role") else None
        text = delta.content if hasattr(delta, "content") else None

        # Handle finishing
        if finish_reason is not None:
            # Send a messageStop event
            bedrock_stop = {
                "messageStop": {
                    "stopReason": map_finish_reason_to_bedrock(finish_reason)
                },
            }
            yield encode_event_stream_message(bedrock_stop, "messageStop")
            break

        # If we get a role and haven't started a message yet, send messageStart
        if role and not message_start_sent:
            bedrock_message_start = {"messageStart": {"role": role}}
            yield encode_event_stream_message(bedrock_message_start, "messageStart")
            message_start_sent = True

        # If we have text content, send a contentBlockDelta event
        if text is not None:
            if not message_start_sent:
                # If no messageStart was sent, default to assistant
                bedrock_message_start = {
                    "messageStart": {"role": "assistant"},
                }
                yield encode_event_stream_message(bedrock_message_start, "messageStart")
                message_start_sent = True

            bedrock_chunk = {
                "contentBlockDelta": {
                    "contentBlockIndex": content_block_index,
                    "delta": {"text": text},
                },
            }
            yield encode_event_stream_message(bedrock_chunk, "contentBlockDelta")


@app.post("/bedrock/model/{model_id}/converse-stream")
async def handle_bedrock_streaming_request(model_id: str, request: Request):
    try:
        body = await request.json()

        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            api_key = auth_header[len("Bearer ") :]
        else:
            return JSONResponse(
                status_code=401,
                content={"error": "Missing or invalid Authorization header"},
            )

        openai_params = await convert_bedrock_to_openai_streaming(model_id, body)

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
        return JSONResponse(
            status_code=500, content={"error": f"Internal server error: {str(e)}"}
        )

    except Exception as e:
        print(f"exception: {e}")
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
