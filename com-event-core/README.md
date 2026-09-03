# com-event-core

Shared building blocks for the HPE Compute Ops Management (COM) event
integrations — the single source of truth used by both:

- [com-event-relay](https://github.com/jullienl/com-event-relay) — cloud relay +
  outbound-only on-prem shim (durable queue in the middle);
- [com-event-bridge](https://github.com/jullienl/com-event-bridge) — single-box
  on-prem bridge (no cloud, no queue).

> AI-generated reference implementation. Review and harden before production use.

## Why this package exists

The relay's shim and the bridge both need to turn a raw COM webhook payload into
a target-system call. That logic — the event model, the field mapping, the
de-duplication, and the per-target adapters — used to be **duplicated** in both
repos, so any fix had to be made twice. This package holds it once; both
consumers depend on it.

## What's in it

| Module | Purpose |
|---|---|
| `com_event_core.normalize` | `CanonicalEvent` dataclass + `normalize(payload)` — COM → neutral event. |
| `com_event_core.dedup` | `DedupStore` — thread-safe SQLite TTL de-duplication. |
| `com_event_core.adapters` | `get_adapter()` + `TargetAdapter` and the built-in targets: `obm`, `servicenow`, `splunk`, `webhook`. |

Selection is by env var: `TARGET` picks the adapter; each adapter reads its own
target credentials from the environment (see each consumer's `.env.example`).

## Usage

```python
from com_event_core import normalize, DedupStore, get_adapter

adapter = get_adapter()            # selected by TARGET; validates its config
dedup = DedupStore()               # SQLite TTL store

event = normalize(com_payload)     # dict -> CanonicalEvent
if not dedup.is_duplicate(event.dedup_key):
    adapter.forward(event)         # raises on failure so the caller can retry
```

## Install

This package is part of the **HPE-COM-Event-Integrations** monorepo and is
consumed by `com-event-relay` (shim) and `com-event-bridge`. The container images
install it **from local source** (no PyPI publish required) — see each
Dockerfile's `pip install ./com-event-core` step, which is why the images build
straight from a clone.

For **local development** across the sibling projects, install it editable so
changes are picked up immediately. From a consumer subfolder (e.g. `com-event-relay/shim`
or `com-event-bridge/bridge`):

```bash
pip install -e ../../com-event-core
```

Or from the repo root:

```bash
pip install -e ./com-event-core
```

## Adding a new target

Add a module under `com_event_core/adapters/` implementing `TargetAdapter`
(map `CanonicalEvent` → the target's API in `forward()`), then register it in the
`_ADAPTERS` table in `com_event_core/adapters/__init__.py`. Both the relay shim
and the bridge pick it up automatically via `TARGET=<name>`.

## Versioning

Semantic versioning. A change to a mapping or adapter is a **minor** bump; a
breaking change to `CanonicalEvent` or an adapter's env contract is a **major**
bump. Consumers pin a compatible range (e.g. `com-event-core>=0.1,<0.2`).
