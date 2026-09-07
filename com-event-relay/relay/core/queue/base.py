"""Queue publisher interface + backend factory.

The relay depends only on the `QueuePublisher` interface and `get_publisher()`.
Concrete backends (Azure Service Bus, AWS SQS, ...) are imported lazily so the
image doesn't require every cloud SDK to be present/usable at runtime — only the
selected backend is imported.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod


class QueuePublisher(ABC):
    """Minimal contract every queue backend must implement."""

    @abstractmethod
    def publish(self, body: bytes, properties: dict[str, str] | None = None) -> None:
        """Enqueue a raw event payload with optional metadata properties.

        Must raise on failure so the relay can return 5xx. COM is fire-and-forget
        and will not resend, and a 5xx also counts against webhook health (10
        consecutive failures -> webhook DISABLED), so keep the backend highly
        available. `properties` are attached as message metadata
        (Service Bus application properties / SQS message attributes) so the
        shim can filter/route without re-parsing the body."""

    @abstractmethod
    def health(self) -> bool:
        """Readiness probe: return True only if the backend is reachable.

        Used by the relay's readiness endpoint so a broken queue connection
        marks the container 'not ready' and stops receiving traffic."""


def get_publisher() -> QueuePublisher:
    """Return the QueuePublisher selected by the QUEUE_BACKEND env var.

    QUEUE_BACKEND = "servicebus" (default) | "sqs"

    Validates that the required env vars for the chosen backend are present,
    failing fast at startup with a clear message rather than lazily on the
    first request.
    """
    backend = os.environ.get("QUEUE_BACKEND", "servicebus").strip().lower()

    if backend == "servicebus":
        _require(backend, ["SERVICE_BUS_CONNECTION", "QUEUE_NAME"])
        from .servicebus import ServiceBusPublisher

        return ServiceBusPublisher()

    if backend == "sqs":
        _require(backend, ["SQS_QUEUE_URL"])
        from .sqs import SqsPublisher

        return SqsPublisher()

    raise ValueError(
        f"Unsupported QUEUE_BACKEND '{backend}'. Use 'servicebus' or 'sqs'."
    )


def _require(backend: str, names: list[str]) -> None:
    """Raise a clear error if any required env var for the backend is missing.

    A name is considered present if either ``<name>`` or its file-backed form
    ``<name>_FILE`` is set, so secrets projected as files (vault/CSI/Docker/
    systemd) satisfy the check.
    """
    missing = [
        n for n in names
        if not os.environ.get(n) and not os.environ.get(f"{n}_FILE")
    ]
    if missing:
        raise RuntimeError(
            f"QUEUE_BACKEND='{backend}' requires env var(s): {', '.join(missing)}"
        )
