# Appendix A — Linux Shim Deployment Guide

> **Archived design document.** Describes the original single-box, OBM-only Linux
> shim (hand-deployed, referencing files like `com-obm-shim.service` that lived
> alongside it). Superseded by the containerized [com-event-bridge](../../com-event-bridge/README.md)
> (single-box, multi-target, with a systemd unit and Docker/nginx packaging).
> Kept for the OBM-specific deployment notes.

End-to-end steps to stand up a Linux box as the thin handshake shim in front of
OBM for the COM webhook integration.

> Note: The OBM Event REST API endpoint path and event field/schema names used in
> the reference code (Appendix B) are placeholders. Align them to the customer's
> specific OBM version and ingestion method before production use.

---

## Phase 1 — Provision & harden the Linux box

1. **Deploy the VM** in the DMZ (or a segment COM can reach), e.g. RHEL/Ubuntu.
   Give it a stable private IP + DNS name.
2. **Patch & harden:** update packages, create a non-root service user
   (e.g. `shimsvc`), disable password SSH, enable auto-updates.
3. **Firewall:** allow inbound `443` only from COM/HPE GreenLake egress ranges;
   allow outbound to the OBM event API host and (for callbacks) to the COM API
   host. Everything else denied.

## Phase 2 — Network reachability & TLS

4. **Public reachability:** publish the shim so COM (cloud) can reach it — a DMZ
   reverse proxy, load balancer, or NAT to a public HTTPS name
   (e.g. `com-shim.customer.com`).
5. **Valid TLS certificate:** install a CA-signed cert (Let's Encrypt or corporate
   CA). COM will reject/behave badly with an untrusted/self-signed cert. Put
   nginx in front as TLS terminator + reverse proxy to the local app.
6. **Verify** the endpoint is reachable over HTTPS from outside
   (curl from an external host).

## Phase 3 — Build the shim app

7. **Pick a runtime:** Node.js (Express) or Python (FastAPI/Flask) — small footprint.
8. **Implement the handshake (GET):** on any GET, read header
   `x-compute-ops-mgmt-verification-challenge`, respond `200`,
   `Content-Type: application/json`, body `{"verification":"<that token>"}`.
9. **Implement the event receiver (POST):**
   - Validate a shared secret header you set at webhook creation
     (reject if missing/wrong → `401`).
   - Parse the COM JSON payload.
   - Transform to an OBM event (map `name`, `hardware.serialNumber`,
     `hardware.model`, `hardware.health.summary`, `hardware.bmc.ip`,
     `updatedAt`, flattened `tags`).
   - Dedup key: compute from resource `id` + event type (optional, OBM also dedups).
   - Forward to the OBM Event REST API / Operations Connector (OpsCx).
   - Return `200` quickly to COM (do forwarding async if OBM is slow).
10. **Secrets handling:** store the COM shared secret and OBM credentials in env
    vars / a secrets file readable only by `shimsvc` (or a vault). Never hardcode.
11. **Logging:** log each handshake and event (with a correlation id) for
    troubleshooting; avoid logging secrets.

## Phase 4 — Run as a resilient service

12. **Containerize (optional):** Docker + `docker compose` (or run natively).
13. **systemd unit:** run the app under systemd (auto-restart on failure, start on
    boot) as `shimsvc`. See `com-obm-shim.service` in this folder.
14. **Reverse proxy wiring:** nginx `443` → `127.0.0.1:<app port>`; add
    rate-limiting and request size limits.
15. **Health endpoint:** add `/healthz` for monitoring; register it in OBM/your
    monitoring.

## Phase 5 — Register & verify the webhook

16. **Create the webhook in COM** (`POST /compute-ops-mgmt/v1beta1/webhooks`) with:
    - `destination` = your shim's public HTTPS URL,
    - `eventFilter` = your chosen filter,
    - `headers` = your shared secret header.
17. **Confirm handshake:** COM returns `201 PENDING` → sends challenge → shim
    replies → `GET /webhooks/<id>` should show `status: ACTIVE`, `state: ENABLED`.
    If `WARNING / Incorrect handshake response`, fix the body/content-type and
    `PATCH` the webhook to renegotiate.
18. **Send a test event:** trigger a real event (or simulate a POST) and confirm it
    lands in OBM as an event.

## Phase 6 — Bi-directional (optional now, plan later)

19. **COM API client:** provision a GreenLake OAuth client (client_credentials);
    store creds securely.
20. **Callback path:** let OO (preferred) or the shim obtain a token and call the
    COM API for remediation. Keep this separate from the inbound path and behind
    approval gates.

---

## Deployment quickstart

```bash
# 1. App files
sudo mkdir -p /opt/com-obm-shim
sudo cp server.js package.json /opt/com-obm-shim/     # (Node reference)
cd /opt/com-obm-shim && npm install

# 2. Secrets
sudo cp .env.example .env
sudo chown shimsvc:shimsvc .env && sudo chmod 600 .env
# edit .env with real values

# 3. Service
sudo cp com-obm-shim.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now com-obm-shim
systemctl status com-obm-shim

# 4. Front with nginx for TLS (443 -> 127.0.0.1:8080), then create the COM webhook
```
