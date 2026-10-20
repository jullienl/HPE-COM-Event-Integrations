# Project instructions — HPE-COM-Event-Integrations

Concise, rule-shaped guidance for working in this repo. Keep additions terse.

## Working agreement (critical)

- **Never change code, docs, or config without asking first.** For any fix or
  improvement, present 2-3 options with trade-offs and a recommendation, then
  wait for the maintainer's decision. Diagnosis and read-only checks need no
  approval.

## Customer-facing documentation and image publication

- **Customer docs first:** README and public guides target operators and
  customers. Show how to use the published container image before any source
  build or image-publishing procedure.
- **Maintainer procedures stay private:** source builds, GHCR publishing,
  release-tag mechanics, and registry troubleshooting belong in the ignored
  `docs/Private/` maintainer documentation, not in public customer guides.
- **Do not publish private runbooks:** keep maintainer-only material under
  `docs/Private/`, which is intentionally excluded from the public repository.
- **No lab-only knobs in product code:** `com-event-core`, shim, relay and bridge
  ship to customers in production. Solve HOL needs (per-team identity, cleanup,
  pacing) in `docs/Private/COM-Event-HOL/` scripts, payloads and instructions,
  never with a product env var or code path that only the lab needs.
- **Lab/HOL material stays out of this file.** This file is published. Lab
  lessons, runbook notes, costs and accounts go in `docs/Private/COM-Event-HOL/`.

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
  replaying the **same** raise within the TTL is suppressed (`deliver()` skips it
  before `mark_done`, so a suppressed repeat does NOT refresh `seen_at`: the
  window is fixed from the last successful delivery). Consequence for a `raise → clear → raise` test loop: it opens once,
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

## AI analysis rendering — pick strategy by target schema, not uniformly (critical)

- **`analysis_summary`/`analysis_root_cause`/`analysis_confidence`/`analysis_actions`
  on `CanonicalEvent` are optional (`None`/`[]` unless an enricher ran) — every
  adapter that surfaces them must no-op cleanly when absent.** Never raise or
  emit an empty section on a clear/unenriched event.
- **Three render strategies, chosen by what the target actually accepts — never
  copy one adapter's approach to another without checking the target's schema:**
  1. **Rich-markup targets** (Slack Block Kit, Teams Adaptive Card, Jira ADF,
     GitHub Markdown) — build bespoke structured blocks directly from the four
     raw fields (separate sections/blocks, not one joined string).
  2. **Free-text targets** (Halo, OBM, ServiceNow, BMC Helix, Datadog `text`,
     OpsRamp `description`) — append `analysis_text(e)` from
     `com_event_core.enrich.render` (heading-prefixed, blank-line-paragraph
     block, `""` when no analysis) to the existing description/details string.
  3. **Structured JSON-record targets** (Elastic, Grafana, PagerDuty, Splunk) —
     add the four raw fields as new dict keys directly, same as other
     `CanonicalEvent` passthrough fields already on that record.
- **A target with flat-scalar-only columns needs the list field flattened —
  check for a pre-existing sign the adapter already avoids nested types.**
  Sentinel's Log Analytics Data Collector API only accepts flat scalar columns
  per record; the `_to_record()` adapter already omits the `tags` dict for
  exactly this reason. So `analysis_actions` (a list) is joined into one
  semicolon-separated numbered string there instead of sent as an array, while
  the other three fields stay scalar. Dynatrace's flat `com.*` properties get
  the same treatment (confidence formatted `.2f`, actions joined `" | "`).
  Splunk's HEC event, by contrast, already nests `tags` as a dict — proof it
  accepts nested JSON fine — so it gets `analysis_actions` as a raw list.
- General rule: before wiring a new optional field into N adapters, group them
  by what their target API actually accepts (free text vs. structured JSON vs.
  flat-scalar-only) and grep each adapter's existing code for a tell (an
  omitted dict field, a joined string) that reveals the target's real
  constraint — don't assume a JSON-looking target accepts arbitrary nesting.

## HPE Customer Advisories (`hpe_advisories` enricher) — API boundary + ordering (critical)

- **The supported entry point is the COM API, never a scraped HPE Support
  Center search.** Server `firmwareBundleUri` (payload, or a narrow
  `GET /servers/{id}` fallback) → `GET /firmware-bundles/{id}` → that bundle's
  own `advisories` URL is the only advisory document ever fetched. There is no
  separate, stable Customer Advisory REST API — the document behind
  `advisories` is HTML meant for humans, so parse it defensively (see below),
  not as a versioned contract.
- **Authenticate COM calls with client credentials, not a hand-issued PAT, for
  anything long-running.** `ComClient` tries `COM_CLIENT_ID` +
  `COM_CLIENT_SECRET`/`COM_CLIENT_SECRET_FILE` first — exchanged for a Bearer
  token via `POST https://sso.common.cloud.hpe.com/as/token.oauth2`
  (`grant_type=client_credentials`, form-encoded, not JSON), cached
  process-wide and refreshed ~60s before its `expires_in` (a verified real
  token: `expires_in: 7199`, i.e. ~2h) — before falling back to a static
  `COM_PAT`/`COM_PAT_FILE` (via `get_secret`, never `os.environ["COM_PAT"]`
  directly). A manually-issued PAT expires in the same ~2h with **no** refresh
  of its own, so it is fine for a short manual test but wrong for a
  shim/bridge that must keep running — use client credentials there. A `401`
  body reading `"Signature has expired"` from a *previously working* PAT means
  exactly this expiry, not a scope/config bug. A `401`/`403` in general means
  an expired/unscoped credential, not a code bug; the error message says so.
- **Enricher run order is decided by `Enricher.priority`, never by the
  `ENRICHERS` string.** `hpe_advisories` (`priority=10`) must run before
  `ilo_ai` (`priority=50`) so `event.advisory_evidence` is already set by the
  time `ilo_ai` builds the analyzer payload and includes it under
  `input.advisories`. `get_enrichers()` sorts the instantiated list by
  `priority` (stable sort, so equal-priority ties keep `ENRICHERS`' order) —
  so `ENRICHERS=ilo_ai,hpe_advisories` and `ENRICHERS=hpe_advisories,ilo_ai`
  run identically. This was a real bug class (an operator writing the "wrong"
  order silently dropped advisory context, no error) fixed by making
  correctness a framework property instead of a documentation convention. Any
  new enricher with an ordering dependency must set `priority` accordingly —
  never rely on config-order alone.
- **Two outputs, two purposes — don't collapse them into one field.**
  `advisory_evidence` (full Open/Resolved lists + bundle metadata) feeds the
  analyzer only; `advisory_references` (a small, keyword-matched subset) is
  what adapters render in a ticket. A misleading CA in a ticket is worse than
  none, so matching only ever narrows — never render the full unmatched list.
- **Cache by resolved bundle reference, not by event id.** Many servers in a
  fleet share one firmware bundle and the advisory document changes rarely
  (default TTL 24h) — re-fetching it per event is pure waste, same reasoning as
  `ilo_ai`'s per-`correlation_key` analysis cache.
- **Fail open, same contract as every other enricher.** A missing bundle
  reference, a COM API error, or a page that fails to parse must all end in
  "deliver the event unenriched", never a raised delivery failure.
- **Parser durability: no matching heading is a skip, not a best guess.** If
  neither an "Open Customer Advisories" nor a "Resolved Customer Advisories"
  heading is found, treat the result as empty (`parsed_ok: False`, logged) —
  never emit a partial or misaligned extraction from markup that doesn't match
  what the parser expects.
- **The advisories page renders via JS, but its data doesn't need to — the SPA
  itself fetches a static, same-origin JSON asset, verified live (2026-09).**
  A real firmware bundle's `advisories` URL
  (`support.hpe.com/.../a00sppdocen_US/spp/index.aspx?version=<v>`) returns
  only an empty Angular app shell to a static GET; browser devtools (Network →
  Fetch/XHR) showed the app itself loads `.../spp/assets/<v>.json` — a sibling
  static file with a clean `Advisories.OpendCAs`/`Advisories.ResolvedCAs`
  array (`CA`, `Description`, `CALink`, `FixedSPPVersion`, `Date`). `advisories.py`
  derives that URL from the page URL's own `version` param and reads it
  directly (`fetch_advisories_json()`) — no JS engine, no headless browser.
  Verified end-to-end against a real bundle: 8 Open + 6 Resolved CAs extracted
  correctly. **This is still an undocumented, unversioned asset path, not a
  published API** — treat it as a best-effort optimisation only: any failure
  (network error, missing key, reshaped body) must fall through to the static
  HTML heading parse (`parse_advisories_html()`, which itself fails open to
  "no advisories" on real pages, kept as the safety net), never raise or block
  delivery. Don't scrape a *different*, broader HPE endpoint on the strength
  of this precedent — this is still narrowly "the one static asset the
  official advisories URL itself already loads," not a general license to
  reverse-engineer HPE's backend.
- **A server's own `firmwareBundleUri` is a bundle it was directly, once
  UPDATED to — not necessarily the bundle its COM GROUP currently assigns, and
  a "resolved" CA is only proven fixed when those agree.** A COM group can
  have a firmware baseline assigned without every member being compliant with
  it (never updated, partial/failed update, drift since). Verified live: a
  real device had `firmwareBundleUri` pointing at one bundle while its group's
  `groupCompliance.firmware.status` was `"Not Compliant"` against a
  **different, newer** `bundleId` from `GET /groups/{id}/compliance` — i.e. the
  two signals disagreed on a real device, not a hypothetical. There is no
  reverse "which group is this device in" lookup (`filter=devices/any(...)`
  returns `400`); resolve it by paging `GET /groups` and matching each group's
  own `devices` array, bounded (`max_groups`) since this is O(groups) per
  un-cached device, not O(servers). Cache the compliance result per **device**
  id, separately from the per-**bundle** advisories cache — two devices can
  share a bundle but never share a compliance record. Feed `compliance` into
  the analyzer as evidence (`HPE_ADVISORIES_CHECK_COMPLIANCE`, default on) —
  don't reclassify "resolved" to "open" in code; let the prompt reason about
  whether the compliant bundle id matches the one the advisories came from.
- **A raw COM resource reference can OMIT the service-mount segment that the
  same resource's own `resourceUri` includes — never trust a `firmwareBundleUri`
  path verbatim.** Verified live: a server's `firmwareBundleUri` /
  `lastFirmwareUpdate.attemptedBaselineUri` read `/v1/firmware-bundles/<id>`
  (no `/compute-ops-mgmt` prefix) and 404s (`HPE_GL_ERROR_NOT_FOUND`) if GETed
  as-is; the *same* bundle's own `resourceUri` field (once fetched) reads
  `/compute-ops-mgmt/v1/firmware-bundles/<id>` and is what actually resolves.
  `ComClient.get_firmware_bundle()` patches this: a root-relative path missing
  `/compute-ops-mgmt/` gets it prepended before the GET. General rule: a
  "relative URI" field from a vendor API is not guaranteed rooted the same way
  every time — verify one live, don't assume symmetry with paths you already
  call correctly elsewhere.
- **`COM_BASE_URL` has no correct guessable default — GreenLake region names
  are not what you'd assume from the public docs' `<COM-API-base-URL>`
  placeholder.** `global`/`us1`/`eu1` all 404 for a real tenant; the real,
  working region host for one verified account was `eu-central` (i.e.
  `https://eu-central.api.greenlake.hpe.com`). Never hardcode a default; always
  require the operator to supply their own account's actual API base URL.
- **No undocumented GreenLake UI endpoints in product code.** The
  `/api/ui-doorway/...` fallback (served from `<region>.compute.cloud.hpe.com`,
  not the API host) was tried and removed: it is unversioned, its region hosts
  cannot be derived from `COM_BASE_URL`, and its `serverFirmwareCompliance_`
  score/deviations can read 100/empty while components are behind baseline. A
  server with no `firmwareBundleUri` (never updated through COM) gets a clear
  log line that advisories cannot be collected, not a guessed baseline.

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
- **`raise_for_status()` THROWS AWAY the vendor's explanation — never ship it on
  an adapter call.** Jira answers a rejected create with
  `{"errors":{"project":"valid project is required"}}`, but the logged error was
  only `Client error '400 Bad Request' for url .../rest/api/3/issue`, which is
  indistinguishable between a bad project, a bad issue type and a field missing
  from the create screen. Every adapter HTTP call must go through a helper that,
  on `r.is_error`, parses the body and raises with it (`_check()` in
  [jira.py](../com-event-core/com_event_core/adapters/jira.py)); fall back to
  `r.text[:500]` when the body isn't JSON. Rule: a delivery-path error message
  that doesn't name the rejected field costs an entire debugging round-trip.
- **A tenant/site id and a resource id are SEPARATE config — the wrong site fails
  as a FIELD error, not a 404.** `JIRA_URL` must be the Atlassian **site**
  (`https://acme.atlassian.net`), read from the browser address bar; one account
  commonly has several sites (prod + sandbox). Pointing at the wrong one makes
  create fail `400 project: valid project is required` and `GET /project/<KEY>`
  return `404 "No project could be found"` — i.e. it looks exactly like a wrong
  key or a payload bug, while the GUI shows the project perfectly. Auth being
  fine (`401` would say otherwise) is the clue that the *address* is wrong, not
  the credential. Verify with `GET {site}/rest/api/3/project/{KEY}` == `200`
  before blaming the payload. Related: `JIRA_ISSUE_TYPE` defaults to `Incident`,
  which exists only in **JSM** projects — Software/Business ship `Task`/`Bug`/
  `Story`/`Epic`, so confirm via `/issue/createmeta`.
- **A vendor identifier the GUI normalises can be CASE-SENSITIVE in the API, and
  it fails with the SAME error as a wrong tenant.** `JIRA_PROJECT_KEY=COMEvent`
  (retyped from memory) returns the identical
  `400 {"errors":{"project":"valid project is required"}}` as the wrong-site
  case, because to the API `COMEvent` simply is not a key — Jira stores keys
  upper-case. Three *different* faults (wrong site, wrong case, invalid issue
  type) collapse onto one message, so never conclude from the message alone:
  prove each input independently (`GET /project/<KEY>` == `200`, then
  `/issue/createmeta`), and copy identifiers verbatim from the address bar
  instead of retyping them into a `docker run -e` line.
- **Confirm the RUNNING artifact contains the fix before re-diagnosing.** After
  `_check()` was added, the shim still logged the old `raise_for_status()`
  wording — the container was a stale image built before that commit, so a round
  was spent re-analysing a bug already fixed in the tree. The tell is the
  message's *shape*, not its content. Verify with
  `docker run --rm --entrypoint sh <img> -c "grep -c '<new symbol>' <path>"`
  (`0` = stale) before trusting any log it emits.

## Outbound TLS through an intercepting proxy (critical)

- **`httpx` verifies against `certifi`, NOT the OS trust store — so a corporate
  TLS-inspecting proxy breaks every adapter while the browser works fine.**
  Symptom: `forward to <adapter> FAILED: [SSL: CERTIFICATE_VERIFY_FAILED]
  certificate verify failed: unable to get local issuer certificate`. The proxy
  re-signs the connection with an internal CA that `certifi` doesn't carry.
- **Fix is deployment config, not code: set `SSL_CERT_FILE`.** `httpx`'s
  `create_ssl_context()` honours `SSL_CERT_FILE` / `SSL_CERT_DIR` whenever
  `trust_env=True` (the default, and what our adapters use) — verified in
  httpx 0.28 `_config.py`. Never add a `verify=False` escape hatch: adapter
  requests carry credentials.
- **The bundle must be MERGED (internal CA + public roots) because `cafile=`
  REPLACES the trust store rather than adding to it.** A file containing only the
  corporate CA silently breaks every other target. Build it by exporting the
  Windows stores (`Cert:\{LocalMachine,CurrentUser}\{Root,CA}`, de-duped by
  thumbprint) and appending `certifi.where()`.
- **Interception is SELECTIVE by destination, so "other traffic works" proves
  nothing.** The shim's Service Bus/SQS connection kept working while Jira
  failed. Expect the same error on each *new* target added — treat
  `SSL_CERT_FILE` as standard on-prem deployment config, not a per-adapter
  workaround.
- **Validate the bundle against the REAL target, not just by parsing it.**
  `ssl.create_default_context(cafile=...)` loading N certs only proves the file
  is well-formed; `httpx.get("https://<target>/...")` returning `200` under
  `SSL_CERT_FILE` proves the chain actually satisfies the proxy.
- **A process that embeds a NATIVE/Node runtime needs its OWN CA var, and its
  failure mode looks like an auth error, not a TLS error.** The `ai-gateway`'s
  Copilot provider loads a Node-based SDK addon alongside the Python process;
  `SSL_CERT_FILE` fixes Python/httpx's outbound calls but the Node runtime
  needs `NODE_EXTRA_CA_CERTS` pointed at the same merged bundle. Missing it
  surfaced as a generic `Authorization error. Your credentials may be expired
  or invalid.`, indistinguishable from a genuinely bad/unlicensed PAT, so a
  real TLS gap can burn a debugging session chasing the wrong credential. Set
  both vars together whenever a component mixes runtimes.
- **Don't trust an undocumented "is this token valid" probe over the real call
  path.** `GET https://api.github.com/copilot_internal/v2/token` returned `404`
  for a PAT independently proven to work (byte-identical to a Key Vault secret
  used by a live deployment). It's an internal, unstable endpoint; the only
  authoritative check for Copilot access is the actual SDK/gateway call. When
  a quick side-channel probe disagrees with the real integration path, trust
  the real path.

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
- **`_normalize_server()`'s `mgmt_url` is HARDCODED `https://<bmc.ip>` with no
  port and no override** — so any Redfish target it points at (the iLO emulator
  included) MUST actually serve HTTPS on 443, or `ilo_ai`'s `RedfishClient` gets
  `Connection refused`/`wrong version number`. The `ilo-emulator` image bakes
  `ENV HTTPS=Disable`+`PORT=8000` (matching the Azure HOL lab's plain-HTTP
  simplification), so testing the REAL enrichment path requires overriding both
  (`-e HTTPS=Enable -e PORT=443`) — don't copy the lab's plain-HTTP run command
  when the goal is exercising `ilo_ai`, and set `ILO_INSECURE=1` (the emulator's
  self-signed cert has no `basicConstraints: CA:TRUE`, so `ILO_CA_BUNDLE` can't
  work against it).
- **To test the bridge + `ilo_ai` + ai-gateway pipeline locally end to end**:
  one Docker network; build the bridge image from the monorepo ROOT (context
  must include sibling `com-event-core`); run `ilo-emulator` (HTTPS/443 per
  above), `ai-gateway` (working Copilot PAT + CA bundle), a webhook echo target,
  and the bridge (`DELIVERY_MODE=spool`, `ENRICHERS=ilo_ai`, `ILO_INSECURE=1`).
  POST a flat `_normalize_server()`-shaped payload (no COM-API envelope — see
  that function for the exact field paths) to `/com/webhook` with the
  `x-shim-secret` header; a raise drives real Redfish reads + a real
  `POST .../agent/com-rca` before delivery, while a clear delivers in under a
  second with `analysis_*` all null/empty (`IloAiEnricher.wants()` gates on
  `action=RAISE`). Full recipe and verified payload shape in
  `/memories/repo/ai-gateway-copilot-auth.md`.

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

## AI Gateway (moved to its own repo)

The Copilot/OpenAI/Anthropic analyzer service formerly at `copilot-ai-gateway/`
in this repo has moved to a standalone, domain-neutral repo:
[jullienl/ai-gateway](https://github.com/jullienl/ai-gateway) — it has no
dependency on `com_event_core` or anything else here, only the small HTTP
"analyzer contract" documented in
[com-event-core/README.md](../com-event-core/README.md#the-analyzer-contract).

Its own `GUIDE.md` carries the detailed multi-tenancy/provider architecture
notes (per-request token resolution, `session_id` UUIDv5 hashing, per-tenant
secret files, the client-level-vs-per-session auth distinction, etc.) — do not
re-duplicate them here; update that repo's docs instead. This repo only needs
to know: it is one implementation of the analyzer contract, referenced from
[README.md](../README.md) and [com-event-core/README.md](../com-event-core/README.md),
and any other service implementing that same contract works as a drop-in
replacement.
