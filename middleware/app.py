from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
import json
from typing import Dict, Any, AsyncGenerator, List, Optional

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
