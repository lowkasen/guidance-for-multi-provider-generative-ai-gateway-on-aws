import boto3
import os
from botocore.client import Config
from botocore import UNSIGNED
from botocore.exceptions import ClientError


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

    # Get configuration from environment variables
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


def send_message(client, message, model_id="anthropic.claude-3-sonnet-20240229-v1:0"):
    """
    Sends a message to the Bedrock Converse API.

    Args:
        client: Configured Bedrock client
        message (str): Message to send
        model_id (str): ID of the model to use

    Returns:
        dict: API response
    """
    try:
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": message}]}],
        )
        return response
    except Exception as e:
        print(f"Error sending message: {str(e)}")
        raise


def main():
    try:
        # Create the client
        client = create_bedrock_client()

        # Send a test message
        response = send_message(client=client, message="Hello, how are you?")

        print("API Response:", response)

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        error_message = e.response["Error"]["Message"]
        print(f"e.response: {e.response}")

        print(f"AWS Error: {error_code} - {error_message}")
    except Exception as e:
        print(f"Unexpected error: {str(e)}")


if __name__ == "__main__":
    main()
