"""Azure Service Bus consumer backend."""

from __future__ import annotations

import os
from typing import Iterator

from azure.servicebus import ServiceBusClient

from .base import QueueConsumer, ReceivedMessage


class ServiceBusConsumer(QueueConsumer):
    """Outbound pull consumer for an Azure Service Bus queue.

    Config (env vars):
        SERVICE_BUS_CONNECTION  Listen-scoped connection string.
        QUEUE_NAME              Queue to drain (e.g. com-events).
        RECEIVE_MAX_WAIT        Seconds to wait for messages before looping (default 30).
    """

    def __init__(self) -> None:
        self._conn = os.environ["SERVICE_BUS_CONNECTION"]
        self._queue = os.environ["QUEUE_NAME"]
        self._max_wait = int(os.environ.get("RECEIVE_MAX_WAIT", "30"))
        self._client = ServiceBusClient.from_connection_string(self._conn)
        self._receiver = self._client.get_queue_receiver(
            queue_name=self._queue, max_wait_time=self._max_wait
        )

    def receive(self) -> Iterator[ReceivedMessage]:
        for msg in self._receiver:
            props = {}
            if msg.application_properties:
                # Keys/values may be bytes; normalise to str.
                for k, v in msg.application_properties.items():
                    key = k.decode() if isinstance(k, bytes) else str(k)
                    val = v.decode() if isinstance(v, bytes) else str(v)
                    props[key] = val
            yield ReceivedMessage(body=bytes(str(msg), "utf-8"), properties=props, handle=msg)

    def complete(self, msg: ReceivedMessage) -> None:
        self._receiver.complete_message(msg.handle)

    def abandon(self, msg: ReceivedMessage) -> None:
        self._receiver.abandon_message(msg.handle)

    def dead_letter(self, msg: ReceivedMessage, reason: str) -> None:
        self._receiver.dead_letter_message(msg.handle, reason=reason)

    def close(self) -> None:
        self._receiver.close()
        self._client.close()
