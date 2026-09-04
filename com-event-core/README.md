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
| `com_event_core.adapters` | `get_adapter()` + `TargetAdapter` and the built-in targets: `obm`, `servicenow`, `opsramp`, `halo`, `splunk`, `webhook`. |

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

## Example: one COM event across every adapter

To make the mappings concrete, here is a single COM **server health** webhook
(a raise — health went `CRITICAL`) followed by the `CanonicalEvent` it
normalises to and the exact object each adapter builds and sends. An alert
payload flows the same way; only `source_type`/`correlation_key` differ.

> **Snapshot vs. live output.** The payloads below are a hand-checked snapshot
> for quick reading. To see the mapping for the **current** code (after you tweak
> an adapter, or for the clear/alert variants), run the companion script — it
> builds these same objects live, sends nothing, and prints them:
>
> ```bash
> python examples/dump_payloads.py            # server raise (shown below)
> python examples/dump_payloads.py server clear
> python examples/dump_payloads.py alert raise
> python examples/dump_payloads.py alert clear
> ```

**1. Raw COM payload in** (`compute-ops-mgmt/server`, health transitioned to CRITICAL):

```json
{
  "type": "compute-ops-mgmt/server",
  "id": "P28948-B21+CZ2311004G",
  "name": "ESX-node-01",
  "operation": "Updated",
  "updatedAt": "2025-01-01T10:00:00Z",
  "hardware": {
    "serialNumber": "CZ2311004G",
    "productId": "P28948-B21",
    "model": "ProLiant DL360 Gen11",
    "bmc": { "ip": "10.0.0.5" },
    "health": { "summary": "CRITICAL", "fans": "OK", "powerSupplies": "CRITICAL", "memory": "OK" }
  }
}
```

**2. `normalize()` → CanonicalEvent** (the neutral object every adapter consumes):

```python
CanonicalEvent(
    event_id="P28948-B21+CZ2311004G",
    operation="Updated",
    title="Server ESX-node-01 health CRITICAL",
    severity="critical",
    resource_serial="CZ2311004G",
    resource_model="ProLiant DL360 Gen11",
    mgmt_url="https://10.0.0.5",
    time_created="2025-01-01T10:00:00Z",
    tags={},
    dedup_key="82b8f550…",          # sha1(correlation_key | action | severity)
    source_type="server",
    action="raise",
    correlation_key="server:CZ2311004G",
    resource_name="ESX-node-01",
    part_number="P28948-B21",
    description="Server: ESX-node-01\nHealth summary: CRITICAL\nComponents not OK: powerSupplies=CRITICAL",
    resolution=None,
    category="hardware-health",
)
```

**3. What each adapter builds and sends:**

`obm` → `POST` to the OBM Event REST API:

```json
{
  "title": "Server ESX-node-01 health CRITICAL",
  "severity": "critical",
  "lifecycle_state": "open",
  "related_ci": "CZ2311004G",
  "node": "ProLiant DL360 Gen11",
  "mgmt_url": "https://10.0.0.5",
  "time_created": "2025-01-01T10:00:00Z",
  "description": "Server: ESX-node-01\nHealth summary: CRITICAL\nComponents not OK: powerSupplies=CRITICAL",
  "custom_attrs": "",
  "dedup_key": "server:CZ2311004G"
}
```

`servicenow` (default `em_event` table) → `POST /api/now/table/em_event`:

```json
{
  "source": "HPE COM",
  "event_class": "compute-ops-management",
  "resource": "ProLiant DL360 Gen11",
  "node": "CZ2311004G",
  "severity": "1",
  "description": "Server: ESX-node-01\nHealth summary: CRITICAL\nComponents not OK: powerSupplies=CRITICAL",
  "message_key": "server:CZ2311004G",
  "additional_info": ""
}
```

`servicenow` (`SNOW_TABLE=incident`) → `POST /api/now/table/incident`:

```json
{
  "short_description": "Server ESX-node-01 health CRITICAL",
  "description": "Server: ESX-node-01\nHealth summary: CRITICAL\nComponents not OK: powerSupplies=CRITICAL",
  "cmdb_ci": "CZ2311004G",
  "correlation_id": "server:CZ2311004G"
}
```

`opsramp` → `POST /api/v2/tenants/<id>/alerts`:

```json
{
  "serviceName": "HPE COM",
  "device": { "hostName": "CZ2311004G", "resourceName": "CZ2311004G" },
  "currentState": "Critical",
  "alertKey": "server:CZ2311004G",
  "component": "ProLiant DL360 Gen11",
  "subject": "Server ESX-node-01 health CRITICAL",
  "description": "Server: ESX-node-01\nHealth summary: CRITICAL\nComponents not OK: powerSupplies=CRITICAL",
  "app": "HPE COM",
  "alertTime": "2025-01-01T10:00:00Z"
}
```

`halo` → `POST /api/Tickets` (an array of one ticket):

```json
[
  {
    "summary": "Server ESX-node-01 health CRITICAL",
    "details": "COM operation Updated on CZ2311004G (ProLiant DL360 Gen11).\nEvent id: P28948-B21+CZ2311004G\nSeverity: critical\nManagement URL: https://10.0.0.5\nTime: 2025-01-01T10:00:00Z\n\nServer: ESX-node-01\nHealth summary: CRITICAL\nComponents not OK: powerSupplies=CRITICAL",
    "tickettype_id": 1,
    "impact": 1,
    "urgency": 1,
    "thirdpartyref": "server:CZ2311004G"
  }
]
```

`splunk` → `POST` to the HTTP Event Collector:

```json
{
  "source": "hpe-com",
  "sourcetype": "com:event",
  "event": {
    "event_id": "P28948-B21+CZ2311004G",
    "operation": "Updated",
    "action": "raise",
    "title": "Server ESX-node-01 health CRITICAL",
    "severity": "critical",
    "serial": "CZ2311004G",
    "model": "ProLiant DL360 Gen11",
    "mgmt_url": "https://10.0.0.5",
    "time_created": "2025-01-01T10:00:00Z",
    "tags": {},
    "dedup_key": "82b8f550…",
    "correlation_key": "server:CZ2311004G",
    "description": "Server: ESX-node-01\nHealth summary: CRITICAL\nComponents not OK: powerSupplies=CRITICAL",
    "resolution": null
  }
}
```

`webhook` → `POST` the **full** `CanonicalEvent` as JSON (`dataclasses.asdict`,
i.e. every field shown in step 2 plus the original COM payload under `raw`).

**On a clear** (health back to `OK`), the same event flows with
`action="clear"`, the **same** `correlation_key`, and adapters close what they
opened: `obm` sends `severity:"normal"` + `lifecycle_state:"closed"`;
`servicenow` em_event sends `severity:"5"` (Clear); `opsramp` sends
`currentState:"Ok"`; `halo`/`servicenow`-incident look up the open item by
`thirdpartyref`/`correlation_id` and close it; `splunk`/`webhook` deliver the
clear as its own event.

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
