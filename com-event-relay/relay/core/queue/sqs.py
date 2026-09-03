"""AWS SQS backend for the queue abstraction."""

from __future__ import annotations

import os

import boto3

from .base import QueuePublisher


class SqsPublisher(QueuePublisher):
    """Publishes raw event bodies to an AWS SQS queue.

    Config (env vars):
        SQS_QUEUE_URL   Full queue URL, e.g.
                        https://sqs.eu-west-1.amazonaws.com/123456789012/com-events
        AWS_REGION      Region of the queue (optional if set in the environment).

    Credentials are resolved by the default boto3 chain — in production use an
    IAM task/execution role (App Runner / ECS / Lambda), not static keys.
    """

    def __init__(self) -> None:
        self._queue_url = os.environ["SQS_QUEUE_URL"]
        region = os.environ.get("AWS_REGION")
        self._client = boto3.client("sqs", region_name=region) if region else boto3.client("sqs")

    def publish(self, body: bytes, properties: dict[str, str] | None = None) -> None:
        # SQS message bodies are UTF-8 text; COM payloads are JSON text.
        kwargs = {
            "QueueUrl": self._queue_url,
            "MessageBody": body.decode("utf-8"),
        }
        if properties:
            # Surfaced to the shim as message attributes (all String type).
            kwargs["MessageAttributes"] = {
                k: {"DataType": "String", "StringValue": v}
                for k, v in properties.items()
            }
        self._client.send_message(**kwargs)

    def health(self) -> bool:
        # A cheap metadata call validates credentials + queue reachability
        # without sending or receiving a message.
        try:
            self._client.get_queue_attributes(
                QueueUrl=self._queue_url, AttributeNames=["QueueArn"]
            )
            return True
        except Exception:
            return False
