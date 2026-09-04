# COM Event Bridge

A **single-box, on-premises** bridge that lets HPE Compute Ops Management (COM)
deliver webhook events straight to a target system — **no cloud, no queue, one
container**.

> AI-generated reference implementation. Review and harden before production use.

This is the companion to [com-event-relay](../com-event-relay). Both solve the
same problem — getting COM webhook events into a system COM can't reach natively
(OBM, ServiceNow Event Management, Splunk, a generic webhook) — but with a
different trade-off:

| | com-event-relay | **com-event-bridge (this project)** |
|---|---|---|
| Topology | Cloud relay + on-prem shim + queue | **One on-prem box** |
| Cloud footprint | Required (Azure/AWS) | **None** |
| Inbound exposure | None (shim is outbound-only) | You host the public endpoint |
| Durability | Durable cloud queue | Local spool (optional) or COM retries |
| Best for | Zero inbound exposure + durability | Strict no-cloud mandate, simplicity |

**If your target is OpsRamp or ServiceNow incident creation, use neither** — COM
has a native integration for those.

## What it does

One process folds the whole path together:

```
COM ──443──►  handshake ─► auth ─► normalize ─► deliver ──►  target
              └────────── com-event-bridge (one box) ──────┘
              (public DMZ HTTPS via a TLS reverse proxy in front)
```

1. **Verification handshake.** Answers COM's `GET` with the
   `x-compute-ops-mgmt-verification-challenge` header by echoing
   `{"verification":"<token>"}` — so COM accepts the endpoint.
2. **Authentication.** Every event must carry a shared secret header
   (default `x-shim-secret`), compared in **constant time**; a bad/missing secret
   returns `401`.
3. **Input hardening.** Bodies are capped at `MAX_BODY_BYTES` (default 256 KB,
   `413` over that); malformed JSON is rejected with `400`.
4. **Normalise.** The COM payload becomes a neutral `CanonicalEvent` (same model
   and mapping as com-event-relay, so target behaviour is identical).
5. **Deliver** via the selected `TARGET` adapter (`obm` / `servicenow` / `opsramp` / `halo` / `splunk`
   / `webhook`) — see [Delivery modes](#delivery-modes).
6. **De-duplicate.** A local SQLite TTL store suppresses repeats (COM retries).

## Delivery modes

Because there is no queue, you choose how delivery is guaranteed with
`DELIVERY_MODE`:

| Mode | Behaviour | Trade-off |
|---|---|---|
| **`sync`** (default) | Forward inline. If the target fails, return `503` so **COM retries**. | Simplest; delivery bounded by COM's retry window. |
| **`spool`** | Persist the event to a local on-disk spool (SQLite), ack COM with `202` immediately, and drain it in a **background worker** that retries with capped exponential backoff. | Survives target outages without a cloud queue; the box's disk is the buffer. |

In `spool` mode the backlog is capped at `SPOOL_MAX_BYTES`; once exceeded, new
events get `503` (COM holds and retries) so a prolonged outage can't fill the
disk. The spool is crash-safe — unsent events survive a restart.

## Endpoints

| Request | Response |
|---|---|
| Handshake (`x-compute-ops-mgmt-verification-challenge`) | `200` + `{"verification":"<token>"}` |
| `POST` with valid secret — `sync` delivered | `202` (+ `x-bridge-event-id`) |
| `POST` with valid secret — `spool` accepted | `202` (+ `x-bridge-event-id`) |
| `POST`, duplicate (dedup) | `200` |
| `POST` missing/invalid secret | `401` |
| `POST` body over `MAX_BODY_BYTES` | `413` |
| `POST` malformed JSON | `400` |
| `POST`, `sync`, target unavailable | `503` (COM retries) |
| `POST`, `spool`, backlog full | `503` (COM retries) |
| `GET /healthz` (liveness) | `200` |
| `GET /readyz` (readiness) | `200` ready / `503` (spool worker down) |

## What you own (this is the public edge)

Unlike com-event-relay — where the cloud platform provides the public URL, TLS,
patching and autoscaling — **the bridge is the internet-facing endpoint**, so you
operate:

- a **public DNS name** and **inbound `443`** open to COM's egress;
- a **TLS reverse proxy** in front (nginx/Caddy) as the certificate terminator —
  see [deploy/nginx](deploy/nginx/com-event-bridge.conf);
- the **CA certificate lifecycle** (issuance + renewal) — the compose stack wires
  up certbot for this;
- host hardening, patching, and (if you need it) HA — see [HARDENING.md](HARDENING.md).

## Quick start

### Run locally (sync mode)

```bash
cd bridge
cp .env.example .env        # set COM_SHARED_SECRET + TARGET + target creds
pip install -e ../../com-event-core   # shared normaliser/dedup/adapters (+ httpx)
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080
```

Smoke-test the handshake and a bad-secret rejection:

```bash
curl -i localhost:8080/com/webhook -H "x-compute-ops-mgmt-verification-challenge: abc123"
curl -i -X POST localhost:8080/com/webhook -d '{}'          # 401 (no secret)
```

### Run the full DMZ stack (bridge + nginx TLS + certbot)

Edit the server name in [deploy/nginx/com-event-bridge.conf](deploy/nginx/com-event-bridge.conf),
bootstrap the certificate once (see [HARDENING.md](HARDENING.md)), then:

```bash
cp bridge/.env.example bridge/.env    # set secrets + target
docker compose up -d --build
```

## Configuration (env vars)

| Var | Required | Notes |
|---|---|---|
| `COM_SHARED_SECRET` | yes | Secret COM presents on every event. |
| `SHARED_SECRET_HEADER` | no | Header carrying the secret. Default `x-shim-secret`. |
| `MAX_BODY_BYTES` | no | Max request body. Default `262144` (256 KB). |
| `DELIVERY_MODE` | no | `sync` (default) or `spool`. |
| `SPOOL_PATH` / `SPOOL_MAX_BYTES` / `SPOOL_RETRY_SECONDS` / `SPOOL_RETRY_CAP` / `SPOOL_POLL_SECONDS` | no | Spool tuning (`spool` mode). |
| `TARGET` | no | `obm` (default) / `servicenow` / `opsramp` / `halo` / `splunk` / `webhook`. |
| `TARGET_TIMEOUT` | no | Per-target HTTP timeout (s). Default `15`. |
| `DEDUP_DB_PATH` / `DEDUP_TTL_SECONDS` | no | De-dup store + window (`0` disables). |
| `OBM_EVENT_API_URL` / `OBM_USER` / `OBM_PASSWORD` | if `TARGET=obm` | OBM Event REST API + Basic auth. |
| `SNOW_INSTANCE` / `SNOW_USER` / `SNOW_PASSWORD` | if `TARGET=servicenow` | `SNOW_TABLE` optional (`em_event` default / `incident`). |
| `OPSRAMP_API_URL` / `OPSRAMP_TENANT_ID` / `OPSRAMP_KEY` / `OPSRAMP_SECRET` | if `TARGET=opsramp` | OAuth2 client-credentials; `OPSRAMP_SERVICE_NAME` optional. |
| `HALO_API_URL` / `HALO_CLIENT_ID` / `HALO_CLIENT_SECRET` | if `TARGET=halo` | OAuth2 client-credentials; `HALO_TENANT` / `HALO_TICKET_TYPE_ID` optional. |
| `SPLUNK_HEC_URL` / `SPLUNK_HEC_TOKEN` | if `TARGET=splunk` | HEC endpoint + token. |
| `WEBHOOK_URL` | if `TARGET=webhook` | Optional `WEBHOOK_AUTH_HEADER`/`_VALUE`. |

## Register the webhook in COM

Point a COM webhook at your public bridge URL with the shared-secret header:

- `destination` = `https://com-bridge.example.com/com/webhook`
- `headers` = `x-shim-secret: <COM_SHARED_SECRET>`
- `eventFilter` = your chosen filter

COM sends the handshake, the bridge echoes the token, and the webhook goes
`ACTIVE` / `ENABLED`.

## Project layout

```
com-event-bridge/
  bridge/
    app.py                 # single-process: handshake + auth + normalize + deliver
    core/
      spool.py             # durable on-disk spool + background retry worker
    Dockerfile
    requirements.txt
    .env.example
  deploy/
    nginx/com-event-bridge.conf     # TLS reverse proxy
    systemd/com-event-bridge.service # bare-host service unit
  docker-compose.yml       # bridge + nginx + certbot (auto-renew)
  HARDENING.md             # DMZ + TLS + cert lifecycle + host hardening
  README.md
```

The COM normaliser, de-dup store, and target adapters come from the shared
**[com-event-core](../com-event-core)** package (see below) — they are not
vendored here.

## Relationship to com-event-relay

The normaliser, de-dup store, and all target adapters are **not** duplicated here
— they live in the shared **[com-event-core](../com-event-core)** package that both
this project and com-event-relay depend on, so a mapping or adapter fix is made
once. The only bridge-specific code is [app.py](bridge/app.py) (single-process
handshake + auth + deliver) and [core/spool.py](bridge/core/spool.py) (the local
durable buffer that replaces the cloud queue). New target adapters are contributed
to `com-event-core` and become available to both projects automatically.
