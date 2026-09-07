"""Target adapter interface.

An adapter is the only target-specific code in the pipeline. It maps a
CanonicalEvent to the target's native format and delivers it. Everything else
(handshake, auth, normalise, dedup, queue/spool, retry, logging) is shared.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from com_event_core.normalize import CanonicalEvent


class TargetAdapter(ABC):
    """Contract every target integration implements."""

    #: Short name used to select the adapter via the TARGETS env var.
    name: str = "base"

    @abstractmethod
    def forward(self, event: CanonicalEvent) -> None:
        """Deliver the event to the target. MUST raise on failure so the caller
        retries (no silent loss)."""

    def health(self) -> bool:
        """Optional readiness check that the target is reachable."""
        return True
