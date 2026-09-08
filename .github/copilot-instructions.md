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

## Secrets (file-or-env, vendor-neutral)

- **Read every sensitive value via `get_secret("NAME")`** from
  `com_event_core.secrets` — NEVER `os.environ["NAME"]` for a password / client
  secret / token / connection string / shared secret. Resolution order:
  `NAME_FILE` (read file, strip trailing `\n`) → `NAME` (env) → `default` / raise
  `KeyError` when `required` (the default). Non-secrets (URLs, usernames, IDs,
  table names) stay on `os.environ`.
- **Why file-first:** file contents don't leak via `docker inspect`,
  `/proc/<pid>/environ`, or child procs, and any vault projects secrets as files
  (Docker `/run/secrets/*`, K8s Secrets Store CSI = Key Vault / Secrets Manager,
  systemd `LoadCredential` → `$CREDENTIALS_DIRECTORY`, Vault Agent → tmpfs). No
  cloud SDK dependency — the bridge (on-prem, no cloud) uses the SAME code path.
- **Startup validators must accept the `_FILE` form.** The queue `_require()`
  treats a var as present if `NAME` **or** `NAME_FILE` is set — any new
  "is this configured?" check must do the same, else file-backed secrets wrongly
  fail fast.
- General rule: a secret sourced from a mounted file is the safe production
  default; the plain env var is the dev-convenience fallback, not the reverse.

## Multi-adapter fan-out + dedup (critical)

- **Deliver via the shared `deliver(event, adapters, dedup)`** in
  `com_event_core.deliver` — never call `adapter.forward()` directly in a
  consumer. Both the shim and the bridge/spool-worker use it, so fan-out + retry
  semantics stay identical.
- **Selection: `get_adapters()`.** `TARGETS` is the single knob — one name or
  comma-separated for fan-out (`TARGETS=halo,slack`); default `webhook` (the
  vendor-neutral target). Names are
  lower-cased + de-duped, order preserved; unknown name → raise. There is no
  `TARGET` (singular) alias and no `get_adapter()` — both were removed.
- **Dedup is two-phase and per-adapter.** Key on `f"{event.dedup_key}:{adapter.name}"`.
  Use `dedup.already_done(key)` (read-only) then `dedup.mark_done(key)` **only
  after a successful `forward()`**. NEVER mark before delivery.
- **Record the idempotency key AFTER the side effect succeeds, not before.** The
  old mark-on-check `is_duplicate()` was removed because a transient failure +
  retry got wrongly suppressed → **silent event loss**. Always use the two-phase
  `already_done()` / `mark_done()` pair on a delivery path.
- **Partial failure → `PartialDeliveryError`.** `deliver()` keeps going past a
  failing adapter, marks the ones that succeed, then raises. Callers retry the
  whole event (spool reschedule / queue `abandon`); already-done adapters are
  skipped so only failed targets are re-attempted (no duplicate tickets).
- **Fan-out is reliable only in spool/queue mode.** `sync` has no retry, so a
  failed target's copy is lost — consistent with `sync` being best-effort.
- Two instances of the **same** adapter type (e.g. two `webhook`s) is NOT
  supported yet — adapters read fixed global env vars (`WEBHOOK_URL`), so they'd
  collide. Needs per-instance config namespacing (labelled targets); tracked in
  the root README roadmap.

## Server snapshots + multi-condition monitoring (critical)

- **A COM `.../server` webhook is a full-state SNAPSHOT, not a "field X changed"
  delta.** COM never says which attribute changed, so "monitor attribute X" = a
  predicate over the whole snapshot on each delivery. Don't assume a delta.
- **`normalize()` returns `list[CanonicalEvent]`, one per ENABLED condition.**
  Server conditions live in a table (`_SERVER_CONDITIONS` in `normalize.py`):
  `health` (default) / `power` / `connection` / `subscription`, chosen via
  `SERVER_MONITORS` (comma-separated, unknown name → `ValueError`, empty →
  `health`). `alert`/generic still yield one event (wrapped in a list).
- **Correlation key MUST be per-condition: `server:<serial>:<condition>`.** A
  per-*server* key (`server:<serial>`) collides the moment >1 attribute is
  watched — a "reconnected" clear would close the "powered off" issue. Any new
  condition adds its own suffix; never share one key across conditions.
- **Deliver a batch with `deliver_events(events, adapters, dedup)`** (in
  `com_event_core.deliver`) — it loops `deliver()` per event, keeps going past a
  failing one, and raises an aggregated `PartialDeliveryError` so the caller
  retries the whole payload; per-(event,adapter) dedup skips the ones already
  done (no duplicate tickets). All three consumers (shim, bridge sync, spool
  worker) call it.
- **Stateless snapshots emit a `clear` for every healthy condition on every
  delivery** — dedup suppresses the steady-state repeats and the adapter close is
  a no-op when nothing is open. Expected, not a bug.

## Build / structure quick facts

- Monorepo, self-contained Docker builds: build context is the **repo root**;
  shim/bridge Dockerfiles `COPY com-event-core` + `pip install ./com-event-core`
  (no PyPI publish). Don't add `com-event-core==x` pins to shim/bridge
  requirements.
- **If a service imports `com_event_core`, TWO things must both be true or it
  breaks:** (1) its Dockerfile installs the package (`COPY com-event-core` +
  `pip install ./com-event-core`, like shim/bridge), and (2) its CI `paths:`
  filter includes `com-event-core/**`. The **relay** was migrated to
  `get_secret()` (`from com_event_core import get_secret`) but its Dockerfile +
  `build-relay.yml` still said "relay does not depend on com-event-core" → the
  published image shipped WITHOUT the package → **crash on boot**
  (`ModuleNotFoundError: No module named 'com_event_core'`). Symptom in the
  cloud: container shows `Running`, ingress/TLS/port fine, but `curl` gets **0
  bytes / stream timeout** because uvicorn exits before binding. General rule:
  when you add a `com_event_core` import to any service, update that service's
  Dockerfile install AND its workflow `paths:` in the same change; never trust a
  "does not depend on core" comment — grep the source.
- Shared logic (normalise, dedup, adapters) lives once in `com-event-core`; add a
  new target adapter there (`com_event_core/adapters/` + `_ADAPTERS` table).
- **Non-root image + default state file in a root-owned `WORKDIR` = write crash.**
  All images run as a non-root user (`USER shim/bridge/relay`, uid 10001) with
  `WORKDIR /app` (root-owned). Any component that writes a file to a **relative
  default path** (e.g. `DedupStore` → `DEDUP_DB_PATH=./dedup.db` → `/app/dedup.db`)
  fails at runtime with `sqlite3.OperationalError: unable to open database file`.
  Fix in the Dockerfile like the bridge does: create a writable dir
  (`mkdir -p /data && chown <user> /data`), `VOLUME ["/data"]`, and set the env
  default to an absolute writable path (`ENV DEDUP_DB_PATH=/data/dedup.db`). The
  shim shipped without this and crashed on `DedupStore()`; the relay never hit it
  because it doesn't use the dedup store. Rule: if a non-root image writes state,
  the Dockerfile must provide a chown'd writable dir AND default the path there —
  don't rely on the code's relative default.
- CI workflows live at repo-root `.github/workflows/`; shim/bridge builds also
  trigger on `com-event-core/**` changes.
- **CI publishes images to GHCR only** (`ghcr.io/jullienl/com-event-{relay,shim,
  bridge}`) — there is no ECR-Public/DockerHub publish workflow. **Azure Container
  Apps pulls GHCR directly**, but **AWS App Runner CANNOT pull GHCR** — its
  `ImageRepositoryType` accepts only `ECR` / `ECR_PUBLIC`. So any AWS/App Runner
  path must **mirror the GHCR image into a private ECR first** (`docker buildx
  imagetools create --tag <ecr-uri> <ghcr-uri>` copies the full multi-arch
  manifest) and deploy with `ImageRepositoryType=ECR` +
  `AuthenticationConfiguration.AccessRoleArn` (an App Runner **ECR access role**:
  trust `build.apprunner.amazonaws.com`, policy
  `AWSAppRunnerServicePolicyForECRAccess`). This is DISTINCT from the relay
  **instance role** (trust `tasks.apprunner.amazonaws.com`, grants
  `sqs:SendMessage`). Never point App Runner straight at a `ghcr.io/...` image —
  it fails. General rule: before telling a managed container runtime to pull an
  image, confirm that runtime supports the registry; if not, mirror to a
  supported one.
