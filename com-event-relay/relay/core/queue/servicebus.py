"""Azure Service Bus backend for the queue abstraction."""

from __future__ import annotations

import os

from azure.servicebus import ServiceBusClient, ServiceBusMessage

from .base import QueuePublisher


class ServiceBusPublisher(QueuePublisher):
    """Publishes raw event bodies to an Azure Service Bus queue.

    Config (env vars):
        SERVICE_BUS_CONNECTION  Send-scoped connection string (source from Key Vault).
        QUEUE_NAME              Target queue name (e.g. com-events).
    """

    def __init__(self) -> None:
        self._conn = os.environ["SERVICE_BUS_CONNECTION"]
        self._queue = os.environ["QUEUE_NAME"]

    def publish(self, body: bytes, properties: dict[str, str] | None = None) -> None:
        # Reconnect per message keeps the relay stateless and avoids stale
        # AMQP links on a long-idle container. Fine for webhook-rate traffic.
        message = ServiceBusMessage(body)
        if properties:
            # Surfaced to the shim as application (custom) properties.
            message.application_properties = {
                k: v for k, v in properties.items()
            }
        with ServiceBusClient.from_connection_string(self._conn) as client:
            with client.get_queue_sender(self._queue) as sender:
                sender.send_messages(message)

    def health(self) -> bool:
        # Opening a sender validates the connection string and namespace
        # reachability without sending a message.
        try:
            with ServiceBusClient.from_connection_string(self._conn) as client:
                with client.get_queue_sender(self._queue):
                    return True
        except Exception:
            return False
