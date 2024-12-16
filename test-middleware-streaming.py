import boto3
import os
from botocore.client import Config
from botocore import UNSIGNED
from typing import Generator, Dict, Any


def create_bedrock_client():
    """
    Creates a Bedrock client with custom endpoint and authorization header.
    Uses environment variables for configuration.

    Required environment variables:
    - API_ENDPOINT: Custom Bedrock endpoint URL
    - API_KEY: Authorization bearer token
    - AWS_REGION: AWS region

    Returns:
        boto3.client: Configured Bedrock client
    """
    endpoint = os.getenv("API_ENDPOINT")
    api_key = os.getenv("API_KEY")
    region = os.getenv("AWS_REGION")

    if not all([endpoint, api_key, region]):
        raise ValueError(
            "Missing required environment variables: API_ENDPOINT, API_KEY, AWS_REGION"
        )

    # Initialize session and configure client
    session = boto3.Session()
    client_config = Config(
        signature_version=UNSIGNED,  # Disable SigV4 signing
        retries={"max_attempts": 10, "mode": "standard"},
    )

    # Create the Bedrock client
    client = session.client(
        "bedrock-runtime",
        endpoint_url=endpoint,
        config=client_config,
        region_name=region,
    )

    # Define authorization header handler
    def add_authorization_header(request, **kwargs):
        request.headers["Authorization"] = f"Bearer {api_key}"

    # Register the event handler
    client.meta.events.register("request-created.*", add_authorization_header)

    return client


def send_message_stream(
    client,
    message: str,
    model_id: str = "anthropic.claude-3-sonnet-20240229-v1:0",
    max_tokens: int = 1000,
    temperature: float = 0.7,
) -> Generator[Dict[str, Any], None, None]:
    """
    Sends a message to the Bedrock Converse API with streaming response.

    Args:
        client: Configured Bedrock client
        message (str): Message to send
        model_id (str): ID of the model to use
        max_tokens (int): Maximum number of tokens to generate
        temperature (float): Temperature for response generation

    Yields:
        dict: Streaming response events
    """
    try:
        response = client.converse_stream(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": message}]}],
            inferenceConfig={
                "maxTokens": max_tokens,
                "temperature": temperature,
            },
        )
        print(f"response: {response}")
        print(f"response['stream']: {response["stream"]}")

        # Process the streaming response
        for event in response["stream"]:
            yield event

    except Exception as e:
        print(f"Error in streaming request: {str(e)}")
        raise


def process_stream_response(event: Dict[str, Any]) -> str:
    """
    Processes a streaming response event and extracts the text content if present.

    Args:
        event (dict): Streaming response event

    Returns:
        str: Extracted text content or empty string
    """
    if "contentBlockDelta" in event:
        delta = event["contentBlockDelta"].get("delta", {})
        if "text" in delta:
            return delta["text"]
    return ""


def main():
    try:
        # Create the client
        client = create_bedrock_client()

        # Example of using streaming response
        print("Sending streaming request...")
        message = "Hi how are you."

        # Accumulate the response

        # Process the streaming response
        for event in send_message_stream(client, message):
            print(f"event: {event}")
            # Handle different event types
            if "internalServerException" in event:
                raise Exception(
                    f"Internal server error: {event['internalServerException']}"
                )
            elif "modelStreamErrorException" in event:
                raise Exception(
                    f"Model stream error: {event['modelStreamErrorException']}"
                )
            elif "validationException" in event:
                raise Exception(f"Validation error: {event['validationException']}")
            elif "throttlingException" in event:
                raise Exception(f"Throttling error: {event['throttlingException']}")
            # Handle metadata and stop events
            if "messageStop" in event:
                print("\n\nStream finished.")
                print(f"Stop reason: {event['messageStop'].get('stopReason')}")
            elif "metadata" in event:
                usage = event["metadata"].get("usage", {})
                if usage:
                    print(f"\nToken usage: {usage}")

    except Exception as e:
        print(f"Error in main: {str(e)}")


if __name__ == "__main__":
    main()
