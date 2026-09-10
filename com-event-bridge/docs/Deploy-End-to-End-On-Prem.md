# Deploy the single-box bridge on-prem — end-to-end runbook (GitHub Issues target)

A step-by-step guide to stand up the **single-box `com-event-bridge`** on your own
hardware and take it for a first real-world spin using the **GitHub Issues**
adapter: a COM *server health CRITICAL* opens an issue, and the matching
*recovery* closes it.

This is the on-prem, no-cloud counterpart of the relay runbooks
([Azure](../../com-event-relay/docs/Deploy-End-to-End-to-Azure.md) /
[AWS](../../com-event-relay/docs/Deploy-End-to-End-to-AWS.md)). Instead of a cloud
relay + queue + outbound shim, **one container** does the whole pipeline —
receive, transform, forward — with an on-disk **spool** as the durable buffer.

```
COM ──webhook──►  [ nginx :443 (TLS) ]──►  [ BRIDGE :8080 ]──► spool (SQLite)
(public HTTPS)      cert + rate-limit        handshake + secret        │
                    (you host this)          normalise + deliver       │ (background worker)
                                                                       ▼
                                                             GitHub Issues
                                                    (open on raise / close on clear)
```

**What you deploy where — all on one box you own**

| Piece | Role |
|-------|------|
| **nginx** (`:80`/`:443`) | Public TLS edge: terminates HTTPS with a CA-signed cert, rate-limits, proxies to the bridge on `127.0.0.1:8080`. |
| **certbot** | Issues + auto-renews the Let's Encrypt certificate (or bring your own corporate CA). |
| **bridge** (`:8080`, internal) | Answers COM's handshake, validates the shared secret, normalises the event, and delivers it — persisting to a local **spool** first so a target outage never loses an event. |
| **spool + dedup** (`/data`) | On-disk SQLite buffer (durable) + de-dup store, on a mounted volume. |

> **The trade-off vs the relay.** Here **you own the public edge**: a public DNS
> name, an inbound `443` open to COM, TLS/cert lifecycle, and host patching. In
> return there's **zero cloud footprint** — everything runs in one box in your
> DMZ. If you can't expose any inbound port, use the
> [cloud relay + outbound shim](../../com-event-relay/docs/Deploy-End-to-End-to-Azure.md)
> instead.

---

## Contents

- [0. Prerequisites](#0-prerequisites)
- [1. Prepare the target application](#1-prepare-the-target-application)
- [2. DNS + firewall (the public edge)](#2-dns--firewall-the-public-edge)
- [3. Configure the bridge](#3-configure-the-bridge)
- [4. Deploy the stack (bridge + nginx + certbot)](#4-deploy-the-stack-bridge--nginx--certbot)
- [5. Verify the bridge before wiring COM](#5-verify-the-bridge-before-wiring-com)
- [6. Configure the COM webhook](#6-configure-the-com-webhook)
- [7. End-to-end test](#7-end-to-end-test)
- [8. Troubleshooting](#8-troubleshooting)
- [9. Tear down](#9-tear-down)

---

## 0. Prerequisites

- **A Linux host in your DMZ** (VM or bare metal) with a **public IP** reachable
  from the internet on **`443`**, and **outbound** internet to your target
  (GitHub here). 1 vCPU / 1 GB RAM is plenty.
- **Docker + the Compose plugin** on that host:
  ```bash
  docker version        # Server section must print
  docker compose version
  ```
- **This repo cloned on the host** — the compose stack builds the image from
  source (it bundles the sibling `com-event-core` package), so you need the tree:
  ```bash
  git clone https://github.com/jullienl/HPE-COM-Event-Integrations.git
  cd HPE-COM-Event-Integrations/com-event-bridge
  ```
- A **public DNS name** you control (e.g. `com-bridge.example.com`) that you can
  point at the host's public IP (step 2).
- A **GitHub repository** you can create issues in (a throwaway repo is ideal for
  the test) and rights to create a **GitHub Personal Access Token** (step 1).
- Access to configure a **COM webhook** in the HPE GreenLake / Compute Ops
  Management console.

---

## 1. Prepare the target application

This runbook uses **GitHub Issues** as the example target. **Any other target**
(ServiceNow, Slack, Jira, a generic webhook, …) works the same way — only the
credential differs: follow **that application's own documentation** to generate
the API token / key / webhook URL it needs, then set that adapter's env vars in
the bridge `.env` (see [bridge/.env.example](../bridge/.env.example) for every
adapter's variables).

For the GitHub example:

1. Create (or pick) a repository, e.g. `your-org/com-issues`.
2. Create a **fine-grained PAT**: GitHub → *Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate new token*.
   - **Repository access:** *Only select repositories* → pick your repo.
   - **Permissions → Repository permissions → Issues: Read and write.**
   - (Classic PAT alternative: the `repo` scope also works.)
3. Copy the token — you'll put it in the bridge `.env` as `GITHUB_TOKEN` (step 3).

The GitHub adapter reads: `GITHUB_REPO` (required, `owner/repo`), `GITHUB_TOKEN`
(required), and optionally `GITHUB_API_URL` (GitHub Enterprise Server) and
`GITHUB_LABELS` (extra labels on new issues).

---

## 2. DNS + firewall (the public edge)

Because the bridge **is** the public endpoint, COM has to reach it over public
HTTPS with a valid certificate. Before deploying:

1. **Point DNS at the host.** Create an `A`/`AAAA` record for your name (e.g.
   `com-bridge.example.com`) → the host's public IP. Confirm it resolves:
   ```bash
   dig +short com-bridge.example.com
   ```
2. **Open inbound `443`** (and `80`, needed once for the ACME HTTP-01 challenge)
   from the internet to the host. Ideally restrict the source to COM's egress
   ranges — but note HPE documents these as **infrastructure-managed and subject
   to change**, so pin them cautiously and revisit:
   [GreenLake webhooks FAQ → source IPs](https://developer.greenlake.hpe.com/docs/greenlake/guides/public/frequently_asked_questions/webhook_faq).
3. **Open outbound `443`** to your target (`api.github.com` here).

> Everything else stays closed — the bridge app itself binds `127.0.0.1:8080` and
> is only reachable through nginx.

---

## 3. Configure the bridge

Create the bridge config from the template and fill in the essentials:

```bash
cp bridge/.env.example bridge/.env
```

Edit `bridge/.env` and set at least:

```ini
# --- COM authentication ---------------------------------------------------
# Long random secret COM must present on every event:
#   openssl rand -hex 32
COM_SHARED_SECRET=<paste 64-hex secret>
SHARED_SECRET_HEADER=x-shim-secret

# --- Delivery mode --------------------------------------------------------
# spool (default, durable): persist locally, ack COM immediately, background
# worker drains + retries. Needs a durable SPOOL_PATH — the image defaults it to
# /data/spool.db and the compose stack mounts a volume there, so leave it.
DELIVERY_MODE=spool
SPOOL_PATH=/data/spool.db

# --- Target ---------------------------------------------------------------
TARGETS=github
GITHUB_REPO=your-org/com-issues
GITHUB_TOKEN=<your-fine-grained-PAT>

# --- Server monitored conditions ------------------------------------------
# health (default) / power / connection / subscription — comma-separated.
SERVER_MONITORS=health
```

- **`COM_SHARED_SECRET`** — generate it now and keep it; you'll paste it into the
  COM webhook in step 6. `openssl rand -hex 32` gives a 64-hex value.
- **`DELIVERY_MODE=spool`** is the safe default and the reason to run the bridge:
  it acks COM with `202` immediately and a background worker retries delivery, so
  a GitHub outage never loses an event and never degrades the COM webhook. It
  **requires** a durable `SPOOL_PATH`; the bridge **refuses to start** if
  `DELIVERY_MODE=spool` and `SPOOL_PATH` is unset (an ephemeral path would
  silently lose the backlog on restart). The image + compose already wire
  `/data/spool.db` on a named volume, so the default just works.
- **`SERVER_MONITORS`** picks which server conditions open/close an item. A
  condition you **don't** list is simply not monitored — and **each one you add
  needs a matching COM webhook** (step 6). See the
  [Env vars reference](#env-vars-reference) below.

> **Secrets from files (vault) — recommended for production.** Every sensitive
> value (`COM_SHARED_SECRET`, `GITHUB_TOKEN`, any adapter token) also accepts a
> **`<NAME>_FILE`** form: point it at a file the bridge reads at startup instead
> of the plain env var. File-backed secrets don't leak via `docker inspect`,
> `/proc/<pid>/environ`, or child processes. The compose file has a commented
> `secrets:` block that mounts them at `/run/secrets/<name>`; full wiring (Docker
> secrets, systemd `LoadCredential`, Vault Agent, K8s CSI) is in the bridge
> README's [Secrets management](../README.md#secrets-management).

### Env vars reference

Key env vars (full list in [bridge/.env.example](../bridge/.env.example)):

| Var | Value here | Notes |
|-----|-----------|-------|
| `COM_SHARED_SECRET` | your 64-hex secret | Also accepts `COM_SHARED_SECRET_FILE`. |
| `SHARED_SECRET_HEADER` | `x-shim-secret` | Header COM sends the secret in; must match the webhook. |
| `MAX_BODY_BYTES` | `262144` (default) | 256 KB body cap → `413` if exceeded (also enforced in nginx). |
| `DELIVERY_MODE` | `spool` | `spool` (durable, default) or `sync` (best-effort, lossy). |
| `SPOOL_PATH` | `/data/spool.db` | **Required in spool mode**; must be on the mounted volume. |
| `SPOOL_MAX_BYTES` | `52428800` (default) | 50 MB backlog cap → `503` backpressure over it. |
| `TARGETS` | `github` | One name, or comma-separated to fan out (`github,slack`). |
| `GITHUB_REPO` / `GITHUB_TOKEN` | your repo + PAT | `GITHUB_REPO` is the **`owner/repo` slug only**, not a URL. Token needs **Issues: read/write**. |
| `SERVER_MONITORS` | `health` | Watch server health (default). Add more as a **comma-separated** list — `health,power,connection,subscription`. **Each monitor you add needs a matching COM webhook** targeting this bridge. **A condition you *don't* list is simply not monitored** — no item opens or closes for it and no error is raised. |
| `DEDUP_TTL_SECONDS` | `3600` (default) | Suppresses duplicate/redelivered events within the window. |

---

## 4. Deploy the stack (bridge + nginx + certbot)

The [docker-compose.yml](../docker-compose.yml) stack runs three services:
**bridge** (internal `:8080`), **nginx** (public `:80`/`:443`, TLS), and
**certbot** (issues + auto-renews the cert). The bridge's spool + dedup live on
the `bridge-data` volume mounted at `/data`.

**1. Set your hostname in the nginx config.** Edit
[deploy/nginx/com-event-bridge.conf](../deploy/nginx/com-event-bridge.conf) and
replace every `com-bridge.example.com` with your DNS name (the `server_name` and
the two `ssl_certificate*` paths):

```bash
sed -i 's/com-bridge.example.com/<your-fqdn>/g' deploy/nginx/com-event-bridge.conf
```

**2. Issue the first certificate (one-time bootstrap, HTTP-01).** nginx must be up
on `:80` to serve the ACME challenge before the cert exists:

```bash
# Start nginx alone so it can answer the ACME challenge on :80
docker compose up -d nginx

# Issue the certificate (replace host + email)
docker compose run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  -d <your-fqdn> \
  --email ops@example.com --agree-tos --no-eff-email
```

**3. Bring up the full stack:**

```bash
docker compose up -d --build
```

certbot renews the cert twice daily; after a renewal reload nginx to pick it up
(add a cron/hook, or restart nginx on a schedule):

```bash
docker compose exec nginx nginx -s reload
```

> **Corporate CA instead of Let's Encrypt?** Drop the `certbot` service and mount
> your own `fullchain.pem` / `privkey.pem` into the nginx cert paths, then track
> expiry in your own PKI. See [HARDENING.md](../HARDENING.md).

Confirm all three services are healthy:

```bash
docker compose ps          # bridge (healthy), nginx, certbot all Up
docker compose logs bridge --tail 20
```

The bridge log should show `started in spool mode; 0 event(s) already pending`.

---

## 5. Verify the bridge before wiring COM

Check the app locally on the host (through nginx and directly), then over public
HTTPS from anywhere.

**Liveness / readiness** (readiness is `503` until the spool worker is alive):

```bash
curl -s https://<your-fqdn>/healthz     # {"status":"ok"}
curl -s http://127.0.0.1:8080/readyz    # {"status":"ready"}  (direct to app)
```

**Handshake** — COM proves it owns the endpoint via a `GET` with a challenge
header; the bridge echoes it back:

```bash
curl -s https://<your-fqdn>/com/webhook \
  -H "x-compute-ops-mgmt-verification-challenge: hello123"
# → {"verification": "hello123"}
```

**Auth check** — a POST without the secret must be rejected `401`; with the
correct header it returns `202` and spools a message:

```bash
# Expect 401 (no secret)
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<your-fqdn>/com/webhook \
  -H "content-type: application/json" -d '{}'

# Expect 202 (valid secret) — spools a minimal but VALID-JSON test message
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<your-fqdn>/com/webhook \
  -H "content-type: application/json" \
  -H "x-shim-secret: <COM_SHARED_SECRET>" -d '{}'
```

> A malformed body is rejected with `400` (not spooled). The bridge validates the
> secret with a constant-time compare and caps the body at `MAX_BODY_BYTES`
> (default 256 KB → `413`), which nginx also enforces (`client_max_body_size`).
>
> **This `202` test spools one real message.** When the worker runs it delivers
> this `{}`; with no `type` it maps via the normaliser's generic branch to a single
> harmless "COM event" item you can close. (A payload without a `type`/`hardware`
> block is handled on purpose — nothing is dropped. Unlike the cloud relay the
> bridge **parses** the body at ingress, so an invalid one is rejected `400` up
> front and never reaches the spool.)

---

## 6. Configure the COM webhook

> **Webhooks are created via the COM API, not the console UI.** The GreenLake /
> Compute Ops Management console does **not** expose webhook creation today, so
> you register the webhook with a `POST` to the COM webhooks API. The easiest way
> is the ready-made **"Create webhook"** requests in the public Postman
> collection:
>
> [Lionel Jullien's public workspace → HPE Compute Ops Management (v2) → Webhooks](https://www.postman.com/jullienl/lionel-jullien-s-public-workspace/)
>
> Fork/import that collection, set your COM API token, and run one of the
> **Create webhook** calls with the values below.
>
> **New to COM's API or Postman?** The collection's **Overview** page includes an
> **initial setup guide** (create an HPE GreenLake API client, get an access
> token, configure the Postman environment) — do that first.
>
> For a deeper walkthrough of COM webhooks — how to create one, the available
> event **filter** options, and the resources they cover — see the blog post
> [Implementing webhooks with COM](https://jullienl.github.io/Implementing-webhooks-with-COM/).

Register a webhook with these settings:

- **Destination URL:** `https://<your-fqdn>/com/webhook`
- **Custom header:** name `x-shim-secret` (your `SHARED_SECRET_HEADER`), value =
  your `COM_SHARED_SECRET`. COM authenticates with a **static header only**.
- **Event filter (`eventFilter`):** scope it to servers so only relevant events
  flow. Filtering happens **server-side at COM**; the bridge forwards whatever COM
  sends. **The resource type you filter on must match the bridge's
  `SERVER_MONITORS` (step 3):** this runbook watches the **server** resource, so
  filter on server events.

A typical **Create webhook** call (replace the destination host with your
`<your-fqdn>` and the secret with your `COM_SHARED_SECRET`):

```http
POST https://<COM-API-base-URL>/compute-ops-mgmt/<webhooks-API-version>/webhooks
```
```json
{
    "name": "On-prem bridge - Webhook event for servers that get unhealthy",
    "destination": "https://com-bridge.example.com/com/webhook",
    "state": "ENABLED",
    "eventFilter": "type eq 'compute-ops/server' and old/hardware/health/summary eq 'OK' and changed/hardware/health/summary eq True",
    "headers": {
        "x-shim-secret": "<your COM_SHARED_SECRET>"
    }
}
```

**Create a second webhook for the recovery (clear).** A COM webhook only fires for
the transition its `eventFilter` selects, so the one above delivers *only* the
**raise** (health left `OK`). To have the bridge **close** the GitHub issue when
the server becomes healthy again, register a **second** webhook pointing at the
**same** bridge URL with the same secret header, filtering on the **opposite**
transition (health returned to `OK`):

```http
POST https://<COM-API-base-URL>/compute-ops-mgmt/<webhooks-API-version>/webhooks
```
```json
{
    "name": "On-prem bridge - Webhook event for servers that recover",
    "destination": "https://com-bridge.example.com/com/webhook",
    "state": "ENABLED",
    "eventFilter": "type eq 'compute-ops/server' and new/hardware/health/summary eq 'OK' and changed/hardware/health/summary eq True",
    "headers": {
        "x-shim-secret": "<your COM_SHARED_SECRET>"
    }
}
```

The difference is `old/...` (raise: was `OK`, so it **left** `OK`) vs `new/...`
(clear: is now `OK`, so it **returned** to `OK`). Both events carry the same
`correlation_key` (`server:<serial>:health`), so the clear closes exactly the
issue the raise opened. **Without the clear webhook, issues open but never close.**

COM first calls `GET` (the handshake in step 5) and only enables the webhook once
it echoes the challenge over public HTTPS with a valid certificate — nginx +
certbot provide that TLS.

**Verify the webhook was created and enabled.** Run the **Get webhooks** call (or
`GET https://<COM-API-base-URL>/compute-ops-mgmt/<webhooks-API-version>/webhooks`).
It should report:

```json
"state": "ENABLED",
"status": "ACTIVE"
```

`ENABLED` means the handshake succeeded; `ACTIVE` means COM will deliver events.
If you see `DISABLED` / `ERROR`, the handshake or recent deliveries failed —
recheck the destination URL, the certificate, and the bridge health (step 5).

> Keep the webhook **healthy**: COM disables a webhook after **10 consecutive
> non-2xx** responses. In `spool` mode the bridge returns `202` as soon as the
> event is persisted, so a slow/broken GitHub target never affects webhook
> health — that decoupling is the whole point of the spool.

> **Multiple webhooks, one bridge.** The bridge exposes a single URL and just
> **accepts whatever COM sends** — it dispatches on each payload's `type`. So you
> can register **several webhooks, each with a different `eventFilter`/resource
> type** (e.g. one on `server`, one on `alert`), **all pointing at this same
> bridge URL** with the same secret header. `SERVER_MONITORS` only tunes the
> `server` branch — an `alert` webhook is unaffected by it.

---

## 7. End-to-end test

**Path A — real COM event.** Trigger (or wait for) a server health change that
matches your `eventFilter`. Within a few seconds you should see:
1. Bridge logs a `202` (event spooled) then the worker logs `event <id> delivered
   from spool`.
2. A new **issue** appears in your repo, labelled `com:server:<serial>:health`.
When the server returns to healthy, COM sends a *clear*; the bridge finds the open
issue by that label and **closes** it (with a recovery comment).

**Path B — synthetic event (no waiting).** Post a sample server snapshot straight
at the bridge to exercise the whole chain: a `raise` snapshot (unhealthy) opens an
issue, a `clear` snapshot (healthy) closes it. The two fixtures are just **static
JSON** — write them with your shell (no Python needed), then POST them. Pick the
block for your shell.

> **These are `server` *health* payloads — not `alert` payloads.** The fixtures
> use `type: compute-ops-mgmt/server` with a `hardware/health` block, so they
> exercise the **server health** condition. That means this test is only valid
> when the bridge runs with `SERVER_MONITORS=health` (step 3) — the default — and
> mirrors a **compute/server** webhook (step 4, `type eq 'compute-ops/server'`),
> **not** an `alert` webhook. An `alert` payload has a completely different shape
> and normalises down a different branch, so it would neither open nor close an
> issue via the health condition. If you changed `SERVER_MONITORS` to something
> without `health`, this snapshot is (correctly) ignored — post a fixture for a
> condition you *do* monitor instead.

**Linux/macOS:**

```bash
# 1. Write the two fixtures — a raise (health CRITICAL) and a clear (health OK).
cat > raise.json <<'JSON'
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
JSON

cat > clear.json <<'JSON'
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
    "health": { "summary": "OK", "fans": "OK", "powerSupplies": "OK" }
  }
}
JSON

# 2. POST the fixtures at the bridge (replace the <placeholders> with your values).
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<your-fqdn>/com/webhook \
  -H "content-type: application/json" -H "x-shim-secret: <COM_SHARED_SECRET>" --data "@raise.json"    # → issue opens
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<your-fqdn>/com/webhook \
  -H "content-type: application/json" -H "x-shim-secret: <COM_SHARED_SECRET>" --data "@clear.json"    # → same issue closes
```

**PowerShell (Windows):**

```powershell
# 1. Write the two fixtures — a raise (health CRITICAL) and a clear (health OK).
@'
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
'@ | Set-Content -Encoding ascii raise.json

@'
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
    "health": { "summary": "OK", "fans": "OK", "powerSupplies": "OK" }
  }
}
'@ | Set-Content -Encoding ascii clear.json

# 2. POST the fixtures at the bridge (replace the <placeholders> with your values).
curl.exe -s -o NUL -w "%{http_code}`n" -X POST "https://<your-fqdn>/com/webhook" `
  -H "content-type: application/json" -H "x-shim-secret: <COM_SHARED_SECRET>" --data "@raise.json"    # → issue opens
curl.exe -s -o NUL -w "%{http_code}`n" -X POST "https://<your-fqdn>/com/webhook" `
  -H "content-type: application/json" -H "x-shim-secret: <COM_SHARED_SECRET>" --data "@clear.json"    # → same issue closes
```

> These fixtures are just static JSON copied from the project's shipped sample
> builder. If you'd rather **generate** them (or see how they *normalise* into
> `CanonicalEvent`s) with Python, run `python com-event-core/examples/dump_payloads.py
> server raise` from a venv (`pip install ./com-event-core`) — it prints the raw
> COM payload plus the resulting events.

**What you should see.** The `raise` opens a GitHub issue and the `clear` closes
the **same** issue:

<img src="../../docs/images/com-event-path-b-github-issue.png" alt="Path B result: a GitHub issue titled 'Server ESX-node-01 health CRITICAL', labelled com:server:CZ2311004G:health, opened on the raise and closed as completed with a 'Resolved by COM clear event' comment" width="900" />

The title (**Server ESX-node-01 health CRITICAL**), the
**`com:server:CZ2311004G:health`** label (the correlation key that ties the raise
to the clear), the body listing the non-OK component (`powerSupplies=CRITICAL`),
the **"Resolved by COM clear event"** comment, and the **Closed as completed**
state all come straight from the two fixtures above.

> First *clear* can occasionally race GitHub's search index (~1s lag); if the
> issue isn't found the spool row is rescheduled and the retry closes it — no lost
> events.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| COM won't enable the webhook | Handshake failed | Confirm `GET /com/webhook` echoes the challenge over **public HTTPS** with a valid cert (step 5). Check DNS resolves and the URL ends in `/com/webhook`. |
| Cert issuance fails | `:80` not reachable / DNS wrong | ACME HTTP-01 needs inbound `80` open and DNS pointing at the host. Recheck step 2, then re-run the `certbot certonly` command. |
| Bridge **won't start** | `DELIVERY_MODE=spool` but `SPOOL_PATH` unset | The bridge fails fast by design. Keep `SPOOL_PATH=/data/spool.db` and the `bridge-data:/data` volume (both are defaults), or set `DELIVERY_MODE=sync` for best-effort. |
| Bridge returns `401` | Wrong/missing header | Header **name** must equal `SHARED_SECRET_HEADER` (`x-shim-secret`) and value must equal `COM_SHARED_SECRET`. |
| Bridge returns `413` | Body too large | Raise `MAX_BODY_BYTES` **and** nginx `client_max_body_size` together if you genuinely send large payloads. |
| Bridge returns `503` on POST | Spool full (backpressure) or `sync` target down | In spool mode: backlog exceeded `SPOOL_MAX_BYTES` — the target has been down; check the worker logs. In sync mode: the target is unreachable (event lost — prefer spool). |
| `/readyz` returns `503` | Spool worker died | Check `docker compose logs bridge`; restart the service. |
| No GitHub issue appears | Token/repo/scope | Verify `GITHUB_REPO=owner/repo` and the PAT has **Issues: read/write**. Watch the bridge logs for the forward error. |
| Issue opens but never closes | Clear not delivered / label mismatch | Confirm a *clear* event fired; the bridge matches the open issue by its `com:<correlation_key>` label. |
| Webhook shows WARNING/ERROR in COM | Repeated non-2xx from the bridge | In spool mode the bridge returns `202` fast; if you see `5xx`, check the spool/worker — sustained failures **disable** the webhook. |

Handy commands:

```bash
# Follow the bridge logs (structured JSON, one object per line)
docker compose logs bridge --follow

# Is the app ready? (spool worker alive)
curl -s http://127.0.0.1:8080/readyz

# How big is the spool backlog right now?
docker compose exec bridge python -c \
  "import sqlite3; print(sqlite3.connect('/data/spool.db').execute('select count(*), coalesce(sum(length(body)),0) from spool').fetchone())"
# Example output — 1 event still pending in the spool, 376 bytes total:
#   (1, 376)
# Drops to (0, 0) once the worker delivers it. A count that only grows means the
# worker can't deliver (target down / misconfig) — check the bridge logs above.

# nginx / cert issues
docker compose logs nginx --tail 50
docker compose logs certbot --tail 50
```

> **Not a problem — expected log noise.** These bridge log lines look alarming but
> are healthy:
> - **`event_type": "unknown"` on an accepted event** — the bridge reads the type
>   from COM's `x-compute-ops-mgmt-event-type` header and defaults to `unknown`
>   when it's absent (any synthetic/manual POST, or a caller that omits it). It
>   doesn't affect delivery — the real classification happens in the normaliser
>   from the body. `unknown` + `202` = accepted and spooled correctly.
> - **`GET / HTTP/1.1 404 Not Found`** — the bridge only serves `/com/webhook`,
>   `/healthz`, `/readyz`; hitting the base URL (browser, uptime pinger, port scan)
>   correctly returns `404`. Harmless. (There's no queue/AMQP here, so none of the
>   Service Bus connection-churn spam the Azure relay logs applies.)

---

## 9. Tear down

```bash
docker compose down            # stop the stack, keep the volumes (spool/dedup/certs)
docker compose down -v         # also delete the bridge-data + cert volumes
```

Deleting the volumes removes the spool DB, dedup DB, and the issued certificate.
Then remove the DNS record and close the inbound firewall rules.

---

### Where this maps in the code

- Bridge app + endpoints: [bridge/app.py](../bridge/app.py) (`/com/webhook`, `/healthz`, `/readyz`)
- Delivery mode + fail-fast spool check: [bridge/app.py](../bridge/app.py)
- Spool store + retry worker: [bridge/core/spool.py](../bridge/core/spool.py)
- Bridge config: [bridge/.env.example](../bridge/.env.example)
- TLS reverse proxy: [deploy/nginx/com-event-bridge.conf](../deploy/nginx/com-event-bridge.conf)
- Full DMZ stack: [docker-compose.yml](../docker-compose.yml)
- Bare-host systemd unit: [deploy/systemd/com-event-bridge.service](../deploy/systemd/com-event-bridge.service)
- Hardening (DMZ, TLS, certs, secrets, HA): [HARDENING.md](../HARDENING.md)
- GitHub adapter: [../../com-event-core/com_event_core/adapters/github.py](../../com-event-core/com_event_core/adapters/github.py)
