# com-event-core

Shared building blocks for the HPE Compute Ops Management (COM) event
integrations — the single source of truth used by both:

- [com-event-relay](https://github.com/jullienl/com-event-relay) — cloud relay +
  outbound-only on-prem shim (durable queue in the middle);
- [com-event-bridge](https://github.com/jullienl/com-event-bridge) — single-box
  on-prem bridge (no cloud, no queue).

> AI-generated reference implementation. Review and harden before production use.

## Contents

- [Why this package exists](#why-this-package-exists)
- [What's in it](#whats-in-it)
- [Usage](#usage)
- [Example: one COM event across every adapter](#example-one-com-event-across-every-adapter)
- [Install](#install)
- [Adding a new target](#adding-a-new-target)
- [Versioning](#versioning)

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
| `com_event_core.adapters` | `get_adapters()` + `TargetAdapter` and the built-in targets: `obm`, `servicenow`, `opsramp`, `halo`, `splunk`, `github`, `slack`, `teams`, `jira`, `pagerduty`, `sentinel`, `datadog`, `elastic`, `bmc_helix`, `dynatrace`, `grafana`, `webhook`. |
| `com_event_core.deliver` | `deliver(event, adapters, dedup)` — fan out one event to one or many adapters, with per-adapter de-dup and partial-failure retry. |
| `com_event_core.secrets` | `get_secret(name)` — resolve a sensitive value from `<name>_FILE` (a vault/CSI/Docker/systemd-projected file) or the environment, so credentials can stay out of `.env`. |

Selection is by the `TARGETS` env var — one name, or a comma-separated list for
fan-out; each adapter reads its own target credentials from the environment (see
each consumer's `.env.example`). Every sensitive value can alternatively be read
from a file via `get_secret()` — see each consumer's **Secrets management** section.

## Usage

```python
from com_event_core import normalize, DedupStore, deliver, get_adapters

adapters = get_adapters()          # one or many, from TARGETS
dedup = DedupStore()               # SQLite TTL store

event = normalize(com_payload)     # dict -> CanonicalEvent
deliver(event, adapters, dedup)    # fan out; raises PartialDeliveryError so the
                                   # caller retries only the failed target(s)
```

> **Why `deliver()` and not `adapter.forward()` directly?** `deliver()` fans the
> event out to every selected adapter, de-duplicates **per adapter**, and marks a
> target done **only after** its `forward()` succeeds. If some targets fail it
> raises `PartialDeliveryError`, so the caller (spool worker / queue consumer)
> retries the event and only the failed targets are re-attempted — no duplicate
> tickets, no lost events.

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
    correlation_key="server:CZ2311004G:health",
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
  "dedup_key": "server:CZ2311004G:health"
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
  "message_key": "server:CZ2311004G:health",
  "additional_info": ""
}
```

`servicenow` (`SNOW_TABLE=incident`) → `POST /api/now/table/incident`:

```json
{
  "short_description": "Server ESX-node-01 health CRITICAL",
  "description": "Server: ESX-node-01\nHealth summary: CRITICAL\nComponents not OK: powerSupplies=CRITICAL",
  "cmdb_ci": "CZ2311004G",
  "correlation_id": "server:CZ2311004G:health"
}
```

`opsramp` → `POST /api/v2/tenants/<id>/alerts`:

```json
{
  "serviceName": "HPE COM",
  "device": { "hostName": "CZ2311004G", "resourceName": "CZ2311004G" },
  "currentState": "Critical",
  "alertKey": "server:CZ2311004G:health",
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
    "thirdpartyref": "server:CZ2311004G:health"
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
    "correlation_key": "server:CZ2311004G:health",
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
`thirdpartyref`/`correlation_id` and close it; `github` finds the open issue by
its `com:<key>` label and closes it; `jira` finds the open issue by its
`com-<key>` label and runs a close transition; `bmc_helix` finds the open incident
by its `[COM:<key>]` marker and sets the resolved status; `pagerduty` sends
`resolve` on the same `dedup_key`; `datadog` posts a `success` event on the same
`aggregation_key`; `dynatrace` posts a `CUSTOM_INFO` recovery on the same
`com.correlation_key`;
`splunk`/`slack`/`teams`/`sentinel`/`elastic`/`grafana`/`webhook` deliver the clear
as its own event/record/message/log line.

### Server conditions (multi-attribute monitoring)

A COM `.../server` webhook is a **full-state snapshot**, not a "field X changed"
delta. `normalize()` therefore returns a **list** of `CanonicalEvent`s: it
evaluates each *condition* enabled by the `SERVER_MONITORS` env var and emits one
event per condition, each with its own `correlation_key`
(`server:<serial>:<condition>`) so one condition's recovery never closes
another's item.

| `SERVER_MONITORS` | Problem (raise) when | Recovery (clear) when | Severity |
|---|---|---|---|
| `health` *(default)* | `hardware.health.summary` ≠ `OK` | back to `OK` | mapped from health |
| `power` | `hardware.powerState` = `OFF` | `ON` | warning |
| `connection` | `state.connected` = `false` | `true` | major |
| `subscription` | `state.subscriptionState` ≠ `SUBSCRIBED` or `subscriptionExpiresAt` in the past | subscribed & not expired | minor |

Default is `health`. Enable several comma-separated to fan
out — e.g. `SERVER_MONITORS=health,power,connection` opens/closes an independent
item per condition. A condition you **don't** list is simply **not monitored**:
no event is emitted and no item opens or closes for it (no error) — e.g. without
`power`, a powered-off server never opens an item and powering back on never
closes one. Because snapshots are stateless, a healthy condition emits a
`clear` on every delivery; dedup suppresses the repeats and the adapter close is a
no-op when nothing is open. `alert` and generic payloads still yield a single
event. Consumers deliver the batch via `deliver_events()`.

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

An adapter is the **only** target-specific code in the pipeline — everything else
(handshake, auth, normalise, dedup, queue/spool, retry, logging) is shared and
already done. Adding a target is usually one small file plus a one-line
registration.

### Step by step

**1. Create the adapter module** — `com_event_core/adapters/<name>.py`. Subclass
`TargetAdapter`, read config from env vars in `__init__`, and map the
`CanonicalEvent` to the target's API in `forward()`:

```python
"""<Name> adapter — maps a CanonicalEvent to <target>'s API."""
from __future__ import annotations

import logging
import os

import httpx

from com_event_core.normalize import CanonicalEvent, ACTION_CLEAR
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.mytool")


class MyToolAdapter(TargetAdapter):
    name = "mytool"                       # the TARGET value that selects this adapter

    def __init__(self) -> None:
        # Read + validate config once, at startup. Use os.environ[...] for
        # REQUIRED vars (fail fast) and .get(...) for optional ones.
        self._url = os.environ["MYTOOL_URL"]
        self._token = os.environ["MYTOOL_TOKEN"]
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))

    def forward(self, event: CanonicalEvent) -> None:
        # Map the canonical fields to the target's payload.
        payload = {
            "summary": event.title,
            "severity": event.severity,          # canonical scale: normal/warning/minor/major/critical
            "node": event.resource_serial,
            "dedupKey": event.correlation_key,   # so a later clear can resolve it
            "state": "resolved" if event.action == ACTION_CLEAR else "active",
            "detail": event.description,
        }
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(self._url, json=payload,
                            headers={"Authorization": f"Bearer {self._token}"})
            r.raise_for_status()             # MUST raise on failure — caller retries
        log.info("event %s forwarded to mytool", event.event_id)
```

**2. Register it** in the `_ADAPTERS` table in
`com_event_core/adapters/__init__.py` — `TARGETS` name → `(module, class)`:

```python
_ADAPTERS = {
    # ...existing entries...
    "mytool": ("com_event_core.adapters.mytool", "MyToolAdapter"),
}
```

That's all the wiring — both the relay shim and the bridge instantiate it
automatically via `TARGETS=mytool` (imports are lazy, so only the selected
target's dependencies/config are required).

**3. Handle raise vs clear.** Every event has `event.action` (`"raise"` /
`"clear"`, constants `ACTION_RAISE` / `ACTION_CLEAR`) and a stable
`event.correlation_key`. To auto-close on recovery, use the `correlation_key` as
the target's dedup/alert key and, on `clear`, resolve/close instead of opening a
new item. If the target has no close concept (e.g. a log sink), just deliver the
clear as its own event.

**4. Contract to respect** (from [`TargetAdapter`](com_event_core/adapters/base.py)):
- Set a unique `name`.
- `forward(event)` **must raise on failure** so the shared retry (queue `abandon`
  / bridge spool) kicks in — never swallow errors (that's silent data loss).
- Keep it idempotent-friendly: the same event may be redelivered; using
  `correlation_key` as the target key makes repeats update rather than duplicate.
- Optionally override `health()` for a readiness check.

**5. Test locally** with the simplest consumer — the bridge in `sync` mode:

```bash
cd com-event-bridge/bridge
pip install -e ../../com-event-core
TARGETS=mytool MYTOOL_URL=... MYTOOL_TOKEN=... DELIVERY_MODE=sync \
  COM_SHARED_SECRET=dev uvicorn app:app --port 8080
# then POST a sample COM payload (see com-event-core "Example" section / examples/)
```

**6. Document it** — add a row to the **Targets supported** table in the
[root README](../README.md#targets-supported) and list the target's env vars.

> **Canonical fields available** on `event` (see
> [`CanonicalEvent`](com_event_core/normalize.py)): `title`, `severity`,
> `resource_serial`, `resource_model`, `resource_name`, `part_number`,
> `description`, `resolution`, `category`, `mgmt_url`, `time_created`, `tags`,
> `action`, `correlation_key`, `source_type`, `event_id`, and `raw` (the original
> COM payload if you need a field the canonical model doesn't expose).

The simplest working example to copy is
[`webhook.py`](com_event_core/adapters/webhook.py) (~40 lines).

## Versioning

Semantic versioning. A change to a mapping or adapter is a **minor** bump; a
breaking change to `CanonicalEvent` or an adapter's env contract is a **major**
bump. Consumers pin a compatible range (e.g. `com-event-core>=0.1,<0.2`).
