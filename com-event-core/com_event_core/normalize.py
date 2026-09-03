"""Canonical event model + COM normaliser.

Every adapter consumes a `CanonicalEvent`, never the raw COM payload. This keeps
adapters tiny and shields them from COM schema changes — when COM's format
shifts, you update `normalize()` in one place and all adapters keep working.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


# Canonical severity scale (targets map from this to their own scales).
SEVERITY_NORMAL = "normal"
SEVERITY_WARNING = "warning"
SEVERITY_MINOR = "minor"
SEVERITY_MAJOR = "major"
SEVERITY_CRITICAL = "critical"


@dataclass
class CanonicalEvent:
    """Normalised, target-agnostic representation of a COM event."""

    event_id: str
    operation: str
    title: str
    severity: str
    resource_serial: str | None
    resource_model: str | None
    mgmt_url: str | None
    time_created: str | None
    tags: dict[str, str] = field(default_factory=dict)
    dedup_key: str = ""
    raw: dict = field(default_factory=dict)


def _map_severity(summary: str | None) -> str:
    """Map COM hardware health summary to the canonical severity scale."""
    return {
        "OK": SEVERITY_NORMAL,
        "WARNING": SEVERITY_MINOR,
        "CRITICAL": SEVERITY_CRITICAL,
        "UNKNOWN": SEVERITY_WARNING,
    }.get((summary or "").upper(), SEVERITY_WARNING)


def normalize(payload: dict) -> CanonicalEvent:
    """Convert a raw COM event payload into a CanonicalEvent.

    Mirrors the field mapping proven in the OBM shim, but produces a neutral
    structure any adapter can consume.
    """
    hw = payload.get("hardware", {}) or {}
    tags = payload.get("tags", {}) or {}
    bmc = hw.get("bmc", {}) or {}

    event_id = str(payload.get("id", ""))
    operation = str(payload.get("operation", ""))
    dedup_key = hashlib.sha1(f"{event_id}|{operation}".encode()).hexdigest()

    return CanonicalEvent(
        event_id=event_id,
        operation=operation,
        title=payload.get("name") or "COM event",
        severity=_map_severity((hw.get("health", {}) or {}).get("summary")),
        resource_serial=hw.get("serialNumber"),
        resource_model=hw.get("model"),
        mgmt_url=f"https://{bmc.get('ip')}" if bmc.get("ip") else None,
        time_created=payload.get("updatedAt"),
        tags=tags,
        dedup_key=dedup_key,
        raw=payload,
    )
