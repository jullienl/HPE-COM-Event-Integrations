# COM Event Relay

A generic, **container-first** relay that lets HPE Compute Ops Management (COM)
deliver webhook events into an environment **without exposing any internal
endpoint** — on **Azure or AWS**, from a single image.

> AI-generated reference implementation. Review and harden before production use.

## Contents

- [Why this exists](#why-this-exists)
- [When you don't need this: native COM integrations](#when-you-dont-need-this-native-com-integrations-opsramp-servicenow)
- [What this project does, in detail](#what-this-project-does-in-detail)
- [Cloud-agnostic by design](#cloud-agnostic-by-design)
- [Deployment model](#deployment-model)
- [Relay behavior](#relay-behavior)
- [Security model](#security-model)
- [Secrets management](#secrets-management)
- [Quick start](#quick-start)
- [Local development with Docker Compose](#local-development-with-docker-compose)
- [Prebuilt images](#prebuilt-images)
- [Configuration (env vars)](#configuration-env-vars)
- [The shim (target adapters)](#the-shim-target-adapters)
- [Project layout](#project-layout)
- [Companion: a single-box on-prem thin shim](#companion-a-single-box-on-prem-thin-shim)

## Why this exists

COM can POST webhook events to a public HTTPS URL. Many customers can't (or won't)
expose an inbound listener on their own network, and often the target system
(OBM, Splunk, HaloITSM, ...) isn't a natively supported COM destination. (For
targets that **are** native — OpsRamp and ServiceNow — you don't need this relay;
see [below](#when-you-dont-need-this-native-com-integrations-opsramp-servicenow).)
This project solves both:

- The **relay** is the *only* public component. It authenticates COM and drops
  each event onto a durable queue. It runs as a managed container with automatic
  HTTPS (Azure Container Apps / AWS App Runner) — no server to patch, no TLS to
  manage.
- A **shim** (separate, outbound-only) pulls from the queue and forwards to the
  target. It opens only *outbound* 443 — **no inbound ports** on the customer
  network.

```
COM ──► Relay (public HTTPS, validates shared secret) ──► Queue ──► Shim (outbound-only) ──► target
        └ Azure Container Apps / AWS App Runner ┘         │ Service Bus / SQS ┘
```

## When you don't need this: native COM integrations (OpsRamp, ServiceNow)

Two targets have **first-party COM integrations** and therefore **don't need this
relay at all** — point COM (or the COM external-service integration) straight at
them:

| Integration | Layer      | Role                                                        | Direct COM support?                       | Notes |
|-------------|------------|-------------------------------------------------------------|-------------------------------------------|-------|
| ServiceNow  | ITSM       | Incident / case management                                  | ✅ Direct — native COM integration         | Native COM external-service integration for incident creation. This is **not** the generic COM webhook mechanism. ([HPE Support Center](https://support.hpe.com/hpesc/public/docDisplay?docId=sd00004003en_us&page=GUID-E622D87F-E8DA-4EA4-9785-3CE271E80A42.html&docLocale=en_US)) |
| OpsRamp     | ITOM / AIOps | Event management, alert correlation, incident creation, automation | ✅ Direct — COM-aware webhook integration | Purpose-built COM event integration. Supports the required COM webhook handshake and COM-to-OpsRamp attribute mapping. ([docs.opsramp.com](https://docs.opsramp.com/integrations/a2r/hpe-greenlake-integrations/working-with-com-opsramp/)) |

### OpsRamp

OpsRamp is HPE's SaaS **AIOps / IT operations management** platform: it ingests
events and alerts from across a hybrid estate, applies **correlation, de-duplication,
and machine-learning-based noise reduction** to turn raw signals into meaningful
**incidents**, and drives **notification, escalation, and automated remediation**
(runbook/process automation). The COM integration plugs COM's hardware health
signals straight into that pipeline: when COM raises Redfish-based alerts (for
example a fan failure followed by a thermal alert), they flow natively into
OpsRamp as alerts, where OpsRamp **correlates the related alerts into a single
incident**, notifies the right stakeholders, and can **trigger an automated
remediation script** (e.g. gracefully power off the affected server) — closing the
loop from detection to response without a custom relay in between.

The reason no shim is required is that **OpsRamp natively answers COM's
verification handshake**: its inbound webhook URL responds to a `GET` carrying
`x-compute-ops-mgmt-verification-challenge` with the expected
`{"verification":"<token>"}` — the exact handshake this relay exists to satisfy
for targets (like OBM) that can't. Setup is simply: create a webhook **on OpsRamp**
(which provides the destination URL + auth), then create a webhook **on COM**
pointing at it. Prerequisite: OpsRamp and COM must be in the **same HPE GreenLake
workspace**. See the official guide:
[Working with HPE COM and OpsRamp event integration](https://docs.opsramp.com/integrations/a2r/hpe-greenlake-integrations/working-with-com-opsramp/).

### ServiceNow

ServiceNow is the **ITSM** system of record for **incident / case management**. COM
provides a **native external-service integration** that creates incidents in
ServiceNow directly — this is a purpose-built COM-to-ServiceNow path, **not** the
generic COM webhook mechanism this relay handles. When it fits your workflow
(COM-driven incident creation), use the native integration and skip the relay
entirely.

> **Note:** this project still ships `servicenow` and `opsramp` shim adapters
> (ServiceNow Table API → `em_event` / `incident`; OpsRamp Alerts API via OAuth2).
> Use them only when you specifically want the **webhook/queue-decoupled** path —
> for example when you **cannot open an inbound firewall port** (the native
> integrations require COM to reach an endpoint you expose; the outbound-only shim
> does not), to feed ServiceNow **Event Management** or OpsRamp **alerts** with
> enriched/normalised events, to keep the internal network unexposed, or to route
> the same COM event stream to several targets at once. For plain COM-driven
> incident creation (ServiceNow) or native event ingestion (OpsRamp), prefer the
> native integrations above.

> **Bottom line:** use this relay when the target is **not** a natively supported
> COM destination (OBM, Splunk, a generic webhook, ...), or when you need the
> queue-decoupled, network-unexposed delivery it provides — including the case
> where you want ServiceNow/OpsRamp but **can't open an inbound firewall port**.
> For **OpsRamp** and **ServiceNow** incident creation *when you can expose an
> endpoint COM reaches*, point COM straight at the native integration.

## What this project does, in detail

The relay is a small FastAPI app with a single job: **safely accept COM webhook
events at the edge and hand them to a queue**. It never transforms payloads and
never talks to the target system — that separation is what keeps the public
surface tiny and the internal network unexposed.

A request goes through these stages:

1. **Verification handshake.** When you register the webhook, COM first sends a
   request carrying an `x-compute-ops-mgmt-verification-challenge` header. The
   relay echoes the token back as `{"verification": "<token>"}` with a `200`.
   This is how COM confirms it owns the endpoint before sending real events.

2. **Authentication.** Every real event must carry a shared secret in a header
   (default `x-shim-secret`, configurable). The relay compares it to
   `COM_SHARED_SECRET` using a **constant-time** comparison (`hmac.compare_digest`)
   so a wrong or missing secret returns `401` and nothing is enqueued. This stops
   anyone who discovers the public URL from injecting events.

3. **Abuse protection.** Because the endpoint is public, the relay caps request
   bodies at `MAX_BODY_BYTES` (default 256 KB) — rejecting oversized payloads
   early with `413` (checked via `Content-Length`, then hard-guarded after read).

4. **Enqueue with metadata + correlation id.** The relay stamps each event with a
   generated `relay_event_id` and attaches metadata (event type, receive time,
   source) as **queue message properties** (Service Bus application properties /
   SQS message attributes). The shim can then filter or route without re-parsing
   the body. The `relay_event_id` is logged at the relay and surfaced in the
   `x-relay-event-id` response header — handy for manual testing (curl/Postman),
   though COM itself only reads the status code. Downstream, delivery is
   correlated by the **COM event id** (`event_id`), which the shim and every
   adapter log — giving you **traceability from COM through to the target**.

5. **Reliable hand-off.** The raw body is published to the queue. COM webhooks are
   **fire-and-forget** (one POST, no retries), so the relay's job is to capture
   each event into the durable queue *immediately*; a successful enqueue returns
   `202`. If the enqueue fails (transient broker issue), the relay returns `503` —
   but **COM will not resend it**, and a `5xx` is not free: COM counts webhook
   failures and after **10 consecutive failures disables the webhook** (stopping
   *all* delivery until you manually re-enable it). A highly available managed
   queue keeps enqueue failures rare so both the loss window and the health impact
   stay tiny.

The queue in the middle **decouples** the public edge from the internal consumer:
if the on-prem shim is down or slow, events wait durably in the queue instead of
being lost, and the relay keeps accepting new ones.

**Operational endpoints.** `/healthz` is a **liveness** probe (process is up).
`/readyz` is a **readiness** probe that actually checks the queue backend is
reachable — so a broken Service Bus/SQS connection marks the instance "not ready"
and the platform stops routing traffic to it.

**Observability.** All logs are emitted as **structured JSON** (one object per
line) with the correlation id and status, so they parse cleanly in Application
Insights, CloudWatch, or any OTLP pipeline.

### Relay vs shim: who does what

The relay model splits the pipeline in two, with the **queue as the durable
buffer** between them. The relay only *accepts safely at the edge*; the shim does
all the *deliver-with-resilience* work (the same logic the single-box
[com-event-bridge](../com-event-bridge) runs in one process):

| Stage | Relay (public cloud edge) | Shim (near the target) |
|---|:---:|:---:|
| Verification handshake | ✅ | — |
| Authentication (shared-secret header) | ✅ | — |
| Input hardening (body-size cap → `413`) | ✅ | — |
| Enqueue → cloud queue (publish) | ✅ | — |
| Health / readiness endpoints | ✅ | — (no HTTP server) |
| Dequeue ← cloud queue (consume) | — | ✅ |
| Normalise → `CanonicalEvent` | — | ✅ |
| De-duplication (SQLite TTL store) | — | ✅ |
| Correlation (raise / clear lifecycle) | id stamp only (tracing) | ✅ |
| Forward to target adapter | — | ✅ |
| Retry / redelivery | — | ✅ (queue `abandon`) |
| Durable buffer | — **the cloud queue sits between them** — | |

The relay is **stateless and fire-and-forget** (enqueue + ack, never touches the
target); the shim is **outbound-only** (pulls from the queue, no inbound ports).
Because the queue is at-least-once, the shim can receive a redelivered message
after an `abandon` — which is exactly what its de-dup store guards against.

## Cloud-agnostic by design

The relay depends only on a small `QueuePublisher` interface. The backend is
chosen at startup by the `QUEUE_BACKEND` env var — the same image runs on either
cloud:

| Concern            | Azure                        | AWS                         |
|--------------------|------------------------------|-----------------------------|
| Public HTTPS relay | Container Apps               | App Runner                  |
| Queue              | Service Bus (`servicebus`)   | SQS (`sqs`)                 |
| Secret store       | Key Vault                    | Secrets Manager / SSM       |
| Identity           | Managed Identity             | IAM role                    |

The backend also validates its required config **at startup** (fails fast with a
clear message if, say, `QUEUE_BACKEND=sqs` but `SQS_QUEUE_URL` is unset) rather
than erroring lazily on the first event.

## Deployment model

COM is a cloud (SaaS) service that **pushes** events over HTTPS to a public
endpoint you provide. Two facts shape how you receive them:

- Whatever COM talks to **must be publicly reachable** (public DNS name, valid TLS
  certificate, inbound `443`).
- Whatever talks to **your target system** only needs **outbound** access — it
  never has to be exposed to the internet.

**This project is the cloud model.** It splits those two jobs so your internal
network is never exposed:

- The **relay** (public, cloud-hosted on Azure Container Apps or AWS App Runner) is
  the only internet-facing component: it authenticates COM and drops each event
  onto a durable queue (Service Bus / SQS). The platform provides the public URL,
  automatic TLS (issuance + renewal), OS patching, and autoscaling — you operate
  none of it.
- The **shim** (outbound-only, runs **on-premises** next to your target) pulls from
  the queue and forwards. Nothing inbound is ever opened on your network, and the
  queue buffers events so a target outage never loses them.

A small managed cloud footprint (one container + one queue) is therefore a
**prerequisite** — that is the model this project is built for.

> **No cloud allowed?** Use the sibling **[com-event-bridge](../com-event-bridge)**
> instead — a single on-prem box that runs the same pipeline with no cloud and no
> queue (you host the public TLS edge yourself; it ships an nginx + certbot stack
> to help).
>
> **Relay or bridge — which should I pick?** The full side-by-side comparison, pros
> and cons, and decision flowchart live in the
> [root README](../README.md#which-project-do-i-use).

If your target is **OpsRamp** or **ServiceNow incident creation**, you don't need
this project at all — use the native COM integration described
[above](#when-you-dont-need-this-native-com-integrations-opsramp-servicenow).

## Relay behavior

| Request                                                 | Response                             |
|---------------------------------------------------------|--------------------------------------|
| Handshake (`x-compute-ops-mgmt-verification-challenge`) | `200` + `{"verification":"<token>"}` |
| POST with valid `x-shim-secret`                         | `202` (enqueued) + `x-relay-event-id`|
| POST with missing/invalid secret                        | `401`                                |
| POST body larger than `MAX_BODY_BYTES`                  | `413`                                |
| POST but the queue enqueue fails (transient)            | `503` (event lost — COM does not retry) |
| `GET /healthz` (liveness)                               | `200` + `{"status":"ok"}`            |
| `GET /readyz` (readiness — checks queue reachable)      | `200` ready / `503` not ready        |

## Security model

What protects the public endpoint, and what is expected from the surrounding
infrastructure:

- **Authentication — static shared secret over TLS.** Every event must carry a
  secret in a header (default `x-shim-secret`), compared in constant time. TLS is
  provided by the hosting platform (Container Apps / App Runner), so the secret is
  never sent in clear text. **Rotate** the secret periodically (update it in the
  COM webhook and in the relay's secret store together).

  > COM does **not** sign webhook payloads (no HMAC), so payload-signature
  > verification isn't available — the shared-secret header is the supported
  > authentication mechanism. Keep the secret long and random
  > (`openssl rand -hex 32`) and store it in Key Vault / Secrets Manager, not in
  > plaintext config.

- **Input hardening.** Request bodies are capped at `MAX_BODY_BYTES` (default
  256 KB) and rejected with `413` to blunt oversized-payload abuse.

- **Fail loud, not silent.** COM is fire-and-forget and does **not** retry, so a
  failed enqueue can't be recovered by COM — the relay returns `503` and logs it
  rather than pretending success. Note a `5xx` is not consequence-free: COM tracks
  webhook failures and **10 consecutive failures disables the webhook**, so the
  managed queue's high availability (keeping enqueue failures rare) protects both
  against event loss *and* against the webhook being disabled. A bad secret
  returns `401` and nothing is enqueued.

- **Rate limiting / DDoS is a platform concern.** The relay is deliberately
  stateless and does not implement per-client throttling (an in-app limiter can't
  coordinate across autoscaled replicas). Put a **WAF / rate limit in front**
  (Azure Front Door / AWS WAF) if you need it — that's the recommended layer for
  abuse protection.

- **Duplicate suppression is the shim's job.** De-duplication happens in the
  outbound shim (where per-event state is natural), keeping the relay thin and
  horizontally scalable.

## Secrets management

Neither the relay nor the shim requires secrets to sit in a plaintext `.env`.
Every sensitive value — `COM_SHARED_SECRET` and `SERVICE_BUS_CONNECTION` (relay),
plus the shim's target passwords / client secrets / tokens (`OBM_PASSWORD`,
`SNOW_PASSWORD`, `OPSRAMP_KEY`/`OPSRAMP_SECRET`, `HALO_CLIENT_ID`/`HALO_CLIENT_SECRET`,
`SPLUNK_HEC_TOKEN`, `GITHUB_TOKEN`, `SLACK_WEBHOOK_URL`, `TEAMS_WEBHOOK_URL`,
`JIRA_API_TOKEN`, `PAGERDUTY_ROUTING_KEY`, `SENTINEL_SHARED_KEY`, `DATADOG_API_KEY`,
`ELASTIC_API_KEY`/`ELASTIC_PASSWORD`, `BMC_HELIX_PASSWORD`, `DYNATRACE_API_TOKEN`,
`GRAFANA_API_TOKEN`, `WEBHOOK_AUTH_VALUE`) — can be read from a
**file** instead.

**How it works.** For any secret `<NAME>`, resolution order is:

1. `<NAME>_FILE` — read the secret from that file path (trailing newline stripped);
2. `<NAME>` — otherwise the plain environment variable (handy for local dev);
3. otherwise startup **fails fast** with a clear "missing secret" error.

Point `<NAME>_FILE` at a path your platform projects a vault secret onto, and the
value never enters the container's environment (so it can't leak via
`docker inspect` / `/proc/<pid>/environ`).

### Azure Key Vault (Container Apps / AKS)

*For relays hosted on Azure. Skip this unless you deploy to Azure — locally, just
use the plain `COM_SHARED_SECRET=...` env var in `.env`.*

- **AKS + Secrets Store CSI driver:** install the driver + the Azure Key Vault
  provider, grant the workload identity `get` on the secrets, and mount them.
  The block below is a fragment of a **Kubernetes** Deployment (not a file you run
  on its own) — add these lines to your app's container spec so each vault secret
  appears as a file and the app is told to read it:

  ```yaml
  # SecretProviderClass mounts each vault secret as a file under the volume.
  volumeMounts:
    - name: secrets-store
      mountPath: /mnt/secrets-store
      readOnly: true
  env:
    - name: COM_SHARED_SECRET_FILE
      value: /mnt/secrets-store/com-shared-secret
    - name: SERVICE_BUS_CONNECTION_FILE
      value: /mnt/secrets-store/sb-connection
  ```

- **Container Apps:** bind a Key Vault reference to a container-app secret, then
  either map it to `COM_SHARED_SECRET` directly, or mount the secret as a file
  (Container Apps secret volume) and set `COM_SHARED_SECRET_FILE` to the mount
  path.

### AWS Secrets Manager (App Runner / EKS)

*The AWS equivalent of the above. Skip unless you deploy the relay on AWS.*

- **EKS + Secrets Store CSI driver** with the AWS provider (ASCP) mounts each
  secret as a file — set `..._FILE` to the mount path exactly as above.
- **App Runner / ECS:** reference the secret in the task definition; to keep it
  out of the environment, mount it (EFS/secret volume) and use `..._FILE`, or map
  it to the plain env var if a file mount isn't available.

### HashiCorp Vault

*For teams already running HashiCorp Vault (cloud-agnostic).*

Use the Vault Agent Injector (Kubernetes) or a sidecar Vault Agent to render each
secret to a shared tmpfs file, then set `..._FILE` to that path. Agent handles
lease renewal; the app reads the file at startup.

> Prefer **least-privilege queue credentials**: a **Send**-scoped
> `SERVICE_BUS_CONNECTION` for the relay, a **Listen**-scoped one for the shim —
> stored as separate vault secrets.

## Quick start

### Run locally

```bash
cd relay
cp .env.example .env        # fill in COM_SHARED_SECRET + a queue backend
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080
```

### Build the image

```bash
cd relay
docker build -t com-event-relay:latest .
docker run -p 8080:8080 --env-file .env com-event-relay:latest
```

### Deploy to Azure (Container Apps)

```bash
./deploy/azure/deploy-relay-azure.sh
# prints the webhook URL + generated shared secret
```

> **Step-by-step runbook:** for a full walk-through — provisioning, wiring the COM
> webhook, running the shim, and an end-to-end GitHub Issues test — see
> [docs/Deploy-Cloud-Relay-to-Azure.md](docs/Deploy-Cloud-Relay-to-Azure.md).

### Deploy to AWS (App Runner)

```bash
export INSTANCE_ROLE_ARN=arn:aws:iam::123456789012:role/com-relay-sqs-send
./deploy/aws/deploy-relay-aws.sh
# prints the webhook URL + generated shared secret
```

> **Step-by-step runbook:** for a full walk-through — provisioning, wiring the COM
> webhook, running the shim, and an end-to-end GitHub Issues test — see
> [docs/Deploy-Cloud-Relay-to-AWS.md](docs/Deploy-Cloud-Relay-to-AWS.md).

## Local development with Docker Compose

[Docker Compose](https://docs.docker.com/compose/) lets you run the relay from a
single declarative file instead of long `docker run` commands. It is meant for
**local testing and demos** — production runs on Azure Container Apps or AWS App
Runner, not Compose.

[docker-compose.yml](docker-compose.yml) defines one `relay` service that builds
from [relay/Dockerfile](relay/Dockerfile), maps port 8080, loads `relay/.env`,
and adds a container healthcheck against `/healthz`. Start it with:

```bash
cp relay/.env.example relay/.env    # set COM_SHARED_SECRET etc.
docker compose up --build
```

Then smoke-test locally (another terminal):

```bash
curl -i localhost:8080/healthz
curl -i localhost:8080/com/webhook -H "x-compute-ops-mgmt-verification-challenge: abc123"
```

Stop the stack with `docker compose down`. For a full local relay -> queue loop,
point `QUEUE_BACKEND` at a real Service Bus/SQS (or add a queue-emulator service
to the compose file).

## Prebuilt images

Ready-made, versioned container images are published to the GitHub Container
Registry — you don't need to build anything:

```
ghcr.io/<owner>/com-event-relay:latest      # relay  (or a pinned version, e.g. :0.1.0)
ghcr.io/<owner>/com-event-shim:latest       # shim   (or a pinned version, e.g. :0.1.0)
```

The deploy scripts pull these images automatically. To build locally instead,
see [Build the image](#build-the-image) above.

## Configuration (env vars)

| Var                     | Required | Notes                                              |
|-------------------------|----------|----------------------------------------------------|
| `COM_SHARED_SECRET`     | yes      | Secret COM must present on every event.            |
| `SHARED_SECRET_HEADER`  | no       | Header carrying the secret. Default `x-shim-secret`.|
| `MAX_BODY_BYTES`        | no       | Max accepted request body. Default `262144` (256 KB).|
| `QUEUE_BACKEND`         | no       | `servicebus` (default) or `sqs`.                   |
| `SERVICE_BUS_CONNECTION`| if azure | Send-scoped connection string.                     |
| `QUEUE_NAME`            | if azure | Queue name, e.g. `com-events`.                     |
| `SQS_QUEUE_URL`         | if aws   | Full SQS queue URL.                                |
| `AWS_REGION`            | if aws   | Region of the queue.                               |

## The shim (target adapters)

The **shim** is the outbound-only consumer that drains the queue and forwards
events to a target. It runs as a container **near the target** (on-prem, a DMZ,
or a container host in any cloud) and opens only an **outbound** connection to
the queue — no inbound ports.

One image serves **every target and both clouds**; you pick behaviour with env
vars:

- `TARGETS` selects the adapter(s): one name, or comma-separated for fan-out
  (e.g. `halo,opsramp`, one COM event delivered to each) — `obm` | `servicenow` |
  `opsramp` | `halo` | `splunk` | `github` | `slack` | `teams` | `jira` |
  `pagerduty` | `sentinel` | `datadog` | `elastic` | `bmc_helix` | `dynatrace` |
  `grafana` | `webhook`
- `QUEUE_BACKEND` selects the queue: `servicebus` | `sqs` (must match the relay)

For each message the shim: parses + **normalises** the COM event into a neutral
`CanonicalEvent`, **de-duplicates** it (local SQLite TTL store, **per target**),
**forwards** it via each selected adapter, then **acknowledges** the message
(complete on success; abandon for retry on transient failure; dead-letter on
malformed input). With multiple targets, a partial failure abandons the message
so redelivery re-attempts **only** the failed target(s) — no duplicate tickets.
Adding a new target is a small adapter that maps `CanonicalEvent` → the target's
API — the consume/dedup/retry core is shared.

> The normaliser, de-dup store, and target adapters live in the shared
> **[com-event-core](../com-event-core)** package (also used by
> [com-event-bridge](../com-event-bridge)), so a mapping or adapter fix is made
> once. The shim itself only adds the queue-consume loop. New adapters are
> contributed to `com-event-core`.

### Run the shim locally

```bash
cd shim
cp .env.example .env        # set TARGETS + queue + target credentials
pip install -e ../../com-event-core   # shared normaliser/dedup/adapters (+ httpx)
pip install -r requirements.txt
python worker.py
```

Or via Docker Compose alongside the relay:

```bash
docker compose --profile shim up --build
```

### Shim configuration (env vars)

| Var                      | Required            | Notes                                             |
|--------------------------|---------------------|---------------------------------------------------|
| `TARGETS`                | no                  | Target(s): one name or comma-separated for fan-out, e.g. `halo,opsramp`. Default `webhook`. Each of `obm` / `servicenow` / `opsramp` / `halo` / `splunk` / `github` / `slack` / `teams` / `jira` / `pagerduty` / `sentinel` / `datadog` / `elastic` / `bmc_helix` / `dynatrace` / `grafana` / `webhook`. |
| `QUEUE_BACKEND`          | no                  | `servicebus` (default) or `sqs` — must match relay.|
| `SERVICE_BUS_CONNECTION` | if azure            | **Listen**-scoped connection string.              |
| `QUEUE_NAME`             | if azure            | Queue to drain, e.g. `com-events`.                |
| `SQS_QUEUE_URL`          | if aws              | Full SQS queue URL.                               |
| `AWS_REGION`             | if aws              | Region of the queue.                              |
| `DEDUP_TTL_SECONDS`      | no                  | Dedup window. Default `3600`; `0` disables.       |
| `TARGET_TIMEOUT`         | no                  | Per-target HTTP timeout (s). Default `15`.        |
| `SERVER_MONITORS`        | no                  | Server conditions to watch, comma-separated: `health` (default) / `power` / `connection` / `subscription`. Each opens/closes its own item. A condition **not** listed is not monitored (no item opens/closes for it, no error). |
| `OBM_EVENT_API_URL` / `OBM_USER` / `OBM_PASSWORD`     | if `obm` in `TARGETS`        | OBM Event REST API + Basic auth.        |
| `SNOW_INSTANCE` / `SNOW_USER` / `SNOW_PASSWORD`       | if `servicenow` in `TARGETS` | `SNOW_TABLE` optional (`em_event` default / `incident`). |
| `OPSRAMP_API_URL` / `OPSRAMP_TENANT_ID` / `OPSRAMP_KEY` / `OPSRAMP_SECRET` | if `opsramp` in `TARGETS` | OAuth2 client-credentials; `OPSRAMP_SERVICE_NAME` optional. |
| `HALO_API_URL` / `HALO_CLIENT_ID` / `HALO_CLIENT_SECRET`  | if `halo` in `TARGETS`       | OAuth2 client-credentials; `HALO_TENANT` / `HALO_TICKET_TYPE_ID` optional. |
| `SPLUNK_HEC_URL` / `SPLUNK_HEC_TOKEN`                 | if `splunk` in `TARGETS`     | HEC endpoint + token.                   |
| `GITHUB_REPO` / `GITHUB_TOKEN`                        | if `github` in `TARGETS`     | `owner/repo` + PAT (`issues:write`); `GITHUB_API_URL` (GHE) / `GITHUB_LABELS` optional. |
| `SLACK_WEBHOOK_URL`                                   | if `slack` in `TARGETS`      | Incoming Webhook URL; `SLACK_USERNAME` optional. |
| `TEAMS_WEBHOOK_URL`                                   | if `teams` in `TARGETS`      | Teams Workflows / Power Automate webhook URL. |
| `JIRA_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` / `JIRA_PROJECT_KEY` | if `jira` in `TARGETS` | Jira Cloud site + Basic auth; `JIRA_ISSUE_TYPE` / `JIRA_CLOSE_TRANSITION` / `JIRA_LABELS` optional. |
| `PAGERDUTY_ROUTING_KEY`                               | if `pagerduty` in `TARGETS`  | Events API v2 integration key; `PAGERDUTY_API_URL` (EU) optional. |
| `SENTINEL_WORKSPACE_ID` / `SENTINEL_SHARED_KEY`       | if `sentinel` in `TARGETS`   | Log Analytics workspace + key; `SENTINEL_LOG_TYPE` optional. |
| `DATADOG_API_KEY`                                     | if `datadog` in `TARGETS`    | API key; `DATADOG_SITE` / `DATADOG_TAGS` optional. |
| `ELASTIC_URL` / `ELASTIC_API_KEY`                     | if `elastic` in `TARGETS`    | Cluster URL + API key (or `ELASTIC_USER`/`ELASTIC_PASSWORD`); `ELASTIC_INDEX` optional. |
| `BMC_HELIX_URL` / `BMC_HELIX_USER` / `BMC_HELIX_PASSWORD` | if `bmc_helix` in `TARGETS` | AR System REST base + JWT auth; `BMC_HELIX_SERVICE_TYPE` / `BMC_HELIX_ASSIGNED_GROUP` / `BMC_HELIX_STATUS_RESOLVED` optional. |
| `DYNATRACE_URL` / `DYNATRACE_API_TOKEN`               | if `dynatrace` in `TARGETS`  | Environment API base + token (`events.ingest`); `DYNATRACE_ENTITY_SELECTOR` / `DYNATRACE_PROPERTIES` optional. |
| `GRAFANA_LOKI_URL` / `GRAFANA_LOKI_USER` / `GRAFANA_API_TOKEN` | if `grafana` in `TARGETS` | Grafana Cloud Logs (Loki) URL + user id + token (`logs:write`); `GRAFANA_LABELS` optional. |
| `WEBHOOK_URL`                                         | if `webhook` in `TARGETS`    | Optional `WEBHOOK_AUTH_HEADER`/`_VALUE`.|

> The relay uses a **Send**-scoped queue credential; the shim uses a
> **Listen**-scoped one — least privilege on both ends.

## Project layout

```
com-event-relay/
  relay/                      # public receiver (COM -> queue)
    app.py                  # FastAPI relay (cloud-agnostic)
    core/queue/
      base.py               # QueuePublisher interface + get_publisher() factory
      servicebus.py         # Azure Service Bus backend
      sqs.py                # AWS SQS backend
    Dockerfile
    requirements.txt
    .env.example
  shim/                       # outbound consumer (queue -> target)
    worker.py               # consume + normalise + dedup + forward loop
    core/
      queue/                # QueueConsumer interface + servicebus/sqs backends
    Dockerfile
    requirements.txt
    .env.example
  deploy/
    azure/deploy-relay-azure.sh
    aws/deploy-relay-aws.sh
  docker-compose.yml        # local dev/demo stack (relay + optional shim profile)
  README.md                 # this file (users)
```

> The shim image installs the shared **[com-event-core](../com-event-core)**
> package. CI workflows live at the **monorepo root**: `../.github/workflows/`.

## Companion: a single-box on-prem thin shim

If you cannot use any cloud service and prefer the simplest possible footprint,
there is an alternative to the relay + queue + shim split: a **single on-prem box**
that COM posts to directly. This is provided as a **separate, complementary
project — `com-event-bridge`** — one container that folds the whole path into a
single process:

```
COM ──► [ thin on-prem shim: handshake + auth → transform → forward ] ──► target
        (public DMZ HTTPS, no queue, no cloud)
```

It reuses the same COM verification handshake, shared-secret authentication, event
normalisation, and target adapters (OBM, ServiceNow, Splunk, generic webhook) as
this project, so behaviour toward COM and your target is identical. The difference
is that the bridge hosts the public TLS edge itself (no cloud, no queue) instead of
the managed relay + queue.

For the full relay-vs-bridge comparison — pros, cons, and when to pick each — see
the [root README](../README.md#which-project-do-i-use). Deployment details live in
the **[com-event-bridge](../com-event-bridge)** project.

## Roadmap

The roadmap is maintained once for the whole repo in the
[root README](../README.md#roadmap).
