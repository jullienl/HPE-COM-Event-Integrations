"""Queue consumer interface + backend factory.

A consumer yields `ReceivedMessage` objects and lets the worker acknowledge each
one with exactly one terminal action:

    complete(msg)     -> processed OK; remove from queue.
    abandon(msg)      -> transient failure; redeliver (broker dead-letters after N).
    dead_letter(msg)  -> permanent failure (e.g. malformed); never redeliver.

Backends map these onto their native semantics (Service Bus complete/abandon/
dead-letter; SQS delete / visibility-timeout return / dead-letter queue).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class ReceivedMessage:
    """A message pulled from the queue, backend-agnostic."""

    body: bytes
    properties: dict[str, str] = field(default_factory=dict)
    # Opaque handle the backend uses to ack the message (lock token / receipt handle).
    handle: object = None


class QueueConsumer(ABC):
    """Outbound-only pull consumer. Implementations open an OUTBOUND connection
    to the cloud queue — they never listen on an inbound port."""

    @abstractmethod
    def receive(self) -> Iterator[ReceivedMessage]:
        """Yield messages as they arrive. Blocks/polls as appropriate."""

    @abstractmethod
    def complete(self, msg: ReceivedMessage) -> None:
        """Acknowledge success; remove the message from the queue."""

    @abstractmethod
    def abandon(self, msg: ReceivedMessage) -> None:
        """Release the message for redelivery (transient failure)."""

    @abstractmethod
    def dead_letter(self, msg: ReceivedMessage, reason: str) -> None:
        """Move the message to the dead-letter queue (permanent failure)."""

    def close(self) -> None:  # optional cleanup hook
        pass


def get_consumer() -> QueueConsumer:
    """Return the QueueConsumer selected by QUEUE_BACKEND (servicebus | sqs)."""
    backend = os.environ.get("QUEUE_BACKEND", "servicebus").strip().lower()

    if backend == "servicebus":
        _require(backend, ["SERVICE_BUS_CONNECTION", "QUEUE_NAME"])
        from .servicebus import ServiceBusConsumer

        return ServiceBusConsumer()

    if backend == "sqs":
        _require(backend, ["SQS_QUEUE_URL"])
        from .sqs import SqsConsumer

        return SqsConsumer()

    raise ValueError(
        f"Unsupported QUEUE_BACKEND '{backend}'. Use 'servicebus' or 'sqs'."
    )


def _require(backend: str, names: list[str]) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise RuntimeError(
            f"QUEUE_BACKEND='{backend}' requires env var(s): {', '.join(missing)}"
        )
