# Project instructions — HPE-COM-Event-Integrations

Concise, rule-shaped guidance for working in this repo. Keep additions terse.

## COM webhook delivery semantics (critical)

Verified against HPE docs (developer.greenlake.hpe.com → compute-ops-mgmt →
webhooks: Prerequisites + Getting Started Guide → "Status changes").

- **No per-event retry / redelivery.** COM sends **one POST per event** and does
  **not** re-send a failed event. Failure counting is per-event (one attempt
  each). Never rely on COM redelivering — design for at-most-once from COM.
- **A non-2xx is NOT harmless.** COM expects a `2xx` ack. Repeated failures
  degrade webhook health: ~5 failures in the last 10 → `WARNING`; **10 consecutive
  failures → `ERROR` → webhook `DISABLED`**, which stops **all** delivery until a
  user re-enables it (PATCH) and it passes the GET handshake again. COM will not
  auto-re-enable. So sustained `5xx` can take the whole pipeline offline.
- **Do:** capture the event immediately into durable storage (relay → cloud queue;
  bridge `spool` → local SQLite) and **return `202` fast**, then deliver to the
  target **asynchronously** with its own retries. This both avoids loss and keeps
  the webhook healthy (`ACTIVE`).
- **Don't:** never write "return 503 so COM retries", "COM's retry window", or
  "COM holds and retries" — COM has no retry. Also don't frame `5xx` as a harmless
  "honest signal COM ignores"; sustained `5xx` disables the webhook.
- **`sync`/inline delivery is best-effort AND risky.** If the target is down, the
  event is **lost** *and* the returned `5xx` degrades webhook health. Prefer
  `spool` (bridge) or the relay+queue: they ack `202` immediately and buffer.
- **Spool/queue is the answer for an inaccessible target** — it decouples COM ack
  from target delivery.
- **Dedup** is for **queue at-least-once** (Service Bus/SQS redelivery after an
  `abandon`) and idempotency — **not** COM (COM has no documented duplicate
  delivery). Distinct state-change events for one resource (WARNING→CRITICAL→OK)
  are not duplicates.

## Bridge delivery mode (safe default + fail-fast)

- **`DELIVERY_MODE=spool` is the DEFAULT** (durable). `sync` is opt-in
  best-effort. Reason: a down target in `sync` loses the event AND sustained
  `5xx` can DISABLE the webhook (see above) — so the safe path must be the
  default, not `sync`.
- **Durability prerequisite is enforced, not documented-only.** `spool` needs a
  durable `SPOOL_PATH` (mounted volume). The app **fails fast at startup** if
  `DELIVERY_MODE=spool` and `SPOOL_PATH` is unset — never fall back to an
  ephemeral default (e.g. `./spool.db`), which gives *false durability* (backlog
  silently lost on container restart/redeploy). General rule: if a "safe" default
  depends on an external prerequisite (persistent volume, mount), **guard it with
  a startup check** rather than a silent default.
- Container/compose: mount a volume at `/data` (image defaults
  `SPOOL_PATH=/data/spool.db`); bare-metal/systemd:
  `/var/lib/com-event-bridge/spool.db`.

## Build / structure quick facts

- Monorepo, self-contained Docker builds: build context is the **repo root**;
  shim/bridge Dockerfiles `COPY com-event-core` + `pip install ./com-event-core`
  (no PyPI publish). Don't add `com-event-core==x` pins to shim/bridge
  requirements.
- Shared logic (normalise, dedup, adapters) lives once in `com-event-core`; add a
  new target adapter there (`com_event_core/adapters/` + `_ADAPTERS` table).
- CI workflows live at repo-root `.github/workflows/`; shim/bridge builds also
  trigger on `com-event-core/**` changes.
