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
| Durability | Durable cloud queue | Local spool (optional); `sync` is best-effort |
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
5. **Deliver** via the adapter(s) named by `TARGETS` (`obm` / `servicenow` / `opsramp` / `halo` / `splunk`
   / `github` / `slack` / `teams` / `jira` / `pagerduty` / `sentinel` / `datadog` / `elastic` / `bmc_helix` / `dynatrace` / `grafana` / `webhook`) — one target, or several comma-separated to fan out — see
   [Delivery modes](#delivery-modes) and
   [Delivering to multiple targets](../README.md#delivering-to-multiple-targets-at-once).
6. **De-duplicate.** A local SQLite TTL store (keyed **per target**) suppresses
   repeats (duplicate or redelivered events for the same fault).

## Delivery modes

Because there is no queue, you choose how delivery is guaranteed with
`DELIVERY_MODE`:

| Mode | Behaviour | Trade-off |
|---|---|---|
| **`spool`** (default) | Persist the event to a local on-disk spool (SQLite), ack COM with `202` immediately, and drain it in a **background worker** that retries with capped exponential backoff. | Survives target outages without a cloud queue; the box's disk is the buffer. **Requires a durable `SPOOL_PATH`** (see prerequisite below). |
| **`sync`** | Forward inline. If the target fails, return `503` — but COM is **fire-and-forget** (no retries), so the event is **lost**, and sustained `5xx` from a down target risks the webhook being **disabled** in COM (10 consecutive failures). | Best-effort only; opt in with `DELIVERY_MODE=sync` where occasional loss is acceptable. |

> **Prerequisite — durable spool storage.** In `spool` mode `SPOOL_PATH` **must**
> point at persistent storage (a mounted volume), and the bridge **refuses to
> start** if `SPOOL_PATH` is unset. An ephemeral path (e.g. `./spool.db` inside a
> container) would silently discard the pending backlog on restart/redeploy —
> false durability — so this is a hard fail rather than a silent footgun.
>
> - **Container / compose:** mount a named volume at `/data`
>   ([docker-compose.yml](docker-compose.yml) already mounts `bridge-data:/data`,
>   and the image defaults `SPOOL_PATH=/data/spool.db`).
> - **Bare metal / systemd:** set `SPOOL_PATH` to an absolute path on a
>   persistent disk, e.g. `/var/lib/com-event-bridge/spool.db`.

In `spool` mode the backlog is capped at `SPOOL_MAX_BYTES`; once exceeded, new
events get `503` **backpressure** (dropped — COM does not retry) so a prolonged
outage can't fill the disk. The spool is crash-safe — unsent events survive a
restart.

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
| `POST`, `sync`, target unavailable | `503` (event lost — COM does not retry) |
| `POST`, `spool`, backlog full | `503` (dropped — COM does not retry) |
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
- **durable spool storage** (a mounted volume for `SPOOL_PATH`) in the default
  `spool` mode — the bridge won't start without it;
- host hardening, patching, and (if you need it) HA — see [HARDENING.md](HARDENING.md).

## Quick start

### Run locally (sync mode)

The default mode is `spool`, which requires a durable `SPOOL_PATH`. For a quick
local smoke test of the handshake and auth, opt into best-effort `sync` so no
spool volume is needed:

```bash
cd bridge
cp .env.example .env        # set COM_SHARED_SECRET + TARGETS + target creds
# for this local smoke test only, force best-effort inline delivery:
#   set DELIVERY_MODE=sync in .env  (production should use spool + a volume)
pip install -e ../../com-event-core   # shared normaliser/dedup/adapters (+ httpx)
pip install -r requirements.txt
DELIVERY_MODE=sync uvicorn app:app --host 0.0.0.0 --port 8080
```

Smoke-test the handshake and a bad-secret rejection:

```bash
curl -i localhost:8080/com/webhook -H "x-compute-ops-mgmt-verification-challenge: abc123"
curl -i -X POST localhost:8080/com/webhook -d '{}'          # 401 (no secret)
```

### Run the container (spool mode, with a durable volume)

In the default `spool` mode the bridge needs a **persistent volume** for the
spool DB, otherwise it refuses to start (an ephemeral path would lose the backlog
on restart). The image already defaults `SPOOL_PATH=/data/spool.db`, so you just
have to mount a volume at `/data`:

```bash
# Build (context is the repo root so the image includes com-event-core)
docker build -f com-event-bridge/bridge/Dockerfile -t com-event-bridge:local .

# Create a named volume once — this is what makes the spool survive restarts
docker volume create bridge-data

# Run, mounting the volume at /data (matches the image's SPOOL_PATH default)
docker run -d --name com-event-bridge \
  -p 8080:8080 \
  -v bridge-data:/data \
  --env-file com-event-bridge/bridge/.env \
  com-event-bridge:local
```

- `-v bridge-data:/data` is the important part — it maps the persistent volume
  onto `/data`, where `SPOOL_PATH` (and `DEDUP_DB_PATH`) live.
- Prefer a **named volume** (`bridge-data`) as above; a host path also works
  (e.g. `-v /srv/com-event-bridge:/data`), just make sure the directory is
  writable by uid `10001` (the non-root user in the image).
- To point the spool elsewhere, override both the path and the mount, e.g.
  `-e SPOOL_PATH=/data/spool.db -v bridge-data:/data`.

> This runs the bridge alone (no TLS). It still needs a TLS reverse proxy in
> front for COM — use the full compose stack below for that.

### Run the full DMZ stack (bridge + nginx TLS + certbot)

Edit the server name in [deploy/nginx/com-event-bridge.conf](deploy/nginx/com-event-bridge.conf),
bootstrap the certificate once (see [HARDENING.md](HARDENING.md)), then:

```bash
cp bridge/.env.example bridge/.env    # set secrets + target
docker compose up -d --build
```

The compose stack already declares the `bridge-data` volume and mounts it at
`/data` for you ([docker-compose.yml](docker-compose.yml)), so the spool is
durable out of the box.

## Configuration (env vars)

| Var | Required | Notes |
|---|---|---|
| `COM_SHARED_SECRET` | yes | Secret COM presents on every event. |
| `SHARED_SECRET_HEADER` | no | Header carrying the secret. Default `x-shim-secret`. |
| `MAX_BODY_BYTES` | no | Max request body. Default `262144` (256 KB). |
| `DELIVERY_MODE` | no | `spool` (default, durable) or `sync` (best-effort). |
| `SPOOL_PATH` | **yes, in `spool` mode** | Path to the spool DB on **durable** storage (mounted volume). Bridge won't start in spool mode if unset. Container default `/data/spool.db`. |
| `SPOOL_MAX_BYTES` / `SPOOL_RETRY_SECONDS` / `SPOOL_RETRY_CAP` / `SPOOL_POLL_SECONDS` | no | Spool tuning (`spool` mode). |
| `TARGETS` | no | Target(s): one name or comma-separated for fan-out, e.g. `halo,opsramp`. Default `webhook`. Each of `obm` / `servicenow` / `opsramp` / `halo` / `splunk` / `github` / `slack` / `teams` / `jira` / `pagerduty` / `sentinel` / `datadog` / `elastic` / `bmc_helix` / `dynatrace` / `grafana` / `webhook`. Fan-out is reliable in `spool` mode (retry re-attempts only failed targets); best-effort in `sync`. |
| `TARGET_TIMEOUT` | no | Per-target HTTP timeout (s). Default `15`. |
| `SERVER_MONITORS` | no | Server conditions to watch, comma-separated: `health` (default) / `power` / `connection` / `subscription`. Each opens/closes its own item. |
| `DEDUP_DB_PATH` / `DEDUP_TTL_SECONDS` | no | De-dup store + window (`0` disables). |
| `OBM_EVENT_API_URL` / `OBM_USER` / `OBM_PASSWORD` | if `obm` in `TARGETS` | OBM Event REST API + Basic auth. |
| `SNOW_INSTANCE` / `SNOW_USER` / `SNOW_PASSWORD` | if `servicenow` in `TARGETS` | `SNOW_TABLE` optional (`em_event` default / `incident`). |
| `OPSRAMP_API_URL` / `OPSRAMP_TENANT_ID` / `OPSRAMP_KEY` / `OPSRAMP_SECRET` | if `opsramp` in `TARGETS` | OAuth2 client-credentials; `OPSRAMP_SERVICE_NAME` optional. |
| `HALO_API_URL` / `HALO_CLIENT_ID` / `HALO_CLIENT_SECRET` | if `halo` in `TARGETS` | OAuth2 client-credentials; `HALO_TENANT` / `HALO_TICKET_TYPE_ID` optional. |
| `SPLUNK_HEC_URL` / `SPLUNK_HEC_TOKEN` | if `splunk` in `TARGETS` | HEC endpoint + token. |
| `GITHUB_REPO` / `GITHUB_TOKEN` | if `github` in `TARGETS` | `owner/repo` + PAT (`issues:write`); `GITHUB_API_URL` (GHE) / `GITHUB_LABELS` optional. |
| `SLACK_WEBHOOK_URL` | if `slack` in `TARGETS` | Incoming Webhook URL; `SLACK_USERNAME` optional. |
| `TEAMS_WEBHOOK_URL` | if `teams` in `TARGETS` | Teams Workflows / Power Automate webhook URL. |
| `JIRA_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` / `JIRA_PROJECT_KEY` | if `jira` in `TARGETS` | Jira Cloud site + Basic auth; `JIRA_ISSUE_TYPE` / `JIRA_CLOSE_TRANSITION` / `JIRA_LABELS` optional. |
| `PAGERDUTY_ROUTING_KEY` | if `pagerduty` in `TARGETS` | Events API v2 integration key; `PAGERDUTY_API_URL` (EU) optional. |
| `SENTINEL_WORKSPACE_ID` / `SENTINEL_SHARED_KEY` | if `sentinel` in `TARGETS` | Log Analytics workspace + key; `SENTINEL_LOG_TYPE` optional. |
| `DATADOG_API_KEY` | if `datadog` in `TARGETS` | API key; `DATADOG_SITE` / `DATADOG_TAGS` optional. |
| `ELASTIC_URL` / `ELASTIC_API_KEY` | if `elastic` in `TARGETS` | Cluster URL + API key (or `ELASTIC_USER`/`ELASTIC_PASSWORD`); `ELASTIC_INDEX` optional. |
| `BMC_HELIX_URL` / `BMC_HELIX_USER` / `BMC_HELIX_PASSWORD` | if `bmc_helix` in `TARGETS` | AR System REST base + JWT auth; `BMC_HELIX_SERVICE_TYPE` / `BMC_HELIX_ASSIGNED_GROUP` / `BMC_HELIX_STATUS_RESOLVED` optional. |
| `DYNATRACE_URL` / `DYNATRACE_API_TOKEN` | if `dynatrace` in `TARGETS` | Environment API base + token (`events.ingest`); `DYNATRACE_ENTITY_SELECTOR` / `DYNATRACE_PROPERTIES` optional. |
| `GRAFANA_LOKI_URL` / `GRAFANA_LOKI_USER` / `GRAFANA_API_TOKEN` | if `grafana` in `TARGETS` | Grafana Cloud Logs (Loki) URL + user id + token (`logs:write`); `GRAFANA_LABELS` optional. |
| `WEBHOOK_URL` | if `webhook` in `TARGETS` | Optional `WEBHOOK_AUTH_HEADER`/`_VALUE`. |

> **Every secret above can be read from a file instead of the environment** —
> see [Secrets management](#secrets-management).

## Secrets management

Sensitive values — `COM_SHARED_SECRET`, the target passwords / client secrets /
tokens (`OBM_PASSWORD`, `SNOW_PASSWORD`, `OPSRAMP_KEY`/`OPSRAMP_SECRET`,
`HALO_CLIENT_ID`/`HALO_CLIENT_SECRET`, `SPLUNK_HEC_TOKEN`, `GITHUB_TOKEN`,
`SLACK_WEBHOOK_URL`, `TEAMS_WEBHOOK_URL`, `JIRA_API_TOKEN`, `PAGERDUTY_ROUTING_KEY`,
`SENTINEL_SHARED_KEY`, `DATADOG_API_KEY`, `ELASTIC_API_KEY`/`ELASTIC_PASSWORD`,
`BMC_HELIX_PASSWORD`, `DYNATRACE_API_TOKEN`, `GRAFANA_API_TOKEN`,
`WEBHOOK_AUTH_VALUE`)
— should **not** live in a plaintext `.env` in production. Every one of them can
instead be read from a **file**, so you can back them with a vault or the
platform's native secret store.

**How it works.** For any secret `<NAME>`, the bridge resolves it in this order:

1. `<NAME>_FILE` — if set, the secret is the **contents of that file** (a trailing
   newline is stripped);
2. `<NAME>` — otherwise the plain environment variable (handy for local dev);
3. otherwise startup **fails fast** with a clear "missing secret" error.

So you keep the value out of the environment entirely by pointing `<NAME>_FILE` at a
path your deployment projects onto disk. Reading a file avoids the value leaking
into `docker inspect`, `/proc/<pid>/environ`, or child processes.

### Docker Compose / Swarm secrets

*Use this if you run the bridge with `docker compose` (the default deployment).*

Docker mounts each secret at `/run/secrets/<name>` on **tmpfs** (never in the
image or `docker inspect`). In [docker-compose.yml](docker-compose.yml), uncomment
the `secrets:` blocks and set the `*_FILE` vars (and remove those secrets from
`bridge/.env`):

```yaml
services:
  bridge:
    environment:
      COM_SHARED_SECRET_FILE: /run/secrets/com_shared_secret
      OBM_PASSWORD_FILE: /run/secrets/obm_password
    secrets: [com_shared_secret, obm_password]
secrets:
  com_shared_secret:
    file: ./secrets/com_shared_secret.txt   # or `external: true` on Swarm
  obm_password:
    file: ./secrets/obm_password.txt
```

Create the files (`0400`, git-ignored) or, on Swarm,
`docker secret create com_shared_secret ./com_shared_secret.txt` and use
`external: true`.

### systemd `LoadCredential` (bare metal, no Docker)

*Use this if you run the bridge directly on a Linux host via systemd (no Docker).*

systemd copies each credential into a private, per-service `0400` tmpfs dir
exposed as `$CREDENTIALS_DIRECTORY` (`%d`). In
[deploy/systemd/com-event-bridge.service](deploy/systemd/com-event-bridge.service),
uncomment:

```ini
LoadCredential=com_shared_secret:/etc/com-event-bridge/com_shared_secret
LoadCredential=obm_password:/etc/com-event-bridge/obm_password
Environment=COM_SHARED_SECRET_FILE=%d/com_shared_secret
Environment=OBM_PASSWORD_FILE=%d/obm_password
```

Write the source files as `root:0400`, then drop those secrets from
`bridge.env`. For encryption-at-rest (TPM / host key) use
`LoadCredentialEncrypted=` with `systemd-creds encrypt`.

### HashiCorp Vault (on-prem)

*Use this only if your site already runs HashiCorp Vault.*

Run the **Vault Agent** alongside the bridge and render a secret to a tmpfs file
with an Agent template, then point the `*_FILE` var at it — e.g. template
`{{ with secret "secret/com-bridge" }}{{ .Data.data.com_shared_secret }}{{ end }}`
to `/run/com-bridge/com_shared_secret`, and set
`COM_SHARED_SECRET_FILE=/run/com-bridge/com_shared_secret`. Agent handles renewal;
the bridge just re-reads the file at startup. (For Kubernetes, the Vault Agent
Injector or the Secrets Store CSI driver mount files the same way.)

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
