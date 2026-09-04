# COM Event Relay

A generic, **container-first** relay that lets HPE Compute Ops Management (COM)
deliver webhook events into an environment **without exposing any internal
endpoint** — on **Azure or AWS**, from a single image.

> AI-generated reference implementation. Review and harden before production use.

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
   the body. The id is also returned to COM in the `x-relay-event-id` response
   header and written to the logs, giving you **end-to-end traceability**
   (COM → relay → queue → shim → target).

5. **Reliable hand-off.** The raw body is published to the queue. If the enqueue
   fails (transient broker issue), the relay returns `503` so **COM retries** —
   events are never silently dropped. A successful enqueue returns `202`.

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

## Choosing a deployment model

COM is a cloud (SaaS) service that **pushes** events over HTTPS to a public
endpoint you provide. How you receive those events — and how much infrastructure
you operate — depends on which model you choose. This section helps you decide.

Two facts shape every option:

- The component that COM talks to **must be publicly reachable** (public DNS name,
  valid TLS certificate, inbound `443`).
- The component that talks to **your target system** only needs **outbound**
  access — it never has to be exposed to the internet.

This project splits those two jobs into a **relay** (public, cloud-hosted) and a
**shim** (outbound-only, runs next to your target), so your internal network is
never exposed. Below are the models you can choose from, with pros and cons.

### Option A — Cloud relay + on-prem shim (recommended)

The relay and queue run in a managed cloud service (Azure Container Apps / AWS App
Runner + Service Bus / SQS); the shim runs **on-premises** next to your target
(OBM, ServiceNow Event Management, a local Splunk, or any webhook target). This is
the model this project is built for and the one most customers should choose.

**Pros**

- **No inbound ports on your network.** The shim connects *outbound* only; nothing
  in your data center is exposed to the internet.
- **Nothing to patch or secure at the edge.** The cloud platform provides the
  public URL, automatic TLS certificate (issuance + renewal), OS patching, and
  autoscaling.
- **No lost events.** A durable cloud queue buffers events, so if your target is
  down or slow, events wait safely and are delivered when it recovers.
- **Works for on-prem targets** that COM cannot reach directly.

**Cons**

- Requires a **small cloud footprint** (one managed container + one queue) in
  Azure or AWS.
- Events transit a cloud service you operate (though the payloads are COM hardware
  events, not your application data).

### Option B — Fully on-premises (no cloud at all)

If you cannot use any cloud service, everything — the public endpoint, the queue,
and the shim — runs in your own environment (typically in a DMZ).

**Pros**

- **No cloud dependency whatsoever**; all components stay within your
  infrastructure.
- Full control over where events flow and where they are stored.

**Cons**

- **You operate the public edge.** You must publish and secure an internet-facing
  HTTPS endpoint (reverse proxy such as nginx/Traefik as the TLS terminator).
- **You own the TLS certificate lifecycle** — issuing, renewing, and rotating a
  CA-signed certificate before it expires.
- **You own the operations** — firewall rules for inbound `443`, host hardening,
  OS/patch management, and high availability.
- **You provide the queue** — a self-hosted broker (RabbitMQ, Redis Streams,
  Kafka) instead of a managed cloud queue.

> This model reintroduces the very edge-hosting and certificate burden that the
> cloud relay removes. If your only requirement is an **on-prem target** (not
> forbidding cloud entirely), Option A gives you the same "no inbound exposure"
> benefit without operating a public endpoint. If you truly cannot run any cloud
> component, also see the **single-box thin shim** companion described below, which
> collapses everything into one simpler process.

### Option C — A different cloud (GCP, OCI, ...)

The same design runs on other clouds — the relay is a standard container and the
queue is pluggable. Today the project ships ready-made deployment for **Azure** and
**AWS**; running on **GCP** (e.g. Cloud Run + Pub/Sub), **OCI**, or Kubernetes is
supported by the architecture but needs a small amount of additional enablement
(a queue backend module for that cloud and a deployment script). The pros and cons
otherwise match Option A.

### At a glance

| Model | Public endpoint | Certificate & patching | Event buffering | Best for |
|---|---|---|---|---|
| **A — Cloud relay + on-prem shim** | Managed by the cloud | Managed for you | Durable cloud queue | Most customers; on-prem or unreachable targets |
| **B — Fully on-premises** | You host & secure it | You manage it | Self-hosted broker | Strict no-cloud mandates |
| **C — Another cloud** | Managed by that cloud | Managed for you | That cloud's queue | Standardizing on GCP/OCI/etc. |

If your target is **OpsRamp** or **ServiceNow incident creation**, you don't need
any of these models — use the native COM integration described
[above](#when-you-dont-need-this-native-com-integrations-opsramp-servicenow).

## Relay behavior

| Request                                                 | Response                             |
|---------------------------------------------------------|--------------------------------------|
| Handshake (`x-compute-ops-mgmt-verification-challenge`) | `200` + `{"verification":"<token>"}` |
| POST with valid `x-shim-secret`                         | `202` (enqueued) + `x-relay-event-id`|
| POST with missing/invalid secret                        | `401`                                |
| POST body larger than `MAX_BODY_BYTES`                  | `413`                                |
| POST but the queue enqueue fails (transient)            | `503` (COM retries)                  |
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

- **No silent drops.** A transient enqueue failure returns `503` so COM retries;
  a bad secret returns `401` and nothing is enqueued.

- **Rate limiting / DDoS is a platform concern.** The relay is deliberately
  stateless and does not implement per-client throttling (an in-app limiter can't
  coordinate across autoscaled replicas). Put a **WAF / rate limit in front**
  (Azure Front Door / AWS WAF) if you need it — that's the recommended layer for
  abuse protection.

- **Duplicate suppression is the shim's job.** De-duplication happens in the
  outbound shim (where per-event state is natural), keeping the relay thin and
  horizontally scalable.

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

### Deploy to AWS (App Runner)

```bash
export INSTANCE_ROLE_ARN=arn:aws:iam::123456789012:role/com-relay-sqs-send
./deploy/aws/deploy-relay-aws.sh
# prints the webhook URL + generated shared secret
```

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

- `TARGET` selects the adapter: `obm` | `servicenow` | `opsramp` | `halo` | `splunk` | `webhook`
- `QUEUE_BACKEND` selects the queue: `servicebus` | `sqs` (must match the relay)

For each message the shim: parses + **normalises** the COM event into a neutral
`CanonicalEvent`, **de-duplicates** it (local SQLite TTL store), **forwards** it
via the selected adapter, then **acknowledges** the message (complete on success;
abandon for retry on transient failure; dead-letter on malformed input). Adding a
new target is a small adapter that maps `CanonicalEvent` → the target's API — the
consume/dedup/retry core is shared.

> The normaliser, de-dup store, and target adapters live in the shared
> **[com-event-core](../com-event-core)** package (also used by
> [com-event-bridge](../com-event-bridge)), so a mapping or adapter fix is made
> once. The shim itself only adds the queue-consume loop. New adapters are
> contributed to `com-event-core`.

### Run the shim locally

```bash
cd shim
cp .env.example .env        # set TARGET + queue + target credentials
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
| `TARGET`                 | no                  | `obm` (default) / `servicenow` / `opsramp` / `halo` / `splunk` / `webhook`. |
| `QUEUE_BACKEND`          | no                  | `servicebus` (default) or `sqs` — must match relay.|
| `SERVICE_BUS_CONNECTION` | if azure            | **Listen**-scoped connection string.              |
| `QUEUE_NAME`             | if azure            | Queue to drain, e.g. `com-events`.                |
| `SQS_QUEUE_URL`          | if aws              | Full SQS queue URL.                               |
| `AWS_REGION`             | if aws              | Region of the queue.                              |
| `DEDUP_TTL_SECONDS`      | no                  | Dedup window. Default `3600`; `0` disables.       |
| `TARGET_TIMEOUT`         | no                  | Per-target HTTP timeout (s). Default `15`.        |
| `OBM_EVENT_API_URL` / `OBM_USER` / `OBM_PASSWORD`     | if `TARGET=obm`        | OBM Event REST API + Basic auth.        |
| `SNOW_INSTANCE` / `SNOW_USER` / `SNOW_PASSWORD`       | if `TARGET=servicenow` | `SNOW_TABLE` optional (`em_event` default / `incident`). |
| `OPSRAMP_API_URL` / `OPSRAMP_TENANT_ID` / `OPSRAMP_KEY` / `OPSRAMP_SECRET` | if `TARGET=opsramp` | OAuth2 client-credentials; `OPSRAMP_SERVICE_NAME` optional. |
| `HALO_API_URL` / `HALO_CLIENT_ID` / `HALO_CLIENT_SECRET`  | if `TARGET=halo`       | OAuth2 client-credentials; `HALO_TENANT` / `HALO_TICKET_TYPE_ID` optional. |
| `SPLUNK_HEC_URL` / `SPLUNK_HEC_TOKEN`                 | if `TARGET=splunk`     | HEC endpoint + token.                   |
| `WEBHOOK_URL`                                         | if `TARGET=webhook`    | Optional `WEBHOOK_AUTH_HEADER`/`_VALUE`.|

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
this project, so behaviour toward COM and your target is identical.

**Pros**

- **Simplest topology** — one container, no queue, no cloud account.
- **Everything stays on-premises**, fully under your control.

**Cons**

- **You host and secure a public endpoint** (reverse proxy as TLS terminator).
- **You own the certificate lifecycle** (issuing, renewing, rotating a CA-signed
  certificate) and the usual host operations (firewall, hardening, patching, HA).
- **No durable buffering.** Without a queue, delivery relies on in-process
  buffering and COM's own retries, so a prolonged target outage can risk dropped
  events unless you add a local on-disk spool.

**Choosing between the models:**

| Your situation | Recommended option |
|---|---|
| Target isn't natively supported by COM, and you want zero inbound exposure + durable delivery | **This project** — cloud relay + on-prem shim (Option A) |
| You cannot use any cloud, and prefer the simplest single-box setup over durability | **Single-box thin shim** (companion project) |
| Your target is OpsRamp or ServiceNow incident creation | Neither — use the native COM integration |

## Roadmap

- More target adapters (Datadog, PagerDuty, Elastic, Microsoft Sentinel, ...) —
  each is a small `CanonicalEvent` -> target mapping.
- Deploy scripts for the shim (Azure Container Instances / AWS ECS) and a
  systemd unit for bare on-prem hosts.
- Bicep / CloudFormation templates + "Deploy to Azure" / one-click AWS.
