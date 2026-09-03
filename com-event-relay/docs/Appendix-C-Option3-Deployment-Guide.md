# Appendix C — Option 3 Deployment Guide (Cloud Relay + Bank Shim)

> **Archived design document.** This is the original design narrative for the
> "cloud relay + on-prem shim" pattern (banking / regulated context). The
> production implementation now lives in this repository — the **relay** and
> **shim** under [../README.md](../README.md) — and in the single-box companion
> [com-event-bridge](../../com-event-bridge/README.md). Kept for the design
> rationale and OBM-specific field mapping.

End-to-end steps to deploy the **Option 3** architecture: a public **cloud relay**
in a customer-owned cloud tenant that receives COM webhooks and buffers them on a
queue, plus an **outbound-only bank shim** on-premises that drains the queue and
forwards to OBM.

Use this instead of [Appendix A](Appendix-A-Deployment-Guide.md) when the customer
**cannot expose a public inbound endpoint from their own network** (e.g. banking /
regulated environments). See [COM-Integration-Options-Deck.md](COM-Integration-Options-Deck.md)
for the design rationale.

> The OBM Event REST API endpoint path and event field/schema names used in the
> reference code are placeholders. Align them to the customer's specific OBM
> version and ingestion method before production use.

---

## Architecture recap

```
        CUSTOMER CLOUD TENANT (public)                    BANK (private, outbound-only)

COM ──webhook──► [ Cloud relay ]                          [ Bank shim ]
  eventFilter    Functions / Container App                worker.py (systemd)
  applied        - answers handshake                        │  outbound pull (443)
  in cloud       - validates shared secret                  ▼
                 - enqueues raw event ──► [ Service Bus queue ] ◄── drains
                                                             │
                                                             ▼  transform + dedup
                                                        OBM Event REST API
```

- Cloud relay code: [com-event-relay relay/](../README.md)
- Bank shim code: [com-event-relay shim/](../README.md#the-shim-target-adapters)

---

## Phase 0 — Prerequisites

1. **Sanctioned cloud tenant** (Azure assumed here) with rights to create a
   Function App (or Container App), a Service Bus namespace, and Key Vault.
2. **HPE GreenLake API client** able to create COM webhooks.
3. **OBM Event REST API** endpoint reachable from the bank network + Basic auth creds.
4. **A long random shared secret**, e.g. `openssl rand -hex 32`.
5. Bank firewall allows **outbound 443** to the Service Bus namespace and the OBM host.

---

## Phase 1 — Provision shared cloud resources

6. **Create a Service Bus namespace + queue** (e.g. `com-events`).
7. **Create two SAS policies** on the queue (least privilege):
   - `send` (Send rights) — for the cloud relay,
   - `listen` (Listen rights) — for the bank shim.
8. **Create a Key Vault** and store: the shared secret and the Service Bus
   connection strings. Grant the relay's managed identity access to the `send`
   secret only.

## Phase 2 — Deploy the cloud relay

9. **Choose a host** (see [com-event-relay README](../README.md)):
   - Serverless (recommended): Azure Functions — no OS to manage.
   - Container: Azure Container Apps / Cloud Run / Fargate.
10. **Configure app settings** (reference Key Vault, do not paste plaintext):
    `COM_SHARED_SECRET`, `SHARED_SECRET_HEADER` (e.g. `x-shim-secret`),
    `SERVICE_BUS_CONNECTION` (the `send` policy), `QUEUE_NAME`.
11. **Deploy** and note the **public HTTPS URL** (this becomes the COM `destination`),
    e.g. `https://<app>.azurewebsites.net/api/com/webhook`.
12. **Smoke test** the handshake and auth (see the curl examples in the relay README):
    - challenge header → `200` `{"verification":"..."}`,
    - valid secret → `202`, wrong secret → `401`.

## Phase 3 — Deploy the bank shim

13. **Provision a small Linux VM** (RHEL/Ubuntu LTS) or container host inside the
    bank. No inbound rules needed — outbound 443 only.
14. **Install app files** and a non-root `shimsvc` user:
    ```bash
    sudo mkdir -p /opt/bank-shim
    sudo cp worker.py requirements.txt /opt/bank-shim/
    cd /opt/bank-shim && pip install -r requirements.txt
    ```
15. **Configure secrets** (`.env` readable only by `shimsvc`):
    ```bash
    sudo cp .env.example .env
    sudo chown shimsvc:shimsvc .env && sudo chmod 600 .env
    # set SERVICE_BUS_CONNECTION (the 'listen' policy), QUEUE_NAME,
    # OBM_EVENT_API_URL, OBM_USER/PASSWORD, DEDUP_* 
    ```
16. **Install the service:**
    ```bash
    sudo cp bank-shim.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now bank-shim
    systemctl status bank-shim
    ```
17. **Verify outbound connectivity:** the log should show
    `bank shim starting; draining queue 'com-events' outbound-only` with no errors.

## Phase 4 — Register & verify the COM webhook

18. **Create the webhook in COM** (`POST /compute-ops-mgmt/v1beta1/webhooks`) with:
    - `destination` = the **cloud relay** public HTTPS URL (Phase 2, step 11),
    - `eventFilter` = your chosen filter,
    - `headers` = the shared secret header, e.g.
      `{ "x-shim-secret": "<the shared secret>" }`.
19. **Confirm handshake:** COM returns `201 PENDING` → sends the challenge to the
    relay → relay replies → `GET /webhooks/<id>` should show `status: ACTIVE`,
    `state: ENABLED`. If `WARNING / Incorrect handshake response`, check the relay
    logs and `PATCH` the webhook (keeping the `headers`) to renegotiate.
20. **End-to-end test:** trigger a real event (or simulate one from COM). Confirm
    the chain: relay enqueues → bank shim drains → event appears in OBM. Check for
    duplicates being collapsed by the shim's dedup (or OBM's own correlation).

---

## Phase 5 — Operations

21. **Monitoring:** alert on Service Bus **queue depth** (backlog = shim down or OBM
    unreachable) and on **dead-letter** count (poison messages / schema mismatch).
22. **Secret rotation:** rotate the shared secret by updating Key Vault and
    `PATCH`-ing the COM webhook `headers`; rotate Service Bus SAS keys and OBM creds
    on the bank side.
23. **Scaling:** the relay autoscales (serverless). For the shim, a single instance
    with SQLite dedup is fine; for HA, run multiple replicas and switch the dedup
    store to Redis (see [com-event-relay README](../README.md)).

---

## Deployment quickstart

```bash
# --- Cloud relay (Azure Functions) ---
cd cloud-relay/azure-function
func azure functionapp publish <function-app-name>
# set COM_SHARED_SECRET, SHARED_SECRET_HEADER, SERVICE_BUS_CONNECTION (send), QUEUE_NAME
#   as App Settings (Key Vault references)

# --- Bank shim (on-prem) ---
sudo mkdir -p /opt/bank-shim
sudo cp bank-shim/worker.py bank-shim/requirements.txt /opt/bank-shim/
cd /opt/bank-shim && pip install -r requirements.txt
sudo cp bank-shim/.env.example /opt/bank-shim/.env
sudo chown shimsvc:shimsvc /opt/bank-shim/.env && sudo chmod 600 /opt/bank-shim/.env
# edit .env (SERVICE_BUS_CONNECTION = listen policy, OBM_*, DEDUP_*)
sudo cp bank-shim/bank-shim.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now bank-shim

# --- COM webhook ---
# POST /compute-ops-mgmt/v1beta1/webhooks with destination = cloud relay URL,
# headers = { "x-shim-secret": "<shared secret>" }, and your eventFilter.
```
