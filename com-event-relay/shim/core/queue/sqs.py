"""AWS SQS consumer backend."""

from __future__ import annotations

import os
from typing import Iterator

import boto3

from .base import QueueConsumer, ReceivedMessage


class SqsConsumer(QueueConsumer):
    """Outbound pull consumer for an AWS SQS queue (long polling).

    Config (env vars):
        SQS_QUEUE_URL     Full queue URL.
        AWS_REGION        Region (optional if set in environment).
        RECEIVE_MAX_WAIT  Long-poll seconds per receive call (default 20, SQS max).

    Terminal actions:
        complete    -> DeleteMessage (removes it).
        abandon     -> ChangeMessageVisibility to 0 (immediate redelivery).
        dead_letter -> rely on the queue's redrive policy (a configured DLQ);
                       we delete from the source so it doesn't loop, after logging.
                       Configure a redrive policy on the queue for true DLQ capture.
    """

    def __init__(self) -> None:
        self._queue_url = os.environ["SQS_QUEUE_URL"]
        region = os.environ.get("AWS_REGION")
        self._client = boto3.client("sqs", region_name=region) if region else boto3.client("sqs")
        self._wait = int(os.environ.get("RECEIVE_MAX_WAIT", "20"))

    def receive(self) -> Iterator[ReceivedMessage]:
        while True:
            resp = self._client.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=self._wait,
                MessageAttributeNames=["All"],
            )
            messages = resp.get("Messages", [])
            if not messages:
                # No messages this poll; loop again (long poll already waited).
                continue
            for m in messages:
                props = {
                    k: v.get("StringValue", "")
                    for k, v in (m.get("MessageAttributes") or {}).items()
                }
                yield ReceivedMessage(
                    body=m["Body"].encode("utf-8"),
                    properties=props,
                    handle=m["ReceiptHandle"],
                )

    def complete(self, msg: ReceivedMessage) -> None:
        self._client.delete_message(QueueUrl=self._queue_url, ReceiptHandle=msg.handle)

    def abandon(self, msg: ReceivedMessage) -> None:
        # Make it immediately visible again for redelivery.
        self._client.change_message_visibility(
            QueueUrl=self._queue_url, ReceiptHandle=msg.handle, VisibilityTimeout=0
        )

    def dead_letter(self, msg: ReceivedMessage, reason: str) -> None:
        # SQS has no per-message dead-letter API; a redrive policy (maxReceiveCount
        # -> DLQ) handles poison messages. For a permanent/malformed message we
        # delete it from the source so it doesn't spin. Configure a DLQ + redrive
        # policy on the queue to capture these instead of dropping.
        self._client.delete_message(QueueUrl=self._queue_url, ReceiptHandle=msg.handle)
