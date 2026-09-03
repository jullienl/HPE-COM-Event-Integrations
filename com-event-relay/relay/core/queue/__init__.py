"""Queue abstraction package for the COM cloud relay.

Exposes a single factory, `get_publisher()`, that returns the correct
QueuePublisher implementation based on the QUEUE_BACKEND environment variable.
This keeps the relay itself cloud-agnostic — the only cloud-specific code lives
in the backend modules (servicebus.py, sqs.py).
"""

from .base import QueuePublisher, get_publisher

__all__ = ["QueuePublisher", "get_publisher"]
