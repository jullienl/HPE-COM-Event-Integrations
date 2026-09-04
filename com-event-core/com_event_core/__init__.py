"""com_event_core — shared building blocks for the COM event integrations.

This package is the single source of truth for the pieces that were previously
duplicated between `com-event-relay` (the cloud relay + on-prem shim) and
`com-event-bridge` (the single-box on-prem bridge):

- `normalize`  — COM payload -> neutral `CanonicalEvent`.
- `dedup`      — thread-safe SQLite TTL de-duplication store.
- `adapters`   — target integrations (OBM, ServiceNow, OpsRamp, HaloITSM,
                 Splunk, generic webhook) selected by the `TARGET` env var via
                 `get_adapter()`.

Both consumers depend on this package, so a fix to a mapping or adapter is made
once, here.
"""

from .normalize import CanonicalEvent, normalize, ACTION_RAISE, ACTION_CLEAR
from .dedup import DedupStore
from .adapters import get_adapter, TargetAdapter

__all__ = [
    "CanonicalEvent",
    "normalize",
    "ACTION_RAISE",
    "ACTION_CLEAR",
    "DedupStore",
    "get_adapter",
    "TargetAdapter",
]

__version__ = "0.1.0"
