"""Canonical event model + COM normaliser.

Every adapter consumes a `CanonicalEvent`, never the raw COM payload. This keeps
adapters tiny and shields them from COM schema changes — when COM's format
shifts, you update `normalize()` in one place and all adapters keep working.

Resource types
--------------
COM delivers several webhook resource types. `normalize()` dispatches on the
payload's ``type`` field and currently understands:

* ``compute-ops-mgmt/server`` — a server object (health, hardware, bmc, ...).
  Used for **health-transition** webhooks (raise when health leaves ``OK``,
  clear when it returns).
* ``compute-ops-mgmt/alert`` — an individual COM alert (description, resolution,
  severity, ``cleared``/``clearedAt``). Used for **alert-lifecycle** webhooks.

Anything else falls back to a best-effort generic mapping so no event is lost.

Note on the type namespace: COM's ``eventFilter`` uses the short form
(``type eq 'compute-ops/server'``) while the delivered payload's ``type`` is the
long form (``compute-ops-mgmt/server``). We match on the suffix so both work.

Lifecycle (raise / clear)
-------------------------
Each event carries an ``action`` of ``raise`` or ``clear`` and a stable
``correlation_key`` that is the **same** for a problem and its later recovery, so
adapters can close/resolve the item they previously opened. ``dedup_key`` stays
per-(problem, action, severity) so repeats are suppressed but a raise and its
clear are never collapsed into one.
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

# Lifecycle actions.
ACTION_RAISE = "raise"
ACTION_CLEAR = "clear"


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

    # --- Lifecycle + enrichment (added for raise/clear support) ----------
    #: 'server' | 'alert' | 'generic' — which COM resource type this came from.
    source_type: str = "generic"
    #: 'raise' (problem) or 'clear' (recovery/resolution).
    action: str = ACTION_RAISE
    #: Stable key shared by a problem and its later clear (for close-on-clear).
    correlation_key: str = ""
    #: Friendly resource name (server name), when available.
    resource_name: str | None = None
    #: Product/part number parsed from the COM asset id, when available.
    part_number: str | None = None
    #: Longer human-readable detail (alert description / server health detail).
    description: str | None = None
    #: Recommended remediation text (alerts carry this).
    resolution: str | None = None
    #: Coarse classification (e.g. 'hardware', 'Operational').
    category: str | None = None


# Values in a server health block that are NOT problems (so we can list the
# genuinely unhealthy subsystems in the description).
_HEALTHY_VALUES = {"OK", "REDUNDANT", "NOT_PRESENT", "READY", "ENABLED"}


def _dedup(*parts: object) -> str:
    """Stable hash over the given parts."""
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()


def _split_asset_id(asset_id: str | None) -> tuple[str | None, str | None]:
    """Split a COM asset id 'PARTNUMBER+SERIAL' into (part_number, serial).

    e.g. 'P28948-B21+CZ2311004G' -> ('P28948-B21', 'CZ2311004G').
    """
    if asset_id and "+" in asset_id:
        part, _, serial = asset_id.partition("+")
        return (part or None), (serial or None)
    return None, (asset_id or None)


def _map_health_severity(summary: str | None) -> str:
    """Map a COM server hardware health summary to the canonical scale."""
    return {
        "OK": SEVERITY_NORMAL,
        "WARNING": SEVERITY_MINOR,
        "CRITICAL": SEVERITY_CRITICAL,
        "UNKNOWN": SEVERITY_WARNING,
    }.get((summary or "").upper(), SEVERITY_WARNING)


def _map_alert_severity(severity: str | None) -> str:
    """Map a COM alert severity to the canonical scale."""
    return {
        "CRITICAL": SEVERITY_CRITICAL,
        "WARNING": SEVERITY_WARNING,
        "OK": SEVERITY_NORMAL,
        "INFO": SEVERITY_NORMAL,
        "INFORMATIONAL": SEVERITY_NORMAL,
    }.get((severity or "").upper(), SEVERITY_WARNING)


def normalize(payload: dict) -> CanonicalEvent:
    """Convert a raw COM webhook payload into a CanonicalEvent.

    Dispatches on the payload ``type`` (server / alert), falling back to a
    generic mapping for any other resource type so nothing is dropped.
    """
    ptype = str(payload.get("type", "")).lower()
    if ptype.endswith("/server"):
        return _normalize_server(payload)
    if ptype.endswith("/alert"):
        return _normalize_alert(payload)
    return _normalize_generic(payload)


def _normalize_server(payload: dict) -> CanonicalEvent:
    """Map a `compute-ops-mgmt/server` payload (health-transition webhooks)."""
    hw = payload.get("hardware", {}) or {}
    health = hw.get("health", {}) or {}
    bmc = hw.get("bmc", {}) or {}

    summary = health.get("summary")
    severity = _map_health_severity(summary)
    # Health back to OK == recovery; anything else is a problem.
    action = ACTION_CLEAR if severity == SEVERITY_NORMAL else ACTION_RAISE

    part_from_id, serial_from_id = _split_asset_id(payload.get("id"))
    serial = hw.get("serialNumber") or serial_from_id
    part_number = hw.get("productId") or part_from_id
    name = payload.get("name") or serial or "server"

    # One ticket per server (health aggregate) -> correlate on serial.
    correlation_key = f"server:{serial or payload.get('id')}"
    operation = str(payload.get("operation", "Updated") or "Updated")

    if action == ACTION_CLEAR:
        title = f"Server {name} returned to healthy (health OK)"
    else:
        title = f"Server {name} health {summary or 'not OK'}"

    return CanonicalEvent(
        event_id=str(payload.get("id", "")),
        operation=operation,
        title=title,
        severity=severity,
        resource_serial=serial,
        resource_model=hw.get("model"),
        mgmt_url=f"https://{bmc.get('ip')}" if bmc.get("ip") else None,
        time_created=payload.get("updatedAt"),
        tags=payload.get("tags", {}) or {},
        dedup_key=_dedup(correlation_key, action, severity),
        raw=payload,
        source_type="server",
        action=action,
        correlation_key=correlation_key,
        resource_name=payload.get("name"),
        part_number=part_number,
        description=_server_health_detail(name, summary, health),
        category="hardware-health",
    )


def _server_health_detail(name: str, summary: str | None, health: dict) -> str:
    """Compact description listing the subsystems that are not healthy."""
    lines = [f"Server: {name}", f"Health summary: {summary or 'unknown'}"]
    unhealthy = [
        f"{k}={v}"
        for k, v in health.items()
        if k not in ("summary", "healthLED")
        and isinstance(v, str)
        and v.upper() not in _HEALTHY_VALUES
    ]
    if unhealthy:
        lines.append("Components not OK: " + ", ".join(unhealthy))
    return "\n".join(lines)


def _normalize_alert(payload: dict) -> CanonicalEvent:
    """Map a `compute-ops-mgmt/alert` payload (alert-lifecycle webhooks)."""
    device = payload.get("device", {}) or {}
    part_number, serial = _split_asset_id(device.get("id"))

    severity = _map_alert_severity(payload.get("severity"))
    # An alert is a recovery when COM has cleared/deleted it — regardless of the
    # severity it still carries.
    cleared = bool(payload.get("cleared")) or bool(payload.get("clearedAt"))
    operation = str(payload.get("operation", "") or "")
    action = ACTION_CLEAR if (cleared or operation.lower() == "deleted") else ACTION_RAISE

    alert_id = str(payload.get("id", ""))
    correlation_key = f"alert:{alert_id}"  # same id on create and clear/delete
    description = payload.get("description")
    title = (description or payload.get("messageId") or "COM alert").splitlines()[0][:200]

    return CanonicalEvent(
        event_id=alert_id,
        operation=operation or ("Deleted" if action == ACTION_CLEAR else "Created"),
        title=title,
        severity=severity,
        resource_serial=serial,
        resource_model=None,
        mgmt_url=None,
        time_created=payload.get("createdAt"),
        tags={},
        dedup_key=_dedup(correlation_key, action, severity),
        raw=payload,
        source_type="alert",
        action=action,
        correlation_key=correlation_key,
        resource_name=serial,
        part_number=part_number,
        description=description,
        resolution=payload.get("resolution"),
        category=payload.get("category") or payload.get("alertType"),
    )


def _normalize_generic(payload: dict) -> CanonicalEvent:
    """Best-effort mapping for any other COM resource type (nothing is lost)."""
    hw = payload.get("hardware", {}) or {}
    bmc = hw.get("bmc", {}) or {}
    event_id = str(payload.get("id", ""))
    operation = str(payload.get("operation", "") or "")
    part_number, serial_from_id = _split_asset_id(payload.get("id"))
    correlation_key = f"generic:{event_id}"

    return CanonicalEvent(
        event_id=event_id,
        operation=operation,
        title=payload.get("name") or "COM event",
        severity=_map_health_severity((hw.get("health", {}) or {}).get("summary")),
        resource_serial=hw.get("serialNumber") or serial_from_id,
        resource_model=hw.get("model"),
        mgmt_url=f"https://{bmc.get('ip')}" if bmc.get("ip") else None,
        time_created=payload.get("updatedAt") or payload.get("createdAt"),
        tags=payload.get("tags", {}) or {},
        dedup_key=_dedup(correlation_key, operation),
        raw=payload,
        source_type="generic",
        action=ACTION_RAISE,
        correlation_key=correlation_key,
        resource_name=payload.get("name"),
        part_number=hw.get("productId") or part_number,
        category=str(payload.get("type", "")) or None,
    )
