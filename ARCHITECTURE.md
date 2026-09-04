# Architecture — processes & the files that implement them

This is the single reference for **every process (pipeline stage) in the repo and
the file that implements it**. It covers all three components:

- **relay** — public cloud edge (COM → queue), `com-event-relay/relay/`
- **shim** — outbound-only consumer (queue → target), `com-event-relay/shim/`
- **bridge** — single-box all-in-one (COM → target), `com-event-bridge/bridge/`
- **core** — shared logic used by shim + bridge, `com-event-core/com_event_core/`

The single-box **bridge** runs the whole pipeline in one process. The **relay
model** splits that same pipeline across **relay** + **shim**, with a **cloud
queue** as the durable buffer between them. Normalisation, de-duplication, and the
target adapters are shared code (`com-event-core`), so the bridge and the shim run
identical delivery logic.

## What each process does

| # | Process | In one line |
|---|---|---|
| 1 | **Handshake** | Answers COM's ownership check on registration — echoes the `x-compute-ops-mgmt-verification-challenge` token so COM confirms it reached the right endpoint before sending real events. |
| 2 | **Authentication** | Rejects anyone but COM — every event must carry a shared secret in a header, compared in constant time; a wrong/missing secret returns `401` and nothing proceeds. |
| 3 | **Input hardening** | Protects the public endpoint from abuse — caps the request body at `MAX_BODY_BYTES` (default 256 KB) and returns `413` for anything larger, before parsing. |
| 4 | **Enqueue → queue** | (Relay) Captures the raw event into a durable cloud queue and acks COM immediately, so the public edge never talks to the target. |
| 5 | **Dequeue ← queue** | (Shim) Pulls events from the queue over an outbound-only connection — no inbound ports on the target network. |
| 6 | **Normalisation** | Turns the raw COM payload into a neutral `CanonicalEvent` so adapters never see COM's schema; a COM change is fixed here once. |
| 7 | **De-duplication** | Suppresses repeats within a TTL window (keyed on `dedup_key`) so a redelivered/duplicate event doesn't open a second ticket or alert. |
| 8 | **Correlation** | Tags a problem and its later recovery with the same `correlation_key` and flips `action` raise→clear, so the target auto-closes the exact item it opened. |
| 9 | **Target forwarding** | Hands the `CanonicalEvent` to the selected adapter (`TARGET`), which maps it to the target system's API call. |
| 10 | **Retry / redelivery** | (Shim) On a transient failure, `abandon`s the message so the queue redelivers it; malformed input is `dead_letter`ed and never retried. |
| 11 | **On-disk spool** | (Bridge) The queue-less durability path — persists events to local SQLite, acks COM `202`, and a background worker drains + retries. |
| 12 | **Health / readiness** | `/healthz` says the process is up; `/readyz` says it's actually able to work (relay checks the queue backend; bridge checks the spool worker). |

## Process → file map

| # | Process (stage) | Bridge | Relay | Shim | Where it lives |
|---|---|:--:|:--:|:--:|---|
| 1 | **Handshake** — echo COM's `x-compute-ops-mgmt-verification-challenge` | ✅ | ✅ | — | [bridge/app.py](com-event-bridge/bridge/app.py#L78) · [relay/app.py](com-event-relay/relay/app.py#L61) |
| 2 | **Authentication** — shared-secret header, constant-time `hmac.compare_digest` | ✅ | ✅ | — | [bridge/app.py](com-event-bridge/bridge/app.py#L160) · [relay/app.py](com-event-relay/relay/app.py#L102) |
| 3 | **Input hardening** — `MAX_BODY_BYTES` cap → `413` (Content-Length + post-read) | ✅ | ✅ | — | [bridge/app.py](com-event-bridge/bridge/app.py#L166) · [relay/app.py](com-event-relay/relay/app.py#L107) |
| 4 | **Enqueue → queue** — publish raw body + metadata to Service Bus / SQS | — | ✅ | — | [relay/core/queue/base.py](com-event-relay/relay/core/queue/base.py#L19) · [servicebus.py](com-event-relay/relay/core/queue/servicebus.py#L24) · [sqs.py](com-event-relay/relay/core/queue/sqs.py#L29) |
| 5 | **Dequeue ← queue** — consume loop (receive) | — | — | ✅ | [shim/worker.py](com-event-relay/shim/worker.py#L32) · [base.py](com-event-relay/shim/core/queue/base.py#L37) · [servicebus.py](com-event-relay/shim/core/queue/servicebus.py#L31) · [sqs.py](com-event-relay/shim/core/queue/sqs.py#L35) |
| 6 | **Normalisation** — COM payload → `CanonicalEvent` | ✅ | — | ✅ | [core/normalize.py](com-event-core/com_event_core/normalize.py#L128) |
| 7 | **De-duplication** — SQLite TTL store, keyed on `dedup_key` | ✅ | — | ✅ | [core/dedup.py](com-event-core/com_event_core/dedup.py#L23) |
| 8 | **Correlation** — stable `correlation_key` + `action` (raise/clear) | ✅ | id stamp only | ✅ | correlation/action set in [core/normalize.py](com-event-core/com_event_core/normalize.py#L128); relay tracing id in [relay/app.py](com-event-relay/relay/app.py#L116) |
| 9 | **Target forwarding** — `get_adapter()` + `adapter.forward()` | ✅ | — | ✅ | [core/adapters/](com-event-core/com_event_core/adapters/__init__.py#L24); called in [bridge/app.py](com-event-bridge/bridge/app.py#L201) · [shim/worker.py](com-event-relay/shim/worker.py#L59) |
| 10 | **Retry / redelivery** — queue `abandon` (transient) / `dead_letter` (malformed) | via spool | — | ✅ | [shim/worker.py](com-event-relay/shim/worker.py#L63) · [shim/core/queue/base.py](com-event-relay/shim/core/queue/base.py#L45) |
| 11 | **On-disk spool** — durable buffer + background drain worker | ✅ | — | — | [bridge/core/spool.py](com-event-bridge/bridge/core/spool.py#L48) (`SpoolStore`) + [SpoolWorker](com-event-bridge/bridge/core/spool.py#L118) |
| 12 | **Health / readiness** — `/healthz`, `/readyz` | ✅ | ✅ | — (no HTTP) | [bridge/app.py](com-event-bridge/bridge/app.py#L129) · [relay/app.py](com-event-relay/relay/app.py#L69) |

> **Durable buffer:** the bridge uses the on-disk **spool** (#11); the relay model
> uses the **cloud queue** between relay (#4) and shim (#5) instead.

## Shared core (`com-event-core`)

The stages that are **identical** in the bridge and the shim live once here and
are imported by both — so a COM schema change or an adapter fix is made in one
place:

| Module | Provides | File |
|---|---|---|
| `normalize` | `CanonicalEvent` + `normalize()` (raise/clear + correlation) | [normalize.py](com-event-core/com_event_core/normalize.py) |
| `dedup` | `DedupStore.is_duplicate()` (SQLite TTL) | [dedup.py](com-event-core/com_event_core/dedup.py) |
| `adapters` | `get_adapter()` + the 6 target adapters (`obm`, `servicenow`, `opsramp`, `halo`, `splunk`, `webhook`) | [adapters/](com-event-core/com_event_core/adapters) |

## Per-component entry points

| Component | Entry point | Role |
|---|---|---|
| relay | [com-event-relay/relay/app.py](com-event-relay/relay/app.py) | FastAPI public receiver → enqueue |
| shim | [com-event-relay/shim/worker.py](com-event-relay/shim/worker.py) | queue consumer → normalise/dedup/forward |
| bridge | [com-event-bridge/bridge/app.py](com-event-bridge/bridge/app.py) | FastAPI receiver → sync forward or spool |

For deployment topology and the deployment-mode decision (`sync` vs `spool`,
cloud vs on-prem), see the [root README](README.md#which-project-do-i-use) and the
[com-event-relay README](com-event-relay/README.md#choosing-a-deployment-model).
