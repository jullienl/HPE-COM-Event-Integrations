"""Queue consumer abstraction for the shim.

Mirrors the relay's publisher abstraction: the shim depends only on the
`QueueConsumer` interface and `get_consumer()`. Concrete backends (Azure Service
Bus, AWS SQS) are imported lazily so only the selected cloud SDK is needed.
"""

from .base import QueueConsumer, ReceivedMessage, get_consumer

__all__ = ["QueueConsumer", "ReceivedMessage", "get_consumer"]
