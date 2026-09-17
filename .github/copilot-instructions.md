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
- **`dedup_key` is per-`(correlation_key, action, severity)`; TTL is a flat
  per-key window, NOT incident state.** `DedupStore` is a flat SQLite set of
  `(dedup_key, seen_at)` rows, each living independently for `DEDUP_TTL_SECONDS`
  (default 3600). A `raise` and its `clear` hash to **different** keys (action
  differs), so delivering the clear NEVER resets/expires the raise row — and
  replaying the **same** raise within the TTL is suppressed (and `mark_done` is
  `INSERT OR REPLACE`, so each repeat *refreshes* `seen_at`, pushing the window
  forward). Consequence for a `raise → clear → raise` test loop: it opens once,
  closes, then the 2nd raise is still deduped until the hour elapses. This is
  client-independent (curl/Postman/real COM all hash identical bytes to the same
  key). To replay freely in testing: `DEDUP_TTL_SECONDS=0` (off) or a short
  window, restart the ephemeral `--rm` shim (empty store), or vary the payload
  (`hardware.serialNumber` / `health.summary`). Keep `3600` in prod — it's the
  intended "don't re-alert for the same ongoing problem / ignore redeliveries"
  window, NOT a bug. General rule: TTL dedup is duplicate-suppression, not a
  state machine — never assume a clear "reopens" a raise key.
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

## Target-API drift + adapter validation (critical)

- **A validated RAISE path proves NOTHING about the CLEAR path — they call
  different endpoints, and only the clear SEARCHES.** `JiraAdapter._create()`
  (`POST /rest/api/3/issue`) kept working while `_close()` was dead, because the
  close first has to *find* the open item by correlation label. Every stateful
  adapter (`jira`, `github`, ITSM) has this asymmetry: create → write-only,
  clear → search-then-write. Live validation must post a `raise` **and** its
  matching `clear`; a green raise is half a test.
- **Jira Cloud REMOVED `POST|GET /rest/api/{2,3}/search`** (deprecated
  2024-10-31, removed after 2025-05-01, changelog `CHANGE-2046`) — it answers
  **`410 Gone`**, so `_close()` failed for every clear. Replacement is the
  *enhanced search* `POST /rest/api/3/search/jql`, with three behaviour changes
  that bite silently: (1) the JQL must be **bounded** or it `400`s — our
  `project = "<KEY>"` clause satisfies that, never drop it; (2) the response is
  `issues` + `nextPageToken` with **no `total`** and **no `startAt`**; (3) issues
  are returned with `id` and `key` is *not* guaranteed (the endpoint defaults to
  ids only), so read `it.get("key") or it["id"]` — `/issue/{issueIdOrKey}/
  transitions` accepts either. Also note it has **no read-after-write
  consistency**: a `clear` fired seconds after its `raise` can legitimately find
  nothing and log `no open Jira issue …; nothing to close`. Space raise/clear
  apart when testing rather than "fixing" the search.
- **A PERMANENT target error is indistinguishable from a transient one to
  `deliver()` — so a removed endpoint burns the whole retry budget, per event,
  forever.** `forward()` signals failure by raising, and the caller cannot tell
  `410 Gone` (will never succeed) from `503` (try again): the shim `abandon`s and
  Service Bus redelivers ~10× before dead-lettering; the bridge reschedules with
  backoff. Symptom is a log loop of `delivery failed for: <adapter>; abandoning
  for retry` with the **same** status code every time. Rule: when triaging a
  delivery failure, read the **status code** before the retry count — a stable
  4xx is a code/config bug, only a varying or 5xx code is a real outage.
- **Vendor SaaS endpoints get removed; pin the check to the vendor's changelog,
  not to intuition.** Before writing or "fixing" any adapter call, confirm the
  endpoint is current (Atlassian: developer.atlassian.com/changelog). Prefer
  omitting optional request knobs you don't need (`fields`, `validateQuery`) —
  each one is extra surface that a successor API can reject.

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
- **Post-only chat adapters (`slack`/`teams`) make those healthy-condition clears
  VISIBLE.** Stateful adapters (`github`/ITSM) *search* for the item a clear would
  close and silently no-op when none is open, so a healthy condition is invisible.
  A post-only incoming webhook has no lookup — it just posts, so **every enabled
  `SERVER_MONITORS` condition that is healthy on a snapshot produces a `Resolved`
  post on every delivery** (e.g. `SERVER_MONITORS=health,power` + a `CRITICAL`
  health / `ON` power snapshot → a critical *health* message AND a `Resolved`
  *power* message). By design, not a bug. Guidance: scope `SERVER_MONITORS` to the
  conditions you actually want alerts on. General rule: a stateless `clear` is
  invisible on stateful (search-then-close) targets but visible on post-only ones
  — never assume adapters render clears identically.
- **A `CanonicalEvent` field is NOT uniformly populated across normalizers —
  grep every `_normalize_*` before gating logic on it.** `mgmt_url` is
  `https://<hardware.bmc.ip>` in `_normalize_server` / `_normalize_generic` but
  **hardcoded `None` in `_normalize_alert`** (the alert normalizer never looks
  for a BMC address). So a feature gated on `source_type in (server, alert)` +
  "needs `mgmt_url`" would silently skip **every** alert event forever — no
  error, just a capability that never fires. The dataclass declaring
  `mgmt_url: str | None` says nothing about which producers fill it. Rule: for
  any field a new consumer depends on, enumerate its value in each normalizer
  (and treat "always provided" claims as a hypothesis to verify in code); when
  it can legitimately be absent, the consumer must **skip and log**, never raise
  — raising on a structurally-missing field turns it into endless redelivery.

## Runbook curl bodies + invalid-JSON dead-letter (docs)

- **A typeless payload is NOT an error.** `normalize()` dispatches on `type`
  (`.../server`, `.../alert`) and **falls back to `_normalize_generic()` for
  anything else — nothing is dropped**. A body like `{"id":"ping"}` becomes a
  valid generic `CanonicalEvent` and would *open* an item; it does not fail.
- **The shim dead-letters INVALID JSON immediately** (first delivery, reason
  `invalid-json`) in [worker.py](../com-event-relay/shim/worker.py) — this is
  distinct from the `abandon`→redelivery path (transient target failure, ~10
  tries before Service Bus/SQS dead-letters). So a lone `Dlq=1` right after a
  smoke test usually means a malformed body was enqueued, not a target outage.
- **PowerShell single-quoted curl JSON bodies keep backslashes LITERALLY.**
  `-d '{\"id\":\"ping\"}'` posts the bytes `{\"id\":\"ping\"}` (invalid JSON) →
  relay returns `202` (it never parses the body, just enqueues) → shim
  dead-letters `invalid-json`. In runbook `curl.exe` examples use `-d '{}'`
  (valid, no embedded-quote escaping) or write a fixture file and `--data
  "@file.json"` (the Path B pattern). Never document `'{\"...\"}'` for a
  PowerShell body.
- General rule: the relay enqueues bytes without validating them, so any
  documented POST body must itself be valid JSON — the failure surfaces later
  (and confusingly) at the shim as a dead-letter, not at the relay.

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
- **A long-running queue consumer's `receive()` must loop forever (`while True`)
  around the SDK receive — never let an idle/`max_wait` timeout end the
  generator.** `azure-servicebus`: iterating a receiver created with
  `max_wait_time` (our `RECEIVE_MAX_WAIT`, default 30s) raises `StopIteration`
  after that many **idle** seconds. If `receive()` is just
  `for msg in self._receiver: yield ...`, the generator ends on the first empty
  window → the worker's `for msg in consumer.receive()` loop ends → `finally:
  close()` → `main()` returns → **container exits cleanly (code 0)** the first
  time the queue is idle. Symptom: shim "runs fine then just exits after a while",
  no error. Fix: wrap the inner `for msg in self._receiver` in `while True:` so an
  idle window re-enters the same receiver ([servicebus.py](../com-event-relay/shim/core/queue/servicebus.py)).
  The SQS backend was already correct (long-poll inside `while True`). Rule: any
  new consumer backend must keep looping past an empty/idle receive.
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
- **A newly pushed GHCR package is PRIVATE by default, and the push gives no hint
  of it.** `docker push` succeeds and prints a digest; the visibility only bites
  later when a runtime tries to pull (ACA revision `Failed`, or an anonymous
  `docker pull` → `denied`). Check with
  `gh api user/packages/container/<name> --jq .visibility`. **User-scoped package
  visibility cannot be scripted** — `gh api --method PATCH
  user/packages/container/<name>/visibility` returns **404**; it is UI-only
  (`https://github.com/users/<user>/packages/container/<name>/settings` → Danger
  Zone). So either make it public by hand, or keep passing pull credentials.
  Credentials must be supplied **on `create`** (`--registry-server/-username
  /-password`), since `az containerapp registry set` fails
  `ResourceNotProvisioned` on an app already `Failed`. Prefer credentials in a
  runbook: they work for both visibilities, whereas "it's public" is a manual
  step a replicator will forget. Caveat to document either way: a PAT passed as a
  registry secret **expires**, and the app then fails to pull a new revision
  although nothing in the config changed.
- **Before making an image public, prove it has no baked-in secrets — don't
  assume.** `docker image inspect <img> --format '{{range .Config.Env}}{{println
  .}}{{end}}'` and check the layer history. Publishing is one-way. For this repo
  the emulator image must show only `PATH`/`MOCKUP_FOLDER`/`PORT`/`ASYNC_SLEEP`
  /`HTTPS`; the Redfish credential arrives at runtime via `AUTH_CONFIG` as an ACA
  **secret reference**, never an image layer. General rule: runtime-injected
  secrets are what make an image safe to publish — verify the injection actually
  happens before relying on the claim.

## Azure Container Apps + Postgres deploy gotchas (verified 2026-09)

- **Docker Hub anonymous pulls hit the rate limit — mirror upstream images too,
  not just GHCR→ECR.** ACA pulling a Docker Hub image (e.g.
  `docker.n8n.io/n8nio/n8n`) with no auth fails
  `TOOMANYREQUESTS: unauthenticated pull rate limit` and the revision goes
  `Failed`. Mirror it into a registry you authenticate to
  (`docker buildx imagetools create --tag ghcr.io/<you>/n8n:<v> docker.n8n.io/n8nio/n8n:<v>`
  copies the multi-arch manifest from your workstation) and deploy from the
  mirror. General rule: any image a managed runtime pulls anonymously from Docker
  Hub is a rate-limit risk — mirror it to an authenticated registry.
- **You cannot `az containerapp registry set` on a `Failed` app**
  (`ResourceNotProvisioned`). A private mirror needs pull creds passed **inline on
  `create`** (`--registry-server ghcr.io --registry-username <u>
  --registry-password (gh auth token)`; the `gh` token needs `read:packages`). If
  the app already failed on a bad image, **delete + recreate** with the creds —
  don't try to patch the Failed app.
- **`--public-access 0.0.0.0` on `az postgres flexible-server create` does NOT
  reliably create the Allow-Azure-services firewall rule.** Symptom: n8n (or any
  app) crash-loops `CrashLoopBackOff` with `There was an error initializing DB` /
  `Connection terminated due to connection timeout` — a **TCP timeout**, not an
  auth/SSL error, because the server silently drops the connection. Fix: add it
  explicitly (`az postgres flexible-server firewall-rule create -n
  AllowAllAzureServicesAndResourcesWithinAzureIps --start-ip-address 0.0.0.0
  --end-ip-address 0.0.0.0`) and **always verify `firewall-rule list` is
  non-empty** after create. Also: `flexible-server create --database-name` is now
  rejected (elastic-cluster-only) — create the DB separately with
  `flexible-server db create --name <db>` (flag is `--name`, not `-d`).
- **Reading ACA logs behind a TLS-intercepting corporate proxy needs the Windows
  CA bundle.** `az containerapp logs show` + `az monitor log-analytics query` hit
  `*.azurecontainerapps.dev` / Log Analytics and fail
  `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`.
  `AZURE_CLI_DISABLE_CONNECTION_VERIFICATION=1` fixes `management.azure.com` calls
  but NOT the `.dev` logstream. Working fix: export the Windows trust store
  (`Cert:\{CurrentUser,LocalMachine}\{Root,CA}`) to a PEM and set
  `$env:REQUESTS_CA_BUNDLE=<pem>` for the log call. Mgmt-plane calls
  (create/show/update) and `gh`/`docker` work through the proxy unchanged — only
  the log data-plane needs the bundle.
- **A CLI loop that writes secrets MUST check the exit code — printing "stored"
  unconditionally produces a FALSE SUCCESS.** Azure Cloud Shell can fail
  `az keyvault secret set` with `Timeout waiting for token from portal. Audience:
  https://vault.azure.net` (Cloud Shell MSI hiccup) while a naive
  `az ...; Write-Host "stored $t"` loop reports every tenant as stored and leaves
  the vault **empty**. Always `if ($LASTEXITCODE -ne 0) { break }` inside the loop
  and verify with `az keyvault secret list` (count the names) afterwards.
  Restarting Cloud Shell re-acquires the token; the already-authenticated local
  `az` session works too. General rule: for any bulk secret/resource load, verify
  the end state independently — don't trust the loop's own echo.
- **Don't POST a JSON body through `az containerapp exec`.** Nested
  PowerShell → `az` → `sh` quoting mangles the payload and the service returns a
  misleading `422 Unprocessable Entity` even when it is perfectly healthy. Use
  **GET** endpoints (`/health`, `/agents`) to prove in-environment connectivity to
  an internal-ingress app, and test POST endpoints from the real caller (the n8n
  HTTP Request node). Internal ingress has no public endpoint, so
  `az containerapp exec` into a *peer* app is the correct way to prove the network
  path.
- **`az containerapp exec` runs ONE quote-free command — it cannot carry a shell
  script — and it is RATE-LIMITED.** A nested payload like
  `sh -c 'for s in a b; do wget …; done'` dies in the container with
  `sh: line 0: syntax error: unterminated quoted string`; only a single simple
  command (e.g. `wget -qO- http://user:pass@host/path`, credentials in the URL so
  there is no `--header` to escape) survives the quoting layers. Separately, a
  handful of sessions in quick succession returns
  `Handshake status 429 Too Many Requests` with `retry-after: 600` — a **ten
  minute** lockout — so never write a `foreach` that execs once per app. Prefer
  **`az containerapp logs show`** for per-app verification: different API, no
  quoting, and apps usually log the decisive fact at start-up (the iLO emulator
  prints its loaded `MOCKUP_FOLDER` and `Redfish endpoint at localhost:8000`,
  proving both the runtime selector and the plain-HTTP port in one line). General
  rule: verify N instances from their logs, and spend scarce `exec` calls only on
  what logs cannot show (an actual authenticated round-trip).
- **Never match the FIRST occurrence of a status field in a nested document.** A
  Redfish `ComputerSystem` payload carries ~14 `Health`/`HealthRollup` fields; the
  early ones belong to sub-resources that are legitimately `OK` while the server
  is `Critical`, so a naive `"Health"\s*:\s*"(\w+)"` reports `OK` on a correctly
  faulted server and fakes a broken deployment. Anchor to the containing block
  (`"Status":\s*\{\s*"Health":\s*"([^"]+)",\s*"HealthRollup"`) or parse the JSON.
  General rule: in a deeply nested document a field name is not an identifier —
  match the path, not the leaf.
- **ACA `external: false` is an INGRESS-ROUTING boundary, not a network one —
  and a `404` is the proof, not a red flag.** In a managed (non-VNet)
  environment (`vnetConfiguration.infrastructureSubnetId: null`) the whole env
  shares ONE public static IP and a wildcard DNS record, so the internal app's
  `*.internal.<env>` FQDN **does resolve publicly, to the same IP as the external
  app** — DNS is not the boundary. A public request completes the TLS handshake
  against the shared ingress, which then finds no route for an `external:false`
  host and returns `404`. **Verify with a three-way probe**: external app → `200`,
  internal app → `404`, and a **hostname that was never deployed** → `404`. Equal
  responses for the last two is the actual evidence (the app is indistinguishable
  from nonexistent); checking only "internal returns 404" can't tell a working
  boundary from a proxy swallowing the request — inspect headers too. For a
  network-layer boundary (no public IP at all) the environment must be created
  with `--infrastructure-subnet-resource-id` + `--internal-only true`; this is
  **create-time only**, so decide before creating the env. General rule: an
  internal-only endpoint with no auth of its own is protected solely by that
  routing decision — prove it externally, and keep an app-level control
  (`COPILOT_REQUIRE_TENANT=1`) as the second layer.
- **ACA ingress needs a PLAIN-HTTP backend — an upstream image that serves TLS
  itself must be switched off.** Container Apps terminates TLS at the edge and
  forwards cleartext to `targetPort`; app-to-app calls inside an environment are
  `http://<app-name>` (no port, no path prefix, no `https://`). The HPE
  `ilo-redfish-emulator` defaults to TLS
  (`HTTPS = os.getenv('HTTPS','Enable')`, `port = int(os.getenv('PORT', 443))` in
  `src/emulator.py`), so the image must be built/run with `HTTPS=Disable` +
  `PORT=8000` or ingress health probes and every request fail. General rule:
  before deploying a third-party image to ACA, **read its source for a TLS/port
  toggle** — don't assume it speaks HTTP.
- **Before building N images that differ only by baked-in data, check whether the
  app selects that data at RUNTIME.** The iLO emulator reads
  `MOCKUP_FOLDER` per process (`os.getenv('MOCKUP_FOLDER','DL325')`) and its
  Dockerfile does `COPY mockups /app/api_emulator/redfish/static` — i.e. it ships
  *every* dataset and picks one at start-up. So six fault scenarios = **one**
  image with six mockup dirs (`mockups/lab-<scenario>`) + six apps differing only
  by `--env-vars MOCKUP_FOLDER=lab-<scenario>`, NOT six images. Saves N-1 builds,
  pushes and tags, and a fix rebuilds once. Two caveats to handle explicitly:
  (1) the per-scenario dir must be a **complete** copy of the base mockup, not a
  partial overlay, because `MOCKUP_FOLDER` loads that dir as the whole static
  tree; (2) a wrong/absent selector value **fails silently** — the app falls back
  to the built-in default and serves healthy data — so add a post-deploy
  assertion (`env[?name=='MOCKUP_FOLDER'].value` equals the expected value per
  app), never just a "replica is Running" check. General rule: a runtime selector
  collapses N build-time variants into one artifact, but it moves the failure
  from build-time-loud to runtime-silent, so it must come with an explicit
  config audit.
- **Cross-document routing must be a single shared convention.** The n8n guide
  and the iLO runbook both key on the same `scenario` name, so a station's
  `SimulatorBaseUrl` is mechanically `http://ilo-<scenario>` and the emulator app
  names ARE the routing table. When two runbooks describe halves of one pipeline,
  name the join key once and derive both sides from it — never let one doc carry
  a placeholder FQDN the other has to fill in by hand.
- **Never paste a second copy of a routing table / generated code block into a
  second document — cross-reference it.** The iLO runbook's Phase 6 carried a
  full duplicate of n8n Node A2 (`const routes = {...}`) that also lives in the
  environment runbook, so the two would silently drift and a reader could
  configure from the stale one. Keep the code in exactly one doc, link to its
  heading anchor from the other, and in the linking doc explain only what is
  local to it (here: that the CSV is the source and the JS is *generated from*
  it, not the reverse). Corollary: when a generator script already emits a block,
  the doc must say "paste the generator's output" and link to the step that
  printed it — a placeholder block (`"<TEAM_01_TOKEN>"`) invites hand-typing,
  which is how a token gets mismatched between the participant's CSV and the map
  the server resolves.
- **A doc-anchor checker must replace EACH whitespace char with a hyphen, not
  collapse runs.** GitHub slugs `## Step 8 — Generate 25 station assignments` to
  `step-8--generate-25-station-assignments`: it strips the em dash and turns the
  two remaining spaces into **two** hyphens. A validator using
  `re.sub(r"\s+", "-", s)` collapses them to one and reports every em-dash
  heading as a broken link — ~45 false failures in one run here. Use
  `re.sub(r"\s", "-", s)` and don't `.strip("-")`. General rule: when a
  validation script reports that *almost everything* is broken, suspect the
  script before the artifact.
- **A link checker must match links whose TEXT wraps across a newline, or it
  reports a clean bill of health it did not earn.** Markdown allows
  `[Some long label\nhere](#anchor)`, and prose wrapped at 80 columns produces
  them constantly. A per-line `re.finditer(r"\[([^\]]*)\]\(([^)]+)\)", line)`
  never sees them, so the link is silently **unchecked** — here a link to a
  section that did not exist at all passed as "ALL ANCHORS RESOLVE". Join the
  body (blanking fenced-code lines to keep line numbers) and match with `re.S`.
  General rule: this is the false-**negative** twin of the `\s+` trap above — a
  validator that reports nothing broken is only trustworthy once you have seen
  it fail on a case you planted.
- **Two PowerShell traps that make an AUDIT lie, in opposite directions.**
  (1) *False alarm:* a `Where-Object` returning ONE `@(start,end)` pair unrolls,
  so `$hit.Count` is **2**, not 1 — an "exactly one match" guard then throws
  `ambiguous: 2 candidate fences` on a perfectly unambiguous document. Count
  matches with an explicit `$n++` in the loop, never `.Count` on a filtered
  result whose elements are themselves collections.
  (2) *False positive:* `-match` and `Select-String` are **case-insensitive by
  default**, so searching an AI report for the acronym `AMS` also matches
  "S**ams**ung" — it reported the memory scenario's root cause as contaminated
  when it named a Samsung DIMM. Use `[regex]'AMS'` (case-sensitive) for
  acronyms. General rule: before acting on an audit result, verify the audit on
  a case you already know the answer to.
- **Deriving a name in two places is fine; forgetting that nothing enforces it is
  not.** The HOL generator writes `SimulatorBaseUrl = "http://ilo-$Scenario"`
  *before* Step 10 creates `$App = "ilo-$Scenario"`, which is safe because the
  name is **derived by a shared convention, not looked up** — so the step can run
  against infrastructure that doesn't exist yet. The cost is that a rename on one
  side generates and deploys cleanly and only fails at run time (unresolvable
  host at the HTTP node). Whenever two steps derive the same identifier
  independently, document the ordering rationale AND add a reconciliation check
  that runs once both sides exist.
- **Validate the field the pipeline actually consumes, not a derived twin.** The
  assignment CSV carries both `Scenario` and `SimulatorBaseUrl`, but n8n Node A2
  rebuilds the URL itself from `scenario` — so `SimulatorBaseUrl` is never read
  at run time. A check keying on it can pass while the live path is broken (and
  vice versa). Key the check on the consumed field, and assert the unused twin
  still agrees as a separate, explicitly-labelled drift check.
- **A payload documented in prose is NOT a contract — derive it from the parser
  and the node expressions, and POST it.** The HOL runbook showed a *flat*
  illustrative event (`serialNumber`, `severity`, `serverName`, `occurredAt`)
  while `normalize()` reads the **nested** canonical shape (`hardware.health
  .summary`, `hardware.powerState`, `hardware.serialNumber`) — so every field the
  doc told the reader to set was a field nothing reads. It even contradicted its
  own adjacent warning ("use the exact variable names… do not invent fields").
  Worse, it **omitted `id`**, the one field n8n reads *by name* (twice:
  `session_id` in A6, `eventId` in A7), so every stored row recorded
  `eventId: "unknown"` and `session_id` collapsed to `team-NN-undefined` — i.e.
  all of a station's runs silently shared one Copilot conversation. Nothing
  errored at any layer. Rule: to document a payload, grep the consuming code for
  every by-name read (`payload.get(...)`, `$json.event.<field>`), publish the
  shape the parser actually dispatches on, and prove it by posting it and
  checking the stored/derived values — never transcribe a plausible-looking
  example. Corollary: **a missing field that the consumer defaults
  (`?? "unknown"`) is invisible** — treat every `??` / `.get(x, default)` on a
  delivery path as a field whose absence must be tested for, not assumed.
- **A runbook code block that reads a file the document never creates is a dead
  end — inline the artifact instead.** The end-to-end smoke test did
  `Get-Content ./sample-canonical-event.json`, a filename appearing exactly once
  in the repo, leaving the reader to invent the very payload the step exists to
  validate. Build it in the block (`@{...} | ConvertTo-Json`) so the test is
  self-sufficient, and generate the unique-per-event fields (`id`, `updatedAt`)
  at run time rather than hardcoding them.
- **An instructor/answer-key artifact must be filtered before it is handed to
  participants.** The HOL handout was documented as "send the station its
  `IngestUrl`, `ResultUrl`, `SimulatorBaseUrl`" — but `http://ilo-memory`
  **names the fault**, i.e. prints the exercise's answer on the handout, and is
  additionally unreachable (internal ingress) so the participant just sees a
  failure. Rule: when one generated file serves both operator and participant,
  enumerate per column who may see it; a column that encodes the expected outcome
  is never distributable.
- **Dropping the answer-key COLUMN is not enough — an identifier you synthesize
  can carry the answer INSIDE a distributed field.** The payload renderer built
  `id` as `evt-team-01-memory` for uniqueness; `id` travels inside the
  `PayloadJson` that participants receive, so the handout printed the fault name
  despite having no `Scenario` column. Compose generated ids from
  non-revealing parts (`evt-<teamId>-<labdate>`) and add a leak audit that scans
  the *distributed* artifact, not the source rows. Two traps in that audit:
  scan parsed string **values**, never the raw JSON — the legitimate key
  `powerState` contains the scenario name `power` and flags all 25 rows; and
  don't flag values the payload must legitimately carry (`WARNING`, `OFF` are
  the event's own content, not a leak).
- **Never split one script across two code blocks when the second reads the
  first's variables.** Step 8 documented the CSV generator and the `routes`-map
  emitter as separate ```powershell fences, but the emitter uses `$Teams` from
  the first — so a reader who saves block 1 as a `.ps1` and runs it gets nothing
  from block 2, because `pwsh -File` starts a **new process** and its variables
  die at exit. Same reason a `$N8nFqdn` set at the caller's prompt is invisible
  inside the script: every script must assign its own inputs (the
  "self-sufficient variable block" rule). Keep one script in one fence, and say
  explicitly where to save it and how to run it — a code block with no "save this
  as X / run it with Y" is a block a replicator will paste into a shell, losing
  the artifact.
- **If a helper script exists in the repo, the runbook must name it — otherwise
  it becomes a third, silently-drifting copy.** `generate-hol-assignments.ps1`
  sat in `docs/Private/` unreferenced while the runbook carried its own inline
  copy; the file had already drifted (stale doc filename, hardcoded FQDN). Fix:
  make the runbook block the authoritative source and instruct saving it to that
  exact path. Verify by running the script in a **temp sandbox copy**, never in
  place, when re-running would destroy live state (here: regenerating the CSV
  reissues all 25 route tokens and invalidates every handout).
- **Reorganising a GITIGNORED folder has no `git checkout` undo — take a backup
  and prove it RESTORES before moving anything.** `git status` shows nothing and
  `git stash` protects nothing for a path matched by `.gitignore` (here
  `docs/Private`, listed with **no trailing slash**, so the whole tree including
  any new subfolders is ignored recursively — a reorg needs no `.gitignore`
  edit). Confirm with `git ls-files <dir>` returning **0** *before* concluding a
  move is safe, then zip and round-trip-verify (original file count == restored
  file count).
- **Splitting sibling files into type folders (`scripts/`, `csv/`) breaks every
  `$PSScriptRoot`-relative sibling lookup SILENTLY.** `Join-Path $PSScriptRoot
  "data.csv"` keeps resolving to a path that simply no longer exists — no import
  error, just a missing input or an output written to the wrong place. Before any
  such move, grep for `$PSScriptRoot` / `__file__` / `dirname($0)`, repoint each
  to the new root (`$HolRoot = Split-Path -Parent $PSScriptRoot`), and re-run
  every script in a **sandbox copy** asserting both that the outputs land in the
  new folders and that **no stray file appears at the old level**. Remember a
  path-rewrite pass must cover `.ps1`/`.py` too — script help headers
  (`.EXAMPLE`) carry paths that a `*.md`-only pass leaves stale.
- **Moving a file that is OPEN in the editor re-materialises it at the OLD path
  on the next save — leaving a stale duplicate that looks authoritative.** Here
  both runbooks reappeared at `docs/Private/*.md` minutes after the move
  (identical names, plausible timestamps, full content), so subsequent edits and
  greps could have landed in the abandoned copy. After ANY bulk move, run a
  duplicate-basename scan (`Get-ChildItem -Recurse -File | Group-Object Name |
  Where-Object Count -gt 1`) and, before deleting either side, **diff them and
  require every differing line to be explained** by the edits you know you made —
  never delete on timestamp or size alone. General rule: `Move-Item` moves bytes,
  not open editor buffers; the filesystem is not the whole state.
- **Labelling a duplicated block "Reference —" does NOT stop it drifting; verify
  it against the running system.** The HOL runbook carried a "Reference — system
  instruction" and "Reference — expected `result` schema" for the gateway's
  `com-rca` agent, explicitly noting the real ones "live server-side in
  agents.py". Both had drifted into fiction: the deployed agent returns
  `summary` / `likely_root_cause` / `evidence` / `confidence` (**a number 0–1**)
  / `recommended_actions`, while the doc promised `incidentSummary` / `severity`
  / `observedFacts` / `correlation` / `likelyRootCause` / `confidence`
  (`high|medium|low`) / `recommendedActions` / `missingEvidence` — different
  names, different casing, a different type for `confidence`, and two fields that
  do not exist. Nothing errors, because the consumer stored the blob opaquely;
  only the acceptance criteria and the answer key were wrong. Fix: replace the
  pasted copy with a short table plus the command that reads the live value
  (`GET /agents`), and say the source file owns it. General rule: a copy marked
  "for reference" is still a copy — either cross-reference it or make the doc
  tell you how to re-derive it, and confirm against the deployment before writing
  acceptance criteria on it.
- **A generated artifact must be written to a FILE, not printed with "leave that
  terminal open".** Step 8's generator emitted the 25-line n8n `routes` map to
  stdout only, so recovering it later meant re-running the generator — which
  **reissues every route token and invalidates every handout**. That couples
  "re-render an artifact" to "re-mint identities", which is exactly the coupling
  you cannot afford. Split them: the generator mints tokens and writes the CSV,
  then delegates rendering to a second script that *only reads* the CSV and
  writes `node-a2-routes.js`, so it is safe to re-run forever. Delegating also
  keeps **one** formatter, so the pasted block cannot drift from the CSV. General
  rule: separate the idempotent renderer from the non-idempotent generator, and
  never make console scrollback the system of record.
- **When two runbooks describe a producer and a consumer, the producer doc must
  end with an explicit CONTRACT, not trail off.** Splitting layers is not just
  deleting the off-layer sections: the producing doc (here the iLO simulator
  runbook) ends at a **Scope** note near its TOC plus a short "what this
  guarantees" table — hostname, scheme/port, credential, where each signal is
  readable — with a *Verified in* column citing the step that proved each one.
  Everything operational (payloads, expected answers, workflow nodes, any
  troubleshooting whose **fix** is in the consumer) moves to the consumer doc.
  Split troubleshooting by *where the fix is applied*, not by which component is
  named in the symptom: "certificate validation failure" is emulator-side
  (wrong scheme), "AI misses the faulty component" is consumer-side (fetch list).
  Relocate into **subsections of existing steps** rather than new steps, so
  downstream numbering and every anchor to it stay valid.
- **Content in the wrong document doesn't just duplicate — it FOSSILISES a
  replaced design.** The iLO runbook's Phase 6 configured n8n (AI "tools" like
  `get_server_health`, a system prompt, a JSON schema) even though the shipped
  pipeline fetches a FIXED evidence bundle and the prompt lives server-side in
  the gateway's agent registry. Because the section sat in a doc nobody edits
  when changing the workflow, it survived the redesign and now reads as
  authoritative instructions for building something that does not exist. Rule:
  each doc owns one layer (infrastructure vs. workflow); when a section is
  off-layer, delete it and cross-reference — and when the removed text described
  a *superseded* design, leave an explicit "this was replaced, do not implement
  it" note, because a replicator who saw the old revision will otherwise
  reintroduce it. Corollary: misplaced content is a **staleness detector** — if
  a section contradicts the implementation, check whether it is simply in the
  wrong file.
- **If the model cannot fetch, an un-fetched resource is invisible — audit the
  evidence list against the scenario matrix.** n8n Node A3 built a fixed list of
  three Redfish URLs (`system`, `ilo_event_log`, `thermal`) and called power and
  storage "optional", but the fault evidence for `power`, `storage` and `memory`
  lives in `Chassis/1/Power`, `…/DiskDrives/8` and `…/Memory/proc1dimm1`. So
  three of six lab scenarios reached the AI as a health rollup plus a log line,
  with no component state to corroborate — no error, just thinner analyses and
  nothing for the participant to check the AI against. When a pipeline
  pre-fetches evidence for a model (rather than giving it tools), enumerate
  which resource carries the signal for EACH case and assert every one is in the
  fetch list. Prefer over-fetching: these were read-only `GET`s where three
  extra calls cost nothing.
- **A pipeline stage that rebuilds an object literal silently drops every field
  it doesn't name.** n8n Node A5 (`Assemble evidence`) returned
  `{routeToken, teamId, receivedAt, event, redfish}`, so the `scenario` produced
  by A2 vanished and A7 stored `"not-provided"` forever — no error, just a wrong
  value. When adding a field to a multi-stage flow, trace it through EVERY stage
  that *constructs* a new object (`return [{json:{...}}]`, `dict(...)`,
  `Model(**...)`), not just the producer and the final consumer. Prefer spreading
  (`...context`) over re-listing fields when the stage isn't deliberately
  narrowing the payload.
- **A variable used across two runbooks must be DEFINED in both.** The simulator
  runbook ran 11 `az containerapp exec -n $ContainerApp` commands but never
  assigned `$ContainerApp` — it only worked while you stayed in the PowerShell
  session from the other doc. Every runbook's variable block must be
  self-sufficient: if a command in doc B references a value, doc B assigns it,
  even when doc A already did.
- **Before documenting a schema, check the platform's RESERVED names — a runbook
  that tells the reader to create one fails at execution time, not review time.**
  n8n Data Tables add system columns `id` / `createdAt` / `updatedAt` (plus
  `dryRunState`) and reject a user column with any of those names
  **case-insensitively** (`409 "… is reserved as a system column name"`), so the
  HOL result table uses `analyzedAt`. Column names must also match
  `^[a-zA-Z][a-zA-Z0-9_]*$`, max 63 chars; the only column types are
  `string|number|boolean|date` (no JSON type — serialize with `JSON.stringify`).
  Verify against the vendor's source/schema, not intuition, and remember a
  renamed column must be renamed in EVERY node that reads or writes it.
- **An n8n Webhook path containing a route parameter is NAMESPACED by a per-node
  UUID — you cannot construct the URL by hand.** With `Path = com-hol/:routeToken`
  the production URL is
  `https://<fqdn>/webhook/<webhookId>/com-hol/<token>`, not
  `https://<fqdn>/webhook/com-hol/<token>`. Source:
  `getNodeWebhookUrl()` in `packages/workflow/src/node-helpers.ts` forces
  `isFullPath = false` when the path `startsWith(':')` or `includes('/:')`, and
  `WebhookEntity.uniquePath` registers `[webhookId, path].join('/')` in the same
  case — so the **registration** matches the display; it is not a UI quirk. The
  node's help text says it too: *"If dynamic values are set 'webhookId' would be
  prepended to path."* Consequences: the id is **per Webhook NODE**, so two
  workflows (ingest vs. result) have **different** ids; deleting and re-adding a
  node mints a new one and silently `404`s every distributed URL. Rule: any
  generator that emits caller-facing n8n URLs must take the webhook id as an
  input copied from the node's Production URL — never derive it — and must run
  *after* the nodes exist, separately from the step that mints identities.
- **A `404` probe against an UNPUBLISHED n8n workflow proves nothing.** Both the
  right and the wrong URL shape return `404` until the workflow is published, so
  such a probe cannot discriminate between them. Settle URL-shape questions from
  the source/help text, and put the real check (published URL returns `202`) in
  the acceptance list.
- **The n8n HTTP Request node REPLACES the item's `json` with the response body —
  upstream fields do not survive it, and no option restores them.** In
  `HttpRequest/V3/Description.ts` the `Put Output in Field`
  (`outputPropertyName`) parameter is nested inside the **Response** option and
  has `displayOptions.show.responseFormat: ['file','text']` — so it is *invisible*
  for `JSON`/`Autodetect` (symptom: "I don't see Put response in field"), and
  even `text` mode emits only `{ <field>: "…" }`. Only `file` preserves
  `newItem.json = items[itemIndex].json`. Consequence: any field a Code node
  attached before the request (`evidenceName`, `routeToken`, `teamId`,
  `scenario`) is `undefined` after it — silently, with no error. Recover it by
  node reference: `$('<Upstream Node>').itemMatching(i).json` (follows the
  `pairedItem` link the node sets) or `$('<Upstream Node>').first().json` for
  run-once context. The referenced node name must match the renamed title
  exactly. General rule: after ANY node that returns a fresh payload rather than
  merging, recover upstream fields by node reference — never assume `$json` still
  carries them.
- **A dev-grade WSGI server behind a managed ingress silently TRUNCATES large
  responses — and retries cannot fix it.** The HPE iLO emulator (Flask/`Werkzeug`
  dev server) serving the 142-entry IML collection through ACA ingress returns
  `HTTP/1.1 200`, `content-type: application/json`, `content-length: 240338` and
  then closes the connection early → `wget: connection closed prematurely`; the
  body arrives truncated and unparseable. In n8n this surfaces as the
  content-free `200 - undefined [item N]` (status received, no parseable body, no
  API error message to quote). It is deterministic — not concurrency (a single
  `wget` reproduces it), not the HTTP Request `Timeout` (that governs *headers*,
  which arrive instantly), and **Retry on Fail just repeats it**. Fix by capping
  the payload at the source (trim the mockup collection), not in the client.
  General rule: a dev server (`app.run()`) is not a transport you can push
  hundreds of KB through — bound the response, or run a real WSGI server.
- **When one of N parallel HTTP requests fails and the rest succeed, the variable
  is the RESOURCE, not the credential or the host.** Auth and hostname errors
  fail all N identically, so a single failing item immediately exonerates them —
  map the index to the request list (n8n's `[item N]` is the 0-based index of the
  fan-out item) and investigate that one URL. Reproduce it with a single `wget
  -S` from inside the environment before changing any node setting; the response
  headers alone (status, content-type, content-length, whether the body
  completes) usually identify the fault.
- **A vendor sample/mockup dataset is NOT a clean baseline — audit it for
  pre-existing signal before injecting your own.** The upstream DL360 mockup's
  142-entry IML already contained **6 `Critical` entries** ("Drive … status
  changed to Erasing", May 2025) — routine drive-erase records, but with
  `ClassDescription: "Drive Array"` and `RecommendedAction: "…Replace the
  defective drive"`. An AI reading that log reports a storage failure on a server
  with no fault, which **silently destroys a negative control** (`no-evidence`)
  and makes the real `storage` scenario ambiguous. Nothing errors — the lab just
  produces wrong answers. So before adding synthetic records to a sample dataset,
  **enumerate the existing values of the field your logic keys on**
  (`jq -r '.Members[].Severity' … | sort | uniq -c`) and neutralise anything that
  competes with your injected signal. Then **assert the end state**: after
  generation, exactly one non-`OK` entry per scenario, and `none` for the
  controls.
- **When synthesizing records into an existing dataset, match that dataset's
  per-field value vocabulary — it is NOT shared across fields.** The same IML
  entry carries severity twice with *different* enums:
  `.Severity` ∈ {`OK`, `Critical`} but `.Oem.Hpe.Severity` ∈ {`Repaired`,
  `Informational`, `Critical`} — **`OK` never appears in the OEM field**. Writing
  `.Oem.Hpe.Severity="OK"` is schema-plausible but is an off-vocabulary tell that
  marks the record as synthetic; `Informational` is the correct downgrade (and
  matches the pre-existing `DriveOffline` entries for the very same drives).
  Always `uniq -c` each field you write, separately, rather than assuming one
  enum spans the document.
- **Verify what a simulator SERVES, not what its fixtures say — an emulator may
  synthesize fields at load time.** The HPE iLO emulator's `Loader.randomize()`
  (`src/api_emulator/loader.py`) overwrites **every** `SerialNumber` in the
  mockup tree at start-up with a generated `[A-Z]{3}[0-9]{10}`, seeded from the
  `XNAME` env var. Unset → `random.Random(None)` → **a new serial on every
  container start**, so a replica restart silently changes the answer. A build
  audit that `jq`s the fixture file reports a value that is never served. Rule:
  audit fixtures only for fields the runtime passes through, and assert
  runtime-synthesized fields over HTTP in the smoke test. Corollary: pin the seed
  in the **image** (`ENV XNAME=...`), not as a per-app env var — a forgotten env
  var fails silently back to random, whereas a baked value also lets the smoke
  test prove it is there by *not* passing it.
- **An AI citing a value that appears nowhere in your source data is not
  necessarily hallucinating — query the LIVE service before concluding it is.**
  A report quoting serial `RVR0192652630`, absent from the whole mockup tree,
  looked like fabrication; it was the emulator's randomized serial for that
  replica. Grepping the fixtures "proved" fabrication and pointed at a
  prompt fix, which would have suppressed a symptom of a real non-determinism
  bug. General rule: the artifact on disk is not the system under test.
- **Strip vendor "simulated / demo / sample" branding from fixture data before a
  model reads it.** Upstream HPE mockups end `Systems/1.Model` and the service
  root's `.Product` with `- SIMULATED` while leaving `Chassis/1.Model` clean, so
  an AI both announces the server is simulated *and* has an asset-identity
  contradiction to chase instead of the injected fault. Fix it in the data
  (a `sub()` in the builder + a `grep -rl … || exit 1` audit), not in the prompt.
  Scope the strip: lowercase `emulat`/`simulat` in BIOS registries is genuine
  firmware prose (USB / serial-console *emulation* options) present on real
  hardware — leave it.
- **When a synthetic event and a simulated device describe the same asset, the
  identity fields are a CONTRACT between two documents.** The COM payload's
  `serialNumber`/`model` must equal what the simulator serves; otherwise the
  model leads with the mismatch ("resolve the asset discrepancy before
  dispatching parts") rather than the fault. Nothing errors — the analysis is
  just about the wrong thing. State the contract in the producing doc's
  guarantee table and link to it from the consuming doc.
- **Two representations of the same resource must be patched together.** A
  Redfish log collection (`Entries/index.json`) embeds a **full copy** of every
  entry alongside the individual `Entries/<id>/index.json` resources. Patching
  only one leaves a consumer that reads the other seeing stale data, and which
  one a client reads is not knowable in advance — so patch both and verify both.
  This applies to **deletions** too: trimming the collection must `rm -rf` the
  now-orphaned entry directories, or a direct `GET` still returns an entry the
  collection no longer lists.
- **A vendor OEM block is a SECOND, independent statement of the same fact —
  patch it or the document contradicts itself.** An HPE Redfish `Systems/1`
  reports health twice: standard `.Status` / `.MemorySummary`, **and**
  `.Oem.Hpe.AggregateHealthStatus` (per-subsystem rollup + `AggregateServerHealth`
  + `Fan/PowerSupplyRedundancy`). The lab builder patched only the standard
  fields, so a "Critical" server still advertised `AggregateServerHealth: OK`
  with `Memory.Status.Health: OK` — nothing errors, but a model that anchors on
  the OEM block (a reasonable choice on HPE hardware) concludes the server is
  healthy. Rule: when injecting synthetic state, grep the resource for **every**
  field expressing that state, including vendor extensions, and add an audit that
  prints the representations **side by side** so a divergence is visible before
  the artifact ships. Corollary: get the allowed values from the schema the
  dataset itself ships (`SchemaStore/en/<Type>.json` → `.enum`), not intuition —
  here `AggregateServerHealth` ∈ OK|Warning|Critical but the redundancy fields use
  a different vocabulary (`Redundant|NonRedundant|FailedRedundant|Unknown`), and
  only `Redundant`/`OK` appeared in the sample data.
- **Apply the OEM-second-statement check at EVERY level, not just the rollup.**
  Fixing `Systems/1.Oem.Hpe.AggregateHealthStatus` is not the end: the faulted
  **component** states its condition twice too. `proc1dimm1` kept
  `.Oem.Hpe.DIMMStatus = "GoodInUse"` beside `Status.Health = "Critical"`, so the
  resource argued with itself and the AI hedged (*"the DIMM reports Critical, but
  the OEM status reports GoodInUse"*) instead of committing to the diagnosis.
  Enumerate the OEM block of each faulted component — some carry a status
  (`DIMMStatus`, `PowerSupplyStatus.State`), others carry only descriptive data
  (a fan's `HotPluggable`/`Location`, a sensor's coordinates) and need nothing.
  Pick the value that agrees with the siblings you already set: `Degraded` (the
  DIMM is still `State: Enabled`), not `MapOutError`, which would imply it was
  mapped out of the memory configuration.
- **One physical thing can be described by TWO peer sensors — patch the pair, and
  select them BY NAME.** The DL360 `Thermal` document reports CPU 1 twice:
  `02-CPU 1` (threshold-bearing, `UpperThresholdCritical: 70`) and
  `50-CPU 1 PkgTmp` (package sensor, no thresholds). Patching only the first left
  one CPU at 78 °C and 31 °C simultaneously and the model said so — *"the CPU 1
  package sensor reports 31 °C while the separate CPU 1 thermal sensor reports
  78 °C. This discrepancy should be rechecked"* — turning a clean fan-failure
  exercise into a sensor-credibility argument. This is the OEM-second-statement
  rule applied to peers rather than to a vendor block: **grep the document for
  every element naming the component, not just the one you found first.** Derive
  the partner's value from the healthy baseline ratio (baseline 40/31, so 78
  pairs with 69), and leave its `Health` alone when it ships no threshold —
  inventing `Critical` on a sensor with no `UpperThresholdCritical` is a value
  nothing in the document justifies. Select by `.Name` (`map(if .Name ==
  "Fan 1" …)`) rather than by array index: an index is an accident of the
  capture, and a renumber is silent where a rename is loud.
- **Audit a "clean" baseline for UNRELATED faults, not just for ones that compete
  with your injected signal.** Beyond the six `Critical` drive-erase IML entries
  already known, the upstream capture came from a host with no agent installed,
  so every scenario inherited `AgentlessManagementService: "Unavailable"` +
  `AMSDeviceDiscovery: "NoAMS"`. That is a real, unexplained abnormality sitting
  in the same document as the injected fault, and it is not inert: it appeared in
  **all six** analyses, and in the `no-evidence` **negative control** the model
  promoted it into `likely_root_cause`. A control exists to prove the AI answers
  "no hardware fault here"; give it something genuine to blame and it will, so
  the lab grades the wrong behaviour as correct — silently. Rule: enumerate every
  non-nominal field in the baseline, not only the ones matching your fault's
  keyword, and neutralise them from the shared sanitiser so all scenarios move
  together. Take replacement values from the shipped schema
  (`SchemaStore/en/HpeComputerSystemExt.json` → `AgentlessManagementService` ∈
  `Unavailable|Ready`, `AMSDeviceDiscovery` ∈ `null|Busy|Complete|NoAMS|Initial`).
- **A conditional `if .field? then … else . end` sanitiser is a SILENT no-op when
  upstream renames the field — pair every one with a failing assertion.** The AMS
  fix and the identity strip are both best-effort filters; only the
  `exit 1` audit turns "upstream changed shape" into a build failure instead of a
  quietly re-armed distractor. Print the audited values side by side
  (`Cooling, both CPU 1 sensors (must agree):`) so a divergence is legible before
  the artifact ships.
- **Severity is a cross-document CONTRACT, exactly like the identity fields — and
  it is NOT uniform across scenarios.** The COM payload was documented once, with
  a hardcoded `CRITICAL`, while the simulators serve `Critical` for
  memory/cooling/storage but **`Warning`** for `power` and **`OK` + `PowerState:
  Off`** for `powered-off`. Nothing errors; the model just leads with the
  conflict — *"Compute Ops reports CRITICAL, while current Redfish hardware health
  reports Warning"* — sending the participant after an alerting-pipeline problem
  instead of the failed power supply. And `CRITICAL` on `powered-off` makes it
  indistinguishable from `no-evidence` at the COM layer, collapsing two exercises
  into one. Rule: when a synthetic event and a simulated device describe the same
  incident, **every** field they both express (identity *and* severity *and*
  message) must be tabulated per scenario in the producing doc and linked from
  the consuming one. Exactly one scenario may contradict deliberately — mark it
  as such, or a future reader "fixes" the intentional mismatch.
- **A model flagging an anomaly is NOT proof of a data defect — check the
  field's semantics before "fixing" it.** The same report questioned
  `LogicalSizeMiB = 0` on a 16384 MiB DIMM. That value is **correct**: capacity
  lives in `VolatileSizeMiB`, and `LogicalSizeMiB` describes an NVDIMM logical
  partition, so it reads `0` on every DIMM in the dataset. Patching it to silence
  the complaint would have corrupted the simulator. Distinguish the two cases by
  enumerating the field across the whole dataset: a value identical everywhere is
  baseline semantics, one that differs only on your patched resource is your bug.
  Document the benign one, or a later maintainer "fixes" it too.
- **When a runbook code fence IS the script, regenerate the working copy from the
  doc — never hand-patch both.** Editing `build-lab-scenarios.sh` in the build
  tree *and* its copy in the markdown guarantees drift. Extract the fence to a
  file (read the doc, slice the line range, write with LF endings) and overwrite
  the working copy, so the document is provably what was executed. Corollary:
  never paste *expected output* you have not seen — run the script, then copy the
  real numbers in, otherwise the "verification" block is fiction.
- **Fixing the builder does NOT fix the pinned artifact version — bump the tag in
  the runbook too.** A version tag records *which build you shipped*, not which
  source it came from, so after adding `trim_iml` the doc still pinned
  `$IloImageTag = "1.0.0"` while `1.0.0` in the registry was the pre-fix image —
  a replicator following the doc would deploy the broken one. Worse, the tag was
  written **twice in two different shells** (`$IloImageTag` in PowerShell,
  `TAG=` in the bash push block) with nothing reconciling them, so bumping one
  yields a pull failure at deploy time. Rule: when a fix changes what an image
  contains, bump the documented tag in **every** shell that names it in the same
  edit, and add a registry check (`gh api .../versions --jq
  '.[].metadata.container.tags[]'`) between push and deploy so a mismatch fails
  loudly instead of as an unexplainable pull error.
- **Verify a generated artifact by RUNNING it, not by inspecting the files.**
  Checking the mockup JSON on disk does not prove the emulator selects, parses
  and serves it. The end-to-end smoke test (start the container once per
  scenario, `curl` `Systems/1` + the IML, plus an unauthenticated request) is
  what actually proved three independent things at once: runtime
  `MOCKUP_FOLDER` selection works, the baseline sanitisation held, and
  `HTTPS=Disable` + `AUTH_CONFIG` took effect (`401` over plain `http://`).
- **Don't hedge a documented step with "if your version supports X".** "If the
  installed n8n version offers Upsert, use it. Otherwise implement Get row → If
  found → Update / Insert" forces the reader to make a decision you already pinned
  ($N8nVersion). Check the pinned version's capability once and document the one
  path, with the exact node settings.
- **A settings TABLE row can only describe a control the reader can already SEE
  — a control they must first CREATE needs prose.** The n8n Data Table match
  filter was documented as one row, `Condition | Column routeToken → Equals →
  {{$json.routeToken}}`, but `filters` is a `fixedCollection` defaulting to `{}`:
  nothing is rendered until you press **Add Condition**, and it then exposes
  *three* inputs (`keyName` / `condition` / `keyValue`, labelled Column /
  Condition / Value). A reader scanning the table looks for a field that does not
  exist. Two silent traps come with it: `keyName` defaults to `id`, so leaving it
  alone makes the upsert match nothing and **insert a new row every event**; and
  `keyValue` is a plain string input, so an expression typed in *Fixed* mode is
  stored literally and matches nothing. General rule: when a runbook step targets
  a UI control, state whether it exists on arrival, and never collapse a
  multi-input widget into a single table cell.
- **A step whose configuration is all DEFAULTS still needs a section — say why
  the node exists and which defaults must not be touched.** n8n's *Respond to
  Webhook* (B4) was documented as one sentence, "Return the first input item as
  JSON with HTTP status 200", which reads as three settings to find; in fact
  `respondWith` already defaults to `First Incoming Item`, `Response Code`
  defaults to `200` and lives under *Options → Add option* (not on the main
  panel), and the node is mandatory only because the Webhook's *Respond* =
  `Using 'Respond to Webhook' Node` leaves the caller's request open — omit it
  and the browser hangs, and a missing webhook ancestor fails with `No Webhook
  node found in the workflow`. Document instead: the node's purpose, the one
  setting that is actually chosen, and the defaults that are load-bearing
  (`All Incoming Items` would wrap the body in an array; *Put Response in Field*
  would nest it). General rule: "configure nothing" is not the same as "nothing
  to explain" — a default you rely on is a decision, and the reader cannot tell
  which knobs are safe to turn unless you say so.
- **A documented "empty case" branch is DEAD CODE unless the producing node is
  set to `Always Output Data`.** n8n stops a branch when a node emits zero items,
  so the Data Table *Get* on a non-matching filter (`executeSelectMany` returns
  `[]`) means every downstream node — including *Respond to Webhook* — simply
  never runs. Symptom: no error, but the caller gets n8n's generic post-execute
  response instead of the `status: "pending"` JSON the Code node was written to
  produce. Enabling *Always Output Data* (node **Settings** tab, not Parameters)
  makes the engine substitute one empty item `{json: {}}`, which is precisely
  what a `if (!row?.field)` guard is testing for. Corollary: this must be a
  REQUIRED step in the producing node's section, never a trailing hedge like
  "if the node emits no item, enable its always-output option" — that leaves the
  reader to discover a broken workflow. General rule: whenever a downstream
  branch handles "nothing found", verify the upstream node actually emits
  something in that case, and document the setting that guarantees it.

## Azure cost reporting + budgets (verified 2026-09)

- **There is no `az costmanagement query`.** The `costmanagement` extension
  (v1.0.0) ships only `export` and `show-operation-result`; `az costmanagement
  query` fails with *"'query' is misspelled or not recognized"*. Call the REST
  endpoints instead (`POST {scope}/providers/Microsoft.CostManagement/
  query|forecast?api-version=2023-11-01`), which also pins the contract. General
  rule: `az <ext> --help` before writing a command from memory — an extension
  installing successfully says nothing about which subcommands it exposes.
- **Cost Management returns `429` with NO `Retry-After` header.** A first call
  was throttled five consecutive times before succeeding, and the CLI surfaces it
  as a bare `Too Many Requests`. Any script that issues more than one cost query
  must wrap every call in a backoff retry, or it fails partway through,
  intermittently, with nothing to explain it. For recurring/bulk reporting prefer
  a scheduled **Export** to storage — that is the supported bulk path and is not
  rate-limited.
- **`includeFreshPartialCost` defaults to FALSE on the Forecast API and silently
  drops the most recent days — from the ACTUAL as well as the projection.** The
  forecast's own `Actual` row came back `1.2716` against a month-to-date of
  `5.7939` (−78%); the difference was exactly the last two days. Nothing errors,
  and the number looks plausible. Set it to `true`, and **assert the forecast's
  actual equals the query API's total** rather than trusting it. General rule:
  when an API can exclude "incomplete" data, find the flag and cross-check the
  derived total against the authoritative one — a silently-narrowed result is
  indistinguishable from a cheap month.
- **Reconcile every grouped breakdown against the ungrouped total.** A dimension
  that drops rows renders as a smaller bill, which is exactly what the reader
  wants to see and therefore will not question. Sum each breakdown, compare, and
  report the delta instead of printing the table as fact.
- **A resource showing `0.00` is EITHER not-yet-rated OR on a free grant — tell
  them apart by the METER NAME, not the amount.** Cost data lags 8–24h (longer
  for new resources), so a fresh resource can read `0.0000` simply because usage
  is unpriced. But a free grant reads `0.0000` too, and the two are
  indistinguishable in a `ServiceName` / `ResourceId` view. Group by `Meter`: a
  grant appears as a meter whose name ends in **`- Free`** (here
  `B1MS Compute - Free`, `Storage Data Stored - Free` for the PostgreSQL
  flexible server — it is genuinely free on 750 B1ms-hours + 32 GB/month, which
  an earlier reading had wrongly written off as "just unrated"). A grant is also
  a cliff, not a discount: 720–744h/month against a 750h grant is <5% headroom,
  so a second server / HA standby / bigger SKU starts billing at full rate
  immediately. Never report "this service is free" from a single reading; check
  the meter, and sanity-check against `https://prices.azure.com/api/retail/prices`
  (anonymous, no auth) when a number must be defended.
- **Never build a UTC "first of the month" by converting a LOCAL midnight.**
  `(Get-Date -Day 1).ToUniversalTime()` in any positive-offset zone moves the date
  *backwards* (West Europe summer: 1 Sep 00:00 +02:00 → 31 Aug 22:00Z), and a
  format string hardcoding `T00:00:00Z` then hides the wrong time while still
  sending the wrong **day**. The budget API rejects it with `Start date should be
  the first day of the month`, which reads like a malformed request rather than a
  timezone bug. Construct it directly: `[datetime]::new($utc.Year, $utc.Month, 1,
  0,0,0, [DateTimeKind]::Utc)`.
- **`az rest --method put` can exit `0` with an empty body when validation
  fails** — confirm a created resource with a separate `GET`, never by the
  absence of an error. This is how the budget bug above was caught instead of
  being reported as success.
- **A budget ALERTS, it does not CAP.** Nothing is stopped or throttled; a
  spending limit exists only on Azure Pass / Visual Studio offers, not
  Pay-As-You-Go. Pair `Actual` thresholds (50/80/100%) with a **`Forecasted`**
  one — the forecast alert is the only one that fires *before* the money is gone.
- **"Consumption" billing is NOT pay-per-use — group by `Meter` to separate
  uptime from traffic.** Container Apps meters a replica that is merely running
  (`Standard vCPU/Memory Idle Usage`) separately from one serving a request
  (`… Active Usage`). Both roll up to the **same resource**, so a per-resource or
  per-service breakdown *cannot* answer "are we paying for usage or for what we
  left switched on?". Measured here: idle **95.8%**, active **4.1%** — and that
  4% covered the entire build-and-test phase. Add `Meter` as a grouping
  dimension before drawing any conclusion about traffic.
- **The marginal cost of a request is the ACTIVE MINUS IDLE rate, not the active
  rate.** A replica you already pay for is re-rated for the seconds it is busy,
  so costing an execution at the headline active rate overstates it ~9×.
  westeurope: vCPU idle `$0.000004`/s, vCPU active `$0.000034`/s → marginal
  `$0.00003`/vCPU-s; **memory idle and active are the SAME rate**
  (`$0.000004`/GiB-s), i.e. zero memory premium. One HOL run ≈ 100 active
  vCPU-s ≈ **$0.003**; a 25-station event ≈ $0.30. Requests are `$0.56/M` after
  2M free/month. Conclusion that generalises: on a min-replicas≥1 deployment,
  execution volume is never the budget risk — the always-on footprint is.
- **Idle cost is exactly `(vCPU + GiB) × seconds × idle-rate` — CALIBRATE the
  model against a day when only one resource existed.** 13–14 Sep billed exactly
  43,200 vCPU-s + 86,400 GiB-s/day for the lone 0.5 vCPU / 1 GiB relay =
  `1.5 × 86400 × 0.000004` = **$0.5184/day**, matching to four decimals — which
  is what makes the projection defensible rather than a guess. Full lab fleet
  (16.5 vCPU+GiB units across 9 apps) = **$5.70/day = $173.58/month**, i.e. a $50
  budget is gone in ~9 days. Always convert a fleet config into a run rate before
  sizing a budget; month-to-date on a partially-built environment understates it.
- **Do NOT convert an ACA Consumption-only environment to workload profiles for a
  lab.** The `Environment Management Hour` meter is `$0.143/hour` ≈ **$104/month**
  and does not appear at all on a Consumption-only environment — it would triple
  this lab's bill by itself.
- **In this lab the bill is idle time, not traffic.** Container Apps was 99.9% of
  spend, and the six `minReplicas=1` simulators ~36% of it. Between sessions,
  scale to zero **and deactivate the revision** (`Stop-HolEnvironment.ps1`)
  rather than deleting the apps: that preserves `MOCKUP_FOLDER` / `AUTH_CONFIG` /
  the Key Vault reference, so the silent wrong-dataset failure cannot be
  reintroduced on redeploy. The documented objection to scale-to-zero (the iLO
  emulator loads its whole mockup tree at start-up, ~20s, so the first Node A3
  request times out) does not apply when a start script scales up and warms
  **before** the lab instead of during it.
- **`--min-replicas 0` does NOT reliably stop the bill, and the failure is
  invisible.** Measured: six simulators and the gateway drained in ~10 min, but
  **n8n stayed at one `Running` replica indefinitely** (polled every 60s for
  10 min; `cooldownPeriod: 300`, `rules: null`). One persistent connection — an
  open n8n editor tab is enough — holds the HTTP scaler above zero. The app
  reports `minReplicas: 0`, so it *looks* switched off while billing
  ~$31.56/month. This supersedes the earlier note that "scale-to-zero remains
  fine for `ca-n8n-com-hol`". `az containerapp revision deactivate --revision
  <active>` drops replicas to zero deterministically; `az containerapp update
  --min-replicas 1` then creates a NEW active revision and restores the app
  (verified: `/healthz` `200` immediately, and the n8n webhook URLs are unchanged
  because webhook ids live in PostgreSQL, not in the revision — so distributed
  handout URLs survive any number of shutdowns). There is no `az containerapp
  stop`/`start`. General rule: for a "did we stop paying?" check, assert the
  **replica count**, never the scale configuration — and prefer deactivation over
  scale-to-zero whenever the app may hold long-lived connections.
- **A shutdown/startup script pair must derive its inventory from the platform,
  not from a hardcoded app list** — enumerate the resource group so the two
  scripts cannot drift and a newly added app is covered automatically. Pair it
  with an explicit **protection list** (name + resource group) re-checked
  immediately before any delete, and refuse to run at all if that list matches
  nothing: a renamed protected resource must fail loudly rather than silently
  become unprotected.

## Copilot AI Gateway multi-tenancy (critical)

- **One image, two modes — token resolution is per-request, not per-process.**
  `copilot-ai-gateway` serves personal use (no `tenant` → single
  `COPILOT_GITHUB_TOKEN`) AND a multi-bot HOL (each request's `tenant` → that
  station's own bot PAT) from the SAME build. Resolve the PAT inside `_run` via
  `_resolve_token(tenant)` and pass it as `create_session(github_token=...)` —
  the SDK accepts a **per-session** token, so don't bind one token at client
  start.
- **`session_id` and bot identity are ORTHOGONAL.** `session_id` only threads a
  conversation; it does NOT isolate quota/rate-limit/attribution. N users need N
  bot accounts, not one PAT + N session_ids. Never conflate "resume a
  conversation" with "separate identity/quota".
- **A persistent Copilot session is tied to the bot that created it →
  namespace `session_id` per tenant** (`f"{tenant}:{session_id}"`) so two tenants
  reusing the same simple id (`"incident1"`) can't collide. Because tenant→bot is
  stable, the namespaced id always resolves to the same bot. **But the runtime
  accepts ONLY a UUID as `sessionId`** — it rejects anything else at
  `session.create` with `JsonRpcError -32603 ... Rejected session.create request
  with invalid sessionId: <your id>` (n8n shows it as *Bad gateway*). The SDK
  hides this because it defaults to `str(uuid.uuid4())` when you pass none, so it
  only bites once you supply your own. Hash the namespaced key through a fixed
  `uuid.uuid5(_SESSION_NAMESPACE, key)` — deterministic, so resume still works —
  and never change the namespace constant or every stored conversation is
  orphaned. General rule: an id you invent and hand to an external runtime must
  be validated against that runtime's format, and the fact that the SDK
  *generates* one for you is the hint about which format it wants.
- **Per-tenant secrets are file-first, same principle as `get_secret`.** Read a
  tenant PAT from `COPILOT_TENANT_TOKENS_DIR/<tenant>` (one file per bot; maps to
  a secret volume / Key Vault CSI) or a `COPILOT_TENANT_TOKENS_FILE` JSON map —
  never a per-tenant plain env var.
- **Validate any id that becomes a filesystem path or secret name.** Tenant ids
  are `[A-Za-z0-9_-]` only (path-traversal guard → `ValueError`→400); unknown
  tenant → `KeyError`→404. For the ACA deploy, tenant ids also become **ACA secret
  names**, so they must be lowercase `[a-z0-9-]` (e.g. `s01`).
- **Guard the unsafe fallback with a flag, don't rely on omission.**
  `COPILOT_REQUIRE_TENANT=1` rejects tenant-less requests (400) so an HOL
  deployment can't silently fall back to the personal token. The Azure deploy
  script sets this automatically whenever `TENANTS_FILE` is used.
- **Per-session identity isolation is VERIFIED, not assumed.** With two real bot
  PATs, both sessions ran concurrently under their own bot while a **garbage**
  per-session `github_token` was **rejected** (`401 Bad credentials` at
  `session.create`) even though the runtime's global CLI login was a *different*
  authenticated account — i.e. a bad per-session token does NOT fall back to the
  runtime global login. There is **no per-session whoami**
  (`CopilotClient.get_auth_status()` is client-level; `CopilotSession` has none),
  so prove isolation with the **garbage-token-rejected** test, not an identity
  accessor.
- **A CLIENT-level SDK call has NO identity in a per-session deployment — it
  fails at runtime, as a 500.** `list_models()` / `get_auth_status()` take no
  token and run under the runtime's **global** login; tenant-only mode sets no
  `COPILOT_GITHUB_TOKEN`, so `GET /models` dies with `JsonRpcError -32603 ...
  Not authenticated. Please authenticate first.` while `/chat` and `/agent/*`
  (per-session token) work fine. It also works on a dev box — the local `gh`
  login *is* the global identity — so it only breaks in the container. Rule:
  when an endpoint wraps a client-level call, decide per deployment mode whether
  to expose it, catch the auth error and return an explaining **501** rather
  than a stack-trace 500, and say so in the endpoint table. General rule: check
  each SDK method's signature for a token parameter — no token parameter means
  it inherits process-global auth, which a multi-tenant service does not have.
