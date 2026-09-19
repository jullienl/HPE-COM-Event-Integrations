"""com_event_core — shared building blocks for the COM event integrations.

This package is the single source of truth for the pieces that were previously
duplicated between `com-event-relay` (the cloud relay + on-prem shim) and
`com-event-bridge` (the single-box on-prem bridge):

- `normalize`  — COM payload -> one or more neutral `CanonicalEvent`s (a
                 server snapshot yields one per monitored condition).
- `dedup`      — thread-safe SQLite TTL de-duplication store.
- `adapters`   — target integrations (OBM, ServiceNow, OpsRamp, HaloITSM,
                 Splunk, generic webhook) selected by the `TARGETS` env var
                 via `get_adapters()`.
- `deliver`    — fan-out helper that delivers one event to one or many adapters
                 with per-adapter de-dup and partial-failure retry.
- `enrich`     — optional analysis stage that runs *between* normalize and
                 deliver (selected by `ENRICHERS`, default none), mutating the
                 event so every adapter renders the enriched version.
- `secrets`    — `get_secret()`: resolve sensitive values from a file
                 (`<name>_FILE`, e.g. a vault/CSI/Docker-projected secret) or the
                 environment, so credentials can stay out of `.env`.

Both consumers depend on this package, so a fix to a mapping or adapter is made
once, here.
"""

from .normalize import CanonicalEvent, normalize, ACTION_RAISE, ACTION_CLEAR
from .dedup import DedupStore
from .adapters import get_adapters, TargetAdapter
from .deliver import deliver, deliver_events, PartialDeliveryError
from .enrich import Enricher, enrich_events, get_enrichers
from .secrets import get_secret

__all__ = [
    "CanonicalEvent",
    "normalize",
    "ACTION_RAISE",
    "ACTION_CLEAR",
    "DedupStore",
    "get_adapters",
    "TargetAdapter",
    "deliver",
    "deliver_events",
    "PartialDeliveryError",
    "Enricher",
    "enrich_events",
    "get_enrichers",
    "get_secret",
]

__version__ = "0.1.0"
