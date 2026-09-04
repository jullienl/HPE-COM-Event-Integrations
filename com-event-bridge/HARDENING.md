# Hardening & operations guide — com-event-bridge

Because the bridge **is** the internet-facing endpoint (unlike com-event-relay,
where a managed cloud platform provides the edge), you own the public-edge
concerns: TLS, the certificate lifecycle, firewalling, and host hardening. This
guide covers them.

---

## 1. Network placement

- Put the box in a **DMZ** (or a segment COM can reach), with a stable private IP
  and a public DNS name (e.g. `com-bridge.example.com`).
- **Inbound firewall:** allow `443` **only** from COM / HPE GreenLake egress
  ranges. Deny everything else. Do **not** expose the app port (`8080`) — only
  nginx's `443` is public; the app binds to `127.0.0.1`.
- **Outbound firewall:** allow only what the selected `TARGET` needs (e.g. the OBM
  Event API host, the ServiceNow instance, the Splunk HEC). Deny the rest.

## 2. TLS termination

COM will reject or misbehave against an untrusted certificate, so use a **CA-signed
cert** (Let's Encrypt or your corporate CA). Terminate TLS in the reverse proxy
([deploy/nginx/com-event-bridge.conf](deploy/nginx/com-event-bridge.conf)) and
proxy plaintext to the app on `127.0.0.1:8080`.

- TLS 1.2/1.3 only.
- Send `Strict-Transport-Security`.
- Keep `client_max_body_size` aligned with `MAX_BODY_BYTES` (256 KB).

## 3. Certificate lifecycle (the part the cloud used to do for you)

Using the compose stack, **certbot** issues and renews automatically. One-time
bootstrap for the first certificate (HTTP-01 challenge):

```bash
# 1. Start nginx first so it can serve the ACME challenge on :80
docker compose up -d nginx

# 2. Issue the certificate (replace host + email)
docker compose run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  -d com-bridge.example.com \
  --email ops@example.com --agree-tos --no-eff-email

# 3. Bring up the full stack; certbot renews twice daily, nginx serves the cert
docker compose up -d
```

After renewal, reload nginx to pick up the new cert (add a cron/hook, or restart
the nginx service on a schedule):

```bash
docker compose exec nginx nginx -s reload
```

> If you use a **corporate CA** instead of Let's Encrypt, drop the certbot service
> and mount your `fullchain.pem` / `privkey.pem` into the nginx volume, then track
> their expiry in your own PKI/monitoring.

## 4. Secrets

- Keep `COM_SHARED_SECRET` **long and random**: `openssl rand -hex 32`.
- Store `.env` (or `/etc/com-event-bridge/bridge.env`) as `chmod 600`, owned by the
  service user — never commit it (`.gitignore` already excludes `.env`).
- **Rotate** the secret periodically: update it on the COM webhook and in the
  bridge config together.

> COM does **not** sign webhook payloads (no HMAC), so the shared-secret header
> over TLS is the supported authentication mechanism.

## 5. Delivery durability

- **`DELIVERY_MODE=spool`** is the **default** and the durable path: events are
  persisted locally and retried with backoff, surviving target outages **without**
  a cloud queue. `SPOOL_PATH` **must** live on durable storage (a mounted
  volume — the Dockerfile defaults to `/data`; the systemd unit uses
  `/var/lib/com-event-bridge`), and **the bridge refuses to start in spool mode
  if `SPOOL_PATH` is unset** — this prevents an ephemeral path from silently
  dropping the backlog on restart. Size `SPOOL_MAX_BYTES` for your worst-case
  outage × event rate, and back up / monitor the spool DB.
- **`DELIVERY_MODE=sync`** is **opt-in, best-effort**: COM webhooks are
  fire-and-forget (one POST, no retries), so if the target is down when an event
  arrives, that event is **lost**. A persistently down target also produces
  sustained `5xx`, and 10 consecutive webhook failures **disable the webhook** in
  COM (all delivery stops until manually re-enabled). Use `sync` only where
  occasional loss is acceptable.
- Monitor the pending backlog and alert if it grows (target trouble). `/readyz`
  returns `503` if the spool worker has died.

## 6. Host hardening

- Run as a **non-root** user (the Docker image uses uid `10001`; the systemd unit
  uses a dedicated `bridge` user with `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome`, `PrivateTmp`).
- Patch the OS and rebuild the image regularly (base image CVEs).
- Enable auto-updates for the host; disable password SSH.

## 7. Availability (optional)

The single box is a single point of failure. If you need HA:

- run **two** boxes behind a load balancer sharing the same public name;
- note the **dedup and spool stores are local** to each box — with two active
  boxes, dedup won't span them and spooled events live only on the box that
  received them. For strict correctness under HA, prefer active/passive (one
  serving, one standby) or move to the cloud-relay model, which is horizontally
  scalable by design.

## 8. Observability

Logs are structured JSON (one object per line) with the event id, mode, and
status — ship them to your log platform. Key things to watch:

- `401` spikes (someone probing the public URL);
- `503` in sync mode (target down) or spool-full;
- spool backlog size and worker liveness (`/readyz`).
