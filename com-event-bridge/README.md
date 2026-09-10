# COM Event Bridge

A single-box, on-premises webhook bridge for **HPE Compute Ops Management (COM)** that receives, authenticates, normalises, de-duplicates, and forwards COM events directly to ITSM, ITOM, SIEM, ChatOps, incident-response, and observability platforms — with **no managed cloud dependency**.

Use this deployment model when you can expose a public HTTPS endpoint that COM can reach and want the smallest infrastructure footprint.

> **Reference implementation**
>
> This is an open-source reference/sample implementation. Review, validate, and harden it for your own environment before production use.

---

## At a glance

<img src="../docs/images/com-event-bridge-architecture.png" alt="COM Event Bridge architecture" width="800" />

The Bridge is the **single-box alternative** to [`com-event-relay`](../com-event-relay/).

| | `com-event-relay` | `com-event-bridge` |
|---|---|---|
| **Topology** | Cloud relay + durable queue + on-prem shim | One on-prem host |
| **Cloud footprint** | Azure or AWS required | None |
| **Inbound exposure** | None into customer network | Public HTTPS endpoint required |
| **Durability** | Managed cloud queue | Local spool |
| **Best for** | No inbound path + cloud durability | No-cloud mandate + simplicity |

> If COM already provides a native integration for your target and that native path meets the requirement, use it. Use the Bridge when you need additional transformation, buffering, de-duplication, fan-out, lifecycle correlation, or support for a target without native COM integration.

---

## Contents

- [What it does](#what-it-does)
- [When to use the Bridge](#when-to-use-the-bridge)
- [When not to use the Bridge](#when-not-to-use-the-bridge)
- [Delivery modes](#delivery-modes)
- [Persistence and durability](#persistence-and-durability)
- [Endpoints](#endpoints)
- [Public edge and TLS](#public-edge-and-tls)
- [High availability considerations](#high-availability-considerations)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Target adapters](#target-adapters)
- [Secrets management](#secrets-management)
- [Register the webhook in COM](#register-the-webhook-in-com)
- [Production checklist](#production-checklist)
- [Project layout](#project-layout)
- [Relationship to com-event-core](#relationship-to-com-event-core)
- [Relationship to com-event-relay](#relationship-to-com-event-relay)

---

# What it does

The Bridge folds the complete receive-and-deliver pipeline into one deployment:

```text
COM
 |
 | HTTPS 443
 v
TLS reverse proxy
 |
 v
Bridge
 |
 +--> handshake
 +--> shared-secret authentication
 +--> input validation
 +--> normalize to CanonicalEvent
 +--> de-duplicate
 +--> correlate raise / clear
 +--> spool or deliver
 |
 v
Target adapter(s)
 |
 v
Target platform(s)
```

The Bridge performs six main functions:

1. **Verification handshake**  
   Answers COM's verification `GET` by echoing the `x-compute-ops-mgmt-verification-challenge` value as:

   ```json
   {"verification":"<token>"}
   ```

2. **Authentication**  
   Every event must carry the configured shared-secret header. The default header is:

   ```text
   x-shim-secret
   ```

   A missing or invalid secret returns `401`.

3. **Input hardening**  
   Request bodies are capped at `MAX_BODY_BYTES` and malformed JSON is rejected before processing.

4. **Normalisation**  
   The COM payload is converted to the shared `CanonicalEvent` model from [`com-event-core`](../com-event-core/).

5. **De-duplication and lifecycle correlation**  
   Repeated events are suppressed and raise/clear events use stable correlation identities.

6. **Target delivery**  
   One or more adapters selected through `TARGETS` deliver the event to the destination platform.

Detailed event semantics, COM webhook filters, `SERVER_MONITORS`, correlation keys, and adapter behavior are documented centrally in:

[`com-event-core/README.md`](../com-event-core/README.md)

---

# When to use the Bridge

Use `com-event-bridge` when:

- a **no-cloud** deployment is required
- you can expose a public HTTPS endpoint that COM can reach
- you want a single deployment rather than Relay + Queue + Shim
- a local durable spool is sufficient
- you want to add transformation, de-duplication, correlation, or fan-out between COM and the target
- the target does not provide a native COM integration

Typical topology:

```text
Internet
   |
   v
Public DNS + TCP/443
   |
   v
TLS reverse proxy
   |
   v
Bridge
   |
   v
Private target
```

---

# When not to use the Bridge

Prefer [`com-event-relay`](../com-event-relay/) when:

- inbound HTTPS cannot be opened into the customer environment
- the public edge should be hosted as a managed cloud service
- durable queue storage outside the Bridge host is preferred
- receive and delivery should scale independently
- you need a stronger failure boundary between public webhook reception and internal delivery

Prefer a **native COM integration** when:

- COM already supports the target natively
- that integration meets the functional requirement
- you do not need extra transformation, buffering, fan-out, or raise/clear correlation

---

# Delivery modes

The Bridge supports two delivery modes through:

```bash
DELIVERY_MODE=spool
```

or:

```bash
DELIVERY_MODE=sync
```

> **For production use, prefer `spool`.**
>
> `sync` is intended for local smoke tests or environments where occasional event loss is acceptable.

---

## `spool` mode — default and recommended

```text
COM
 |
 v
Bridge
 |
 +--> persist event
 |
 v
Local spool
 |
 +--> background worker
 |
 v
Target
```

Behavior:

1. validate and normalise the event
2. write it to the local SQLite spool
3. return `202` to COM
4. deliver from the background worker
5. retry failed deliveries with capped exponential backoff

Benefits:

- target outages do not immediately lose accepted events
- pending events survive a Bridge process restart
- multi-target partial failures can be retried safely
- no cloud queue is required

Trade-off:

- the Bridge host's disk becomes part of the durability model

---

## `sync` mode — best effort

```text
COM
 |
 v
Bridge
 |
 v
Target
```

The Bridge forwards inline.

If the target fails:

```text
Bridge -> 503
```

but COM does **not** retry the webhook delivery, so the event is lost.

Use `sync` only where:

- this behavior is acceptable
- you are smoke-testing
- the target is non-critical

---

# Persistence and durability

## Persistent state

Treat `/data` as part of the Bridge's durable state.

Typical container layout:

```text
/data
 ├── spool.db
 └── dedup.db
```

The container image defaults to:

```text
SPOOL_PATH=/data/spool.db
DEDUP_DB_PATH=/data/dedup.db
```

A persistent volume must therefore be mounted at `/data`.

---

## Spool prerequisite

In `spool` mode, `SPOOL_PATH` must point to durable storage.

The Bridge refuses to start if spool mode is enabled without a configured persistent spool path.

This prevents false durability such as:

```text
./spool.db
```

inside an ephemeral container filesystem.

Recommended examples:

### Docker / Compose

```text
/data/spool.db
```

with a named volume:

```bash
-v bridge-data:/data
```

### Bare metal / systemd

```text
/var/lib/com-event-bridge/spool.db
```

on persistent storage.

---

## Spool capacity and event loss

The backlog is capped by:

```text
SPOOL_MAX_BYTES
```

When the spool reaches that limit, the Bridge returns:

```text
503
```

for new events.

> **Important**
>
> This `503` protects the Bridge host from disk exhaustion, but it does **not** preserve the incoming COM event. COM does not retry failed webhook deliveries.
>
> Once the spool is full, new events are intentionally dropped.

This means spool capacity and backlog growth should be monitored in production.

---

## Multi-target retries

In spool mode, delivery state is tracked per adapter.

Example:

```text
ServiceNow -> success
Splunk     -> success
PagerDuty  -> failure
```

On retry:

```text
ServiceNow -> skipped
Splunk     -> skipped
PagerDuty  -> retried
```

This avoids duplicating successful target deliveries.

The shared delivery semantics are implemented in [`com-event-core`](../com-event-core/).

---

# Endpoints

| Request | Response |
|---|---|
| Handshake with `x-compute-ops-mgmt-verification-challenge` | `200` + `{"verification": "<token>"}` |
| Valid `POST`, `sync` delivered | `202` + `x-bridge-event-id` |
| Valid `POST`, `spool` accepted | `202` + `x-bridge-event-id` |
| Duplicate event | `202` — accepted, then suppressed by de-duplication at delivery |
| `GET` without the challenge header | `400` |
| Missing / invalid shared secret | `401` |
| Body over `MAX_BODY_BYTES` | `413` |
| Malformed JSON | `400` |
| `sync`, target unavailable | `503` — event lost |
| `spool`, backlog full | `503` — event dropped |
| `GET /healthz` | `200` |
| `GET /readyz` | `200` ready / `503` if spool worker is unhealthy |

---

# Public edge and TLS

The Bridge is intended to run **behind a TLS reverse proxy**.

Recommended trust boundary:

```text
Internet
   |
   v
nginx / Caddy
   |
   | private container network
   v
Bridge :8080
   |
   v
Target
```

The Bridge application itself does not need to terminate public TLS directly.

The reverse proxy should own:

- TCP/443
- CA-signed TLS certificate
- public hostname
- TLS policy
- optional source-IP restrictions
- request forwarding to the Bridge's private port

Unlike `com-event-relay`, the public edge is operated by you.

You own:

- public DNS
- inbound TCP/443 connectivity
- reverse proxy configuration
- certificate issuance and renewal
- host patching
- persistent storage
- monitoring
- backup/recovery strategy
- HA if required

See:

[`HARDENING.md`](HARDENING.md)

and:

[`deploy/nginx/`](deploy/nginx/)

for deployment guidance.

---

# High availability considerations

The default Bridge deployment is **stateful and single-instance**.

Its default local state includes:

- SQLite spool
- SQLite de-duplication database

Running multiple independent Bridge replicas does **not** automatically create a safe HA design.

Without shared state or an external coordination layer, multiple replicas can introduce:

- inconsistent spool ownership
- duplicate processing
- inconsistent de-duplication state
- race conditions during failover

Therefore:

> Treat the Bridge as a single-instance deployment unless you deliberately add an external HA and shared-state design.

If high availability across host failure is a primary requirement, the Relay + managed queue architecture is often the better fit.

---

# Quick start

> For the full end-to-end runbook, including DNS, TLS/certificate bootstrap, COM raise/clear webhooks, and a live target test, see:
>
> [`docs/Deploy-End-to-End-On-Prem.md`](docs/Deploy-End-to-End-On-Prem.md)

---

## Run locally in sync mode

The default mode is `spool`, which requires persistent storage.

For a quick local handshake/auth smoke test, use best-effort `sync` mode:

```bash
cd bridge

cp .env.example .env
# Set:
#   COM_SHARED_SECRET
#   TARGETS
#   target credentials
#
# For local smoke testing only:
#   DELIVERY_MODE=sync

pip install -e ../../com-event-core
pip install -r requirements.txt

DELIVERY_MODE=sync uvicorn app:app --host 0.0.0.0 --port 8080
```

Handshake test:

```bash
curl -i localhost:8080/com/webhook \
  -H "x-compute-ops-mgmt-verification-challenge: abc123"
```

Bad-secret test:

```bash
curl -i -X POST localhost:8080/com/webhook -d '{}'
```

Expected result:

```text
401
```

---

## Run the container in spool mode

Build from the repository root so the image includes `com-event-core`:

```bash
docker build \
  -f com-event-bridge/bridge/Dockerfile \
  -t com-event-bridge:local \
  .
```

Create the persistent volume:

```bash
docker volume create bridge-data
```

Run:

```bash
docker run -d \
  --name com-event-bridge \
  -p 8080:8080 \
  -v bridge-data:/data \
  --env-file com-event-bridge/bridge/.env \
  com-event-bridge:local
```

The important part is:

```bash
-v bridge-data:/data
```

because `/data` contains the spool and de-duplication databases.

A host path also works:

```bash
-v /srv/com-event-bridge:/data
```

Make sure the directory is writable by UID `10001`, the non-root user in the image.

> This starts the Bridge application only. It does **not** provide public TLS. Put a TLS reverse proxy in front before registering it with COM.

---

## Run the full DMZ stack

The repository includes a Compose stack with:

- Bridge
- nginx
- certbot
- persistent Bridge data volume

Edit the server name in:

```text
deploy/nginx/com-event-bridge.conf
```

Bootstrap the certificate as described in [`HARDENING.md`](HARDENING.md), then:

```bash
cp bridge/.env.example bridge/.env
# Set COM_SHARED_SECRET + TARGETS + target credentials

docker compose up -d --build
```

The Compose stack mounts `bridge-data` at `/data`, so spool and de-duplication state survive container restarts.

---

# Configuration

The Bridge README documents **Bridge-owned settings**.

Target-specific adapter credentials and mappings are documented centrally in:

[`../com-event-core/README.md`](../com-event-core/README.md)

## Bridge settings

| Variable | Required | Default | Notes |
|---|---|---|---|
| `COM_SHARED_SECRET` | Yes | — | Secret COM sends with every event |
| `SHARED_SECRET_HEADER` | No | `x-shim-secret` | Header carrying the shared secret |
| `MAX_BODY_BYTES` | No | `262144` | Maximum request body (256 KB) |
| `DELIVERY_MODE` | No | `spool` | `spool` (durable) or `sync` (best-effort) |
| `SPOOL_PATH` | Yes in spool mode | `/data/spool.db` | Persistent SQLite spool path (container default; must be on durable storage) |
| `SPOOL_MAX_BYTES` | No | `52428800` | Maximum local spool backlog (50 MB); over this → `503` backpressure |
| `SPOOL_RETRY_SECONDS` | No | `30` | Initial retry delay (grows exponentially per attempt) |
| `SPOOL_RETRY_CAP` | No | `3600` | Maximum retry delay (1 hour) |
| `SPOOL_POLL_SECONDS` | No | `2` | How often the background worker checks for due spooled events |
| `TARGETS` | No | `webhook` | One or more adapter names, comma-separated |
| `TARGET_TIMEOUT` | No | `15` | Per-target HTTP timeout, in seconds |
| `SERVER_MONITORS` | No | `health` | COM server conditions to monitor. See Core README |
| `DEDUP_DB_PATH` | No | `/data/dedup.db` | SQLite de-duplication database (container default) |
| `DEDUP_TTL_SECONDS` | No | `3600` | De-duplication window, in seconds; `0` disables |

> **`SPOOL_POLL_SECONDS` does not delay event receipt.** The Bridge's primary
> path is **not polled** — it receives COM webhooks directly over HTTPS, so an
> incoming event is processed the instant it arrives. In `sync` mode it is
> delivered inline; in `spool` mode it is written to the local spool immediately
> and acknowledged to COM (`202`). `SPOOL_POLL_SECONDS` only governs the
> **background retry drain** of already-spooled events — i.e. how often the
> worker wakes to re-attempt items still pending (for example after a target
> outage). The default `2` therefore adds at most a ~2 s delay to a *retry*, not
> to first receipt.

Example:

```bash
TARGETS=servicenow
```

or:

```bash
TARGETS=servicenow,splunk
```

or:

```bash
TARGETS=sentinel,pagerduty,teams
```

For the current adapter list and required target credentials, see:

[`com-event-core — Supported adapters`](../com-event-core/README.md#supported-adapters)

---

# Target adapters

The Bridge does not implement target adapters itself. All target adapters are implemented once in [`com-event-core`](../com-event-core/) and loaded at runtime, so adapter behavior is identical to the Relay + Shim model.

## Supported platforms

For the full list of supported platforms and their per-adapter details — category, role, authentication, whether COM offers a native integration, how a clear is delivered, and validation status — see the [supported-adapters table in `com-event-core`](../com-event-core/README.md#supported-adapters), the single source of truth.

**Target not listed?** You have two options:

- **Use the generic `webhook` adapter** to POST the `CanonicalEvent` as JSON to any HTTP endpoint — no code required.
- **Add your own adapter** if the target needs a specific API or payload shape — see [adding a new target](../com-event-core/README.md#adding-a-new-target).

## Selecting one or more targets

The Bridge selects adapters with the `TARGETS` environment variable. Use one name, or several comma-separated to fan one COM event out to each target:

```bash
TARGETS=servicenow
TARGETS=servicenow,splunk
```

Each adapter also reads its own connection settings (URLs, credentials, tokens)
from environment variables or mounted secret files. To learn exactly which vars a
specific target needs, follow the four steps in
[Finding an adapter's environment variables](../com-event-core/README.md#finding-an-adapters-environment-variables)
— in short: find your adapter's `# --- Target: <Name> (<name>) ---` block in
[`bridge/.env.example`](bridge/.env.example) (required/optional/secret are flagged
in the comments), or read the adapter's own file in
[`com_event_core/adapters/`](../com-event-core/com_event_core/adapters/).

---

# Secrets management

Sensitive values should not be stored in a plaintext `.env` in production.

The Bridge and shared adapters support two forms for a secret named:

```text
NAME
```

Resolution order:

1. `NAME_FILE`
2. `NAME`
3. otherwise startup fails

Example:

```text
COM_SHARED_SECRET_FILE=/run/secrets/com_shared_secret
```

This allows the secret value itself to stay outside the process environment.

---

## Docker Compose / Swarm secrets

Example:

```yaml
services:
  bridge:
    environment:
      COM_SHARED_SECRET_FILE: /run/secrets/com_shared_secret
    secrets:
      - com_shared_secret

secrets:
  com_shared_secret:
    file: ./secrets/com_shared_secret.txt
```

Use restrictive permissions on source secret files and keep them out of Git.

---

## systemd `LoadCredential`

For a bare-metal deployment:

```ini
LoadCredential=com_shared_secret:/etc/com-event-bridge/com_shared_secret
Environment=COM_SHARED_SECRET_FILE=%d/com_shared_secret
```

For encrypted credentials, consider:

```text
LoadCredentialEncrypted=
```

with `systemd-creds`.

---

## HashiCorp Vault

If Vault is already available in the environment, a Vault Agent can render secrets to files and the Bridge can consume them through `*_FILE`.

Example:

```text
COM_SHARED_SECRET_FILE=/run/com-bridge/com_shared_secret
```

The same pattern can be used with Kubernetes secret projections and CSI drivers.

---

# Register the webhook in COM

Point the COM webhook at the public Bridge URL.

Example:

```text
destination = https://com-bridge.example.com/com/webhook
```

Configure the shared-secret header:

```text
x-shim-secret: <COM_SHARED_SECRET>
```

and the desired:

```text
eventFilter
```

COM performs the verification handshake first.

Once the Bridge echoes the verification token correctly, the webhook can become active.

For detailed server-health, power, connection, subscription, alert, raise, and clear filter examples, see:

[`com-event-core — COM webhook filters and raise / clear lifecycle`](../com-event-core/README.md#com-webhook-filters-and-raise--clear-lifecycle)

---

# Production checklist

Before using the Bridge for production delivery, confirm:

- [ ] public DNS resolves to the reverse proxy
- [ ] TCP/443 is reachable from COM
- [ ] a valid CA-signed certificate is installed
- [ ] certificate renewal is automated
- [ ] `COM_SHARED_SECRET` is configured
- [ ] secrets use `*_FILE` or another secret-management mechanism
- [ ] `DELIVERY_MODE=spool`
- [ ] `/data` is mounted on persistent storage
- [ ] `SPOOL_PATH` is durable
- [ ] `DEDUP_DB_PATH` is durable
- [ ] spool capacity is monitored
- [ ] `/healthz` is monitored
- [ ] `/readyz` is monitored
- [ ] raise and clear are tested end-to-end
- [ ] target outage and recovery are tested
- [ ] host patching and hardening are defined
- [ ] backup / recovery expectations for Bridge state are understood
- [ ] HA requirements have been assessed

---

# Project layout

```text
com-event-bridge/
├── bridge/
│   ├── app.py
│   ├── core/
│   │   └── spool.py
│   ├── Dockerfile
│   ├── requirements.txt
│   └── .env.example
│
├── deploy/
│   ├── nginx/
│   │   └── com-event-bridge.conf
│   └── systemd/
│       └── com-event-bridge.service
│
├── docs/
│   └── Deploy-End-to-End-On-Prem.md
│
├── docker-compose.yml
├── HARDENING.md
└── README.md
```

Bridge-specific code is intentionally small:

- `bridge/app.py` — webhook handshake, authentication, request handling, and delivery orchestration
- `bridge/core/spool.py` — local durable spool and retry worker

Shared event-processing logic lives in `com-event-core`.

---

# Relationship to com-event-core

[`com-event-core`](../com-event-core/) contains:

- COM normalisation
- `CanonicalEvent`
- de-duplication
- correlation
- server-condition handling
- adapter registry
- target adapters
- multi-target delivery logic
- secret helpers

The Bridge imports this package rather than copying the logic.

Therefore:

```text
adapter fix
   |
   v
com-event-core
   |
   +--> com-event-bridge
   |
   +--> com-event-relay shim
```

A new target adapter added to `com-event-core` becomes available to both deployment models.

---

# Relationship to com-event-relay

Both projects solve the same functional problem but use different failure and network boundaries.

```text
Bridge
------
COM -> public customer endpoint -> local spool -> target
```

```text
Relay
-----
COM -> managed cloud endpoint -> cloud queue -> outbound-only shim -> target
```

Choose **Bridge** when:

- no managed cloud should be used
- public HTTPS ingress is acceptable
- local host durability is sufficient
- simplicity is the priority

Choose **Relay + Shim** when:

- no inbound connection into the customer network is allowed
- managed cloud ingress is preferred
- cloud-queue durability is preferred
- the public receiver and internal delivery path should be decoupled
