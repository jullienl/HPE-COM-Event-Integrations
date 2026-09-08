# Deploy the cloud relay to Azure — end-to-end runbook (GitHub Issues target)

A step-by-step guide to stand up the **cloud relay + on-prem shim** on
Azure and take it for a first real-world spin using the **GitHub Issues**
adapter: a COM *server health CRITICAL* opens an issue, and the matching
*recovery* closes it.

```
COM ──webhook──►  [ RELAY on Azure Container Apps ]──►  Azure Service Bus queue
(cloud, public)         GET handshake + x-shim-secret         │
                                                              │ (outbound-only)
                              [ SHIM near you ]──drains queue──┘
                                     │
                                     └──►  GitHub Issues  (open on raise / close on clear)
```

**What you deploy where**

| Piece | Where | Image | Role |
|-------|-------|-------|------|
| Relay | Azure Container Apps (public HTTPS) | `ghcr.io/jullienl/com-event-relay` | Answers COM handshake, validates the shared secret, enqueues events. |
| Queue | Azure Service Bus (Standard) | — | Durable buffer between receive and deliver. |
| Shim | Anywhere with **outbound** internet (your laptop/VM/on-prem) | `ghcr.io/jullienl/com-event-shim` | Drains the queue, forwards to GitHub. **No inbound ports.** |

> Why the split: the relay owns the only public endpoint; the shim reaches the
> queue and GitHub **outbound-only**, so nothing inbound touches your network.

---

## 0. Prerequisites

- **This repo cloned locally** (both the script and the synthetic-event test in
  step 6 use files from it):
  ```powershell
  git clone https://github.com/jullienl/HPE-COM-Event-Integrations.git
  cd HPE-COM-Event-Integrations
  ```
- **Azure CLI** logged in to the target subscription:
  ```powershell
  az login
  az account set --subscription "<your-subscription-id-or-name>"   # Use az account list --output table --refresh to see your subscriptions
  az account show --query "{sub:name, id:id}" -o table
  ```
- **Azure CLI Container Apps extension** (installed automatically below, but you can pre-add it):
  ```powershell
  az extension add --name containerapp --upgrade
  az provider register --namespace Microsoft.App
  az provider register --namespace Microsoft.ServiceBus
  ```
- **Docker** on the machine that will run the **shim** (to run the shim container). The relay itself needs no local Docker — Azure pulls its image.
- A **GitHub repository** you can create issues in (a throwaway repo is ideal for the test).
- Rights to create a **GitHub Personal Access Token** (see step 1).
- Access to configure a **COM webhook** in the HPE GreenLake / Compute Ops Management console.

> Region note: this runbook uses `westeurope`. Override `LOC` if you prefer another region.

---

## 1. Prepare the target application

This runbook uses **GitHub Issues** as the example target, so the steps below
create a repository and a token for it. **Any other target** (ServiceNow, Slack,
Jira, a generic webhook, …) works the same way — only the credential differs:
follow **that application's own documentation** to generate the API token / key /
webhook URL it needs, then pass it to the shim via that adapter's env vars (see
[shim/.env.example](../shim/.env.example) for every adapter's variables).

For the GitHub example:

1. Create (or pick) a repository, e.g. `your-org/com-lab-issues`.
2. Create a **fine-grained PAT**: GitHub → *Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate new token*.
   - **Repository access:** *Only select repositories* → pick your repo.
   - **Permissions → Repository permissions → Issues: Read and write.**
   - (Classic PAT alternative: the `repo` scope also works.)
3. Copy the token — you'll pass it to the **shim** later (step 5) as `GITHUB_TOKEN`
   (nothing GitHub-related is configured on the relay; the relay never talks to GitHub).

The GitHub adapter reads: `GITHUB_REPO` (required, `owner/repo`), `GITHUB_TOKEN`
(required), and optionally `GITHUB_API_URL` (GitHub Enterprise Server) and
`GITHUB_LABELS` (extra labels on new issues).

---

## 2. Choose how to deploy the relay

With the prerequisites done and your target credential in hand, provision the
relay + queue **one of two ways**. The COM/target wiring afterwards
(steps 3–6) is identical either way.

### Option A — Scripted (fastest)

The relay + queue provisioning is scripted in
[deploy/azure/deploy-relay-azure.sh](../deploy/azure/deploy-relay-azure.sh). From
**Azure Cloud Shell** (bash) or **WSL/Git Bash** on Windows, in your clone:

```bash
cd com-event-relay/deploy/azure

# Run it (override any default via env vars on the same line)
RG=rg-com-relay LOC=westeurope bash deploy-relay-azure.sh
```

It prints the **Webhook URL** and the generated **shared secret** at the end —
copy both (you'll need them for COM in step 4). It creates only the **send**
policy, so add the `shim-listen` rule (Option B, step 2) before running the shim, then
**skip to step 3 (verify)**.

### Option B — Manual walkthrough (recommended for a first deploy)

#### 1 - Set the variables

First set the working variables you'll reuse throughout — run these in a
**PowerShell** terminal (locally, or the **PowerShell** option in Azure Cloud Shell):

```powershell
$RG      = "rg-com-relay"                                            # Azure resource group that holds every resource below
$LOC     = "westeurope"                                              # Azure region to deploy into (override if you prefer another)
$SB_NS   = "sbcomrelay$([System.Random]::new().Next(10000,99999))"   # Service Bus namespace name — must be GLOBALLY unique (random suffix)
$QUEUE   = "com-events"                                              # Service Bus queue name; the relay and shim must both use this value
$ACA_ENV = "aca-com-relay"                                           # Container Apps environment (the shared host for the relay app)
$APP     = "com-event-relay"                                         # Container App name for the relay
$IMAGE   = "ghcr.io/jullienl/com-event-relay:latest"                 # relay container image pulled by Azure (published by CI to GHCR)
$HDR     = "x-shim-secret"                                           # HTTP header COM sends carrying the shared secret (auth on every POST)

# 32-byte (64 hex char) shared secret, no openssl needed on Windows:
$SECRET  = -join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) })
$SECRET   # copy this — COM will send it on every POST
```

> Keep `$SECRET` safe. You'll paste it into the COM webhook definition in step 4.

Then run the two sub-steps below, then continue to **step 3 (verify)**.

#### 2. Create the Service Bus queue + two least-privilege policies

The relay publishes with a **Send**-only key; the shim consumes with a
**Listen**-only key. Two separate keys = least privilege on each end.

```powershell
# Resource group — the container for every resource created below
az group create --name $RG --location $LOC -o none

# Service Bus namespace (Standard SKU — queues need Standard, not Basic)
az servicebus namespace create --resource-group $RG --name $SB_NS `
  --location $LOC --sku Standard -o none

# The queue itself — the durable buffer between relay (send) and shim (receive)
az servicebus queue create --resource-group $RG --namespace-name $SB_NS `
  --name $QUEUE -o none

# Relay: SEND-only authorization rule (the relay may only enqueue, not read)
az servicebus queue authorization-rule create --resource-group $RG `
  --namespace-name $SB_NS --queue-name $QUEUE --name relay-send --rights Send -o none

# Shim: LISTEN-only authorization rule (the shim may only receive, not enqueue)
az servicebus queue authorization-rule create --resource-group $RG `
  --namespace-name $SB_NS --queue-name $QUEUE --name shim-listen --rights Listen -o none

# Grab both connection strings (each carries its own scoped key)
# relay's send-only connection string -> relay app
$SB_SEND = az servicebus queue authorization-rule keys list --resource-group $RG `
  --namespace-name $SB_NS --queue-name $QUEUE --name relay-send `
  --query primaryConnectionString -o tsv

# shim's listen-only connection string -> shim container
$SB_LISTEN = az servicebus queue authorization-rule keys list --resource-group $RG `
  --namespace-name $SB_NS --queue-name $QUEUE --name shim-listen `
  --query primaryConnectionString -o tsv
```

#### 3. Deploy the relay to Azure Container Apps

```powershell
# Container Apps environment — the shared, managed host the relay app runs in.
# NOTE: this first run is SLOW (~2-5 min): Azure provisions the underlying
# managed Kubernetes control plane + a Log Analytics workspace + networking.
# It's a one-time setup and not hung — please be patient and let it finish.
az containerapp env create --resource-group $RG --name $ACA_ENV --location $LOC -o none

# Create the relay app and wire its config in one call:
#   --image                : relay image pulled from GHCR (set above)
#   --ingress/--target-port: public HTTPS in, forwarded to the app's port 8080
#   --min/--max-replicas   : keep >=1 warm (COM never retries); scale up to 3 under load
#   --secrets              : store the SB send-string + shared secret as named secrets
#   secretref:<name>       : env var resolves from a named secret above (not stored inline)
#   QUEUE_BACKEND          : tell the relay to publish to Azure Service Bus
#   SHARED_SECRET_HEADER   : header name the relay checks on each POST
#   QUEUE_NAME             : queue to publish to (must match the shim)
az containerapp create `
  --resource-group $RG --name $APP --environment $ACA_ENV `
  --image $IMAGE `
  --ingress external --target-port 8080 `
  --min-replicas 1 --max-replicas 3 `
  --secrets "sb-conn=$SB_SEND" "com-secret=$SECRET" `
  --env-vars `
    "QUEUE_BACKEND=servicebus" `
    "SERVICE_BUS_CONNECTION=secretref:sb-conn" `
    "COM_SHARED_SECRET=secretref:com-secret" `
    "SHARED_SECRET_HEADER=$HDR" `
    "QUEUE_NAME=$QUEUE" `
  -o none

# Fetch the public hostname Azure assigned to the app
$FQDN = az containerapp show --resource-group $RG --name $APP `
  --query properties.configuration.ingress.fqdn -o tsv

"Webhook URL : https://$FQDN/com/webhook"   # give this URL to COM (step 4)
"Secret hdr  : $HDR = $SECRET"               # COM sends this header/value on every POST
```

> **`--min-replicas 1` is deliberate.** COM sends **one POST per event and never
> retries**; if the relay had scaled to zero, the cold-start could drop that single
> delivery. Keeping one warm replica means the handshake and every event are
> always answered fast.

> **Which image?** This runbook uses the project's prebuilt relay image
> `ghcr.io/jullienl/com-event-relay:latest` — it's public and already contains
> everything (handshake, secret check, queue publisher), so **just use it**; that's
> the whole point. You only need your own image if you've forked and changed the
> relay code. In that case, if your fork's package is **private**, add registry
> credentials to the `create` call so Azure can pull it:
> ```powershell
>   --registry-server ghcr.io `
>   --registry-username <your-github-username> `
>   --registry-password <a-PAT-with-read:packages>
> ```

**Shortcut (bash):** prefer not to run these steps by hand? Use the
[deploy-relay-azure.sh](../deploy/azure/deploy-relay-azure.sh) script from
[Option A](#option-a--scripted-fastest) in step 2.

---

## 3. Verify the relay before wiring COM

Run these from the **same PowerShell terminal** you used in step 2 — they reuse
`$FQDN`, `$HDR` and `$SECRET` from there. If you deployed via **Option A** (the
script) or opened a fresh terminal, set them first from the values the script /
step 2 printed:

```powershell
$FQDN   = "<the FQDN from step 2>"          # e.g. com-event-relay.xxxx.westeurope.azurecontainerapps.io
$HDR    = "x-shim-secret"                   # the header name COM sends
$SECRET = "<the shared secret from step 2>" # the 64-hex secret printed at deploy time
```



**Liveness / readiness** (readiness returns `503` until the queue is reachable):

```powershell
curl.exe -s "https://$FQDN/healthz"
curl.exe -s "https://$FQDN/readyz"
```
  > **`curl.exe` vs `curl`:** the commands above use `curl.exe`, which is correct on
  > **Windows PowerShell** (there plain `curl` is an alias for `Invoke-WebRequest`).
  > In **Azure Cloud Shell** the shell runs on **Linux**, so use plain **`curl`**
  > (drop the `.exe`) — `curl.exe` won't be found there.

**Handshake** — COM proves it owns the endpoint via a `GET` with a challenge
header; the relay echoes it back as `{"verification": "<token>"}`:

```powershell
curl.exe -s "https://$FQDN/com/webhook" `
  -H "x-compute-ops-mgmt-verification-challenge: hello123"
# → {"verification": "hello123"}
```

**Auth check** — a POST without the secret must be rejected `401`; with the
correct header it returns `202` and lands a message on the queue:

```powershell
# Expect 401 (no secret)
curl.exe -s -o NUL -w "%{http_code}`n" -X POST "https://$FQDN/com/webhook" `
  -H "content-type: application/json" -d '{}'

# Expect 202 (valid secret) — enqueues a (minimal) test message
curl.exe -s -o NUL -w "%{http_code}`n" -X POST "https://$FQDN/com/webhook" `
  -H "content-type: application/json" -H "$HDR`: $SECRET" -d '{\"id\":\"ping\"}'
```

> The relay validates the secret with a constant-time compare and caps the body
> at `MAX_BODY_BYTES` (default 256 KB → `413` if exceeded).

---

## 4. Configure the COM webhook

In the HPE GreenLake / Compute Ops Management console, create a webhook:

- **Destination URL:** `https://<FQDN>/com/webhook`  (from step 2)
- **Custom header:** name `x-shim-secret` (your `$HDR`), value = `$SECRET`.
  COM authenticates with a **static header only** — this is that header.
- **Event filter (`eventFilter`):** scope it to servers so the lab stays quiet,
  e.g. server health changes. Filtering happens **server-side at COM**; the relay
  forwards whatever COM sends.

COM will first call `GET` (the handshake in step 3) and only enable the webhook
once it echoes the challenge over public HTTPS with a valid certificate — ACA
provides that TLS automatically.

> Keep the webhook **healthy**: COM disables a webhook after **10 consecutive
> non-2xx** responses. The relay returns `202` as soon as the event is queued, so
> a slow/broken GitHub target never affects webhook health — that decoupling is
> the whole point of the queue.

---

## 5. Run the shim (drains the queue → GitHub)

Run this on any machine with **outbound** internet (your laptop is fine for the
test). It needs the **listen** connection string from step 2 and your GitHub
details. Nothing inbound is opened.

```powershell
docker run --rm --name com-event-shim `
  -e QUEUE_BACKEND=servicebus `
  -e "SERVICE_BUS_CONNECTION=$SB_LISTEN" `
  -e QUEUE_NAME=com-events `
  -e TARGETS=github `
  -e GITHUB_REPO=your-org/com-lab-issues `
  -e GITHUB_TOKEN=<your-fine-grained-PAT> `
  -e SERVER_MONITORS=health `
  ghcr.io/jullienl/com-event-shim:latest
```

Key env vars (full list in [shim/.env.example](../shim/.env.example)):

| Var | Value here | Notes |
|-----|-----------|-------|
| `QUEUE_BACKEND` | `servicebus` | Must match the relay. |
| `SERVICE_BUS_CONNECTION` | `$SB_LISTEN` | **Listen**-scoped (least privilege). |
| `QUEUE_NAME` | `com-events` | Must match the relay's `QUEUE_NAME`. |
| `TARGETS` | `github` | One name, or comma-separated to fan out (`github,slack`). |
| `GITHUB_REPO` / `GITHUB_TOKEN` | your repo + PAT | Token needs **Issues: read/write**. |
| `SERVER_MONITORS` | `health` | Watch server health (default). Add `power`/`connection`/`subscription` to watch more. |
| `DEDUP_TTL_SECONDS` | `3600` (default) | Suppresses duplicate/redelivered events within the window. |

> **De-dup persistence (optional):** the shim keeps a small SQLite de-dup store at
> `DEDUP_DB_PATH` (default `./dedup.db`). For the test it can stay in-container;
> for anything longer-lived, mount a volume: `-v com-dedup:/data -e DEDUP_DB_PATH=/data/dedup.db`.

The shim logs each message it drains, the events it normalises, and the forward
result. Leave it running for the end-to-end test.

---

## 6. End-to-end test

**Path A — real COM event.** Trigger (or wait for) a server health change that
matches your `eventFilter`. Within a few seconds you should see:
1. Relay logs a `202` for the POST.
2. Shim logs the drained message → `forwarded to GitHub`.
3. A new **issue** appears in your repo, labelled `com:server:<serial>:health`.
When the server returns to healthy, COM sends a *clear*; the shim finds the open
issue by that label and **closes** it (with a recovery comment).

**Path B — synthetic event (no waiting).** Post a sample server snapshot straight
at the relay to exercise the whole chain: a `raise` snapshot (unhealthy) opens an
issue, a `clear` snapshot (healthy) closes it. Generate the exact raw COM payloads
the repo ships as fixtures (run from the repo root, in your Python venv):

```powershell
# Write clean raw-payload JSON files (uses the shipped sample builder)
python -c "import sys, json; sys.path.insert(0,'com-event-core/examples'); import dump_payloads as d; open('raise.json','w',encoding='utf-8').write(json.dumps(d._build_payload('server','raise')))"
python -c "import sys, json; sys.path.insert(0,'com-event-core/examples'); import dump_payloads as d; open('clear.json','w',encoding='utf-8').write(json.dumps(d._build_payload('server','clear')))"

curl.exe -s -o NUL -w "%{http_code}`n" -X POST "https://$FQDN/com/webhook" `
  -H "content-type: application/json" -H "$HDR`: $SECRET" --data "@raise.json"
# → issue opens

curl.exe -s -o NUL -w "%{http_code}`n" -X POST "https://$FQDN/com/webhook" `
  -H "content-type: application/json" -H "$HDR`: $SECRET" --data "@clear.json"
# → same issue closes
```

> To just *see* the sample payloads and how they normalise (without posting), run
> `python com-event-core/examples/dump_payloads.py server raise` — it prints the raw
> COM payload plus the resulting `CanonicalEvent`s.


> First *clear* can occasionally race GitHub's search index (~1s lag); if the
> issue isn't found the message is abandoned and redelivered, and the retry
> closes it — no lost events.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| COM won't enable the webhook | Handshake failed | Confirm `GET /com/webhook` echoes the challenge over **public HTTPS** with a valid cert (step 3). Check the URL has no typo and ends in `/com/webhook`. |
| Relay returns `401` | Wrong/missing header | Header **name** must equal `SHARED_SECRET_HEADER` (`x-shim-secret`) and value must equal `$SECRET`. |
| Relay returns `413` | Body too large | Raise `MAX_BODY_BYTES` on the relay app if you genuinely send large payloads. |
| Relay returns `503` | Queue unreachable | Check the **send** connection string secret and that the namespace/queue exist; `GET /readyz` should be `200`. |
| `az containerapp create` can't pull image | Private GHCR package | Add `--registry-server ghcr.io --registry-username … --registry-password <read:packages PAT>` (step 4). |
| Shim starts then exits | Missing required env | It fails fast if `SERVICE_BUS_CONNECTION`/`QUEUE_NAME` (servicebus) are unset. Check the **listen** string is set. |
| No GitHub issue appears | Token/repo/scope | Verify `GITHUB_REPO=owner/repo` and the PAT has **Issues: read/write** on that repo. Watch the shim logs for the forward error. |
| Issue opens but never closes | Clear not delivered / label mismatch | Confirm a *clear* event actually fired; the shim matches the open issue by its `com:<correlation_key>` label. |
| Webhook shows WARNING/ERROR in COM | Repeated non-2xx from the relay | The relay should return `202` fast; if you see `5xx`, fix the queue first — sustained failures **disable** the webhook. |

Handy log/inspection commands:

```powershell
# Relay logs (last 5 min, follow)
az containerapp logs show --resource-group $RG --name $APP --follow

# Messages sitting in the queue / dead-letter counts
az servicebus queue show --resource-group $RG --namespace-name $SB_NS `
  --name $QUEUE --query "{active:messageCount, dlq:deadLetterMessageCount}" -o table
```

---

## 8. Tear down

```powershell
docker rm -f com-event-shim 2>$null
az group delete --name $RG --yes --no-wait
```

Deleting the resource group removes the Container App, the Container Apps
environment, and the Service Bus namespace/queue in one shot.

---

### Where this maps in the code

- Relay app + endpoints: [relay/app.py](../relay/app.py) (`/com/webhook`, `/healthz`, `/readyz`)
- Relay config: [relay/.env.example](../relay/.env.example)
- Queue publisher/consumer: [relay/core/queue/](../relay/core/queue/) · [shim/core/queue/](../shim/core/queue/)
- Shim loop: [shim/worker.py](../shim/worker.py) · config: [shim/.env.example](../shim/.env.example)
- GitHub adapter: [../../com-event-core/com_event_core/adapters/github.py](../../com-event-core/com_event_core/adapters/github.py)
- Azure provisioning script: [deploy/azure/deploy-relay-azure.sh](../deploy/azure/deploy-relay-azure.sh)
