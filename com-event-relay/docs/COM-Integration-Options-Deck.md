---
marp: true
title: Integrating HPE Compute Ops Management Events with ITSM / Event Platforms
author: Lionel Jullien
paginate: true
theme: default
class: lead
---

<!-- _class: lead -->

# Integrating COM Events with ITSM / Event Platforms

### Design options for a security-constrained (banking) environment

Compute Ops Management (COM) → OBM / Aria Operations / HaloITSM

<br>

*Prepared for customer design discussion*

---

## Objective

Automatically turn **HPE Compute Ops Management (COM)** events
(server health, connectivity, lifecycle, jobs) into actionable outcomes:

- **Incidents / cases** in **HaloITSM** (system of record)
- **Correlated events** in **OBM** (event consolidation hub)
- **Monitoring signal** in **VMware Aria Operations** (AIOps / performance)

**Goal:** hands-off, real-time IT operations — no manual monitoring.

---

## The platforms and their roles

| Platform | Layer | Role |
|---|---|---|
| **COM** | Source | Emits server hardware / lifecycle events |
| **Aria Operations** | Monitoring / AIOps | Performance & capacity (VMware-centric, can cover physical via mgmt packs) |
| **OBM** (OpenText Operations Bridge Mgr) | Event consolidation | De-duplication & correlation hub |
| **HaloITSM** | ITSM | Ticket / case system of record |
| **"Thin shim"** | Middleware | Lightweight adapter: receive → transform → forward |

> These are **layers**, not competing choices. COM connects to each the same way.

---

## Why a "thin shim" is always required

COM **cannot talk to Halo / OBM / Aria directly** — three mismatches:

| Mismatch | COM behaviour | Target requirement |
|---|---|---|
| **Handshake** | Sends `GET` with header `x-compute-ops-mgmt-verification-challenge: <token>` | Reply `200` + `content-type: application/json` + body `{"verification":"<token>"}` |
| **Auth** | Static custom `headers` on the webhook only | OAuth2 bearer / per-call token |
| **Payload** | `POST` with the full resource representation (JSON) | Target-specific schema |

The shim answers the handshake, authenticates, and **transforms** each event.

> Fail the handshake → webhook goes `WARNING` / `DISABLED`.

---

## The core challenge (the crux)

**Webhooks are** ***push***. COM (cloud/SaaS) **initiates an inbound
connection** to a listener that must be **publicly reachable** on the internet.

<br>

```
COM (cloud)  ──►  Public HTTPS listener (inbound)  ──►  Shim  ──►  OBM / Aria / Halo
                   ▲
                   └── requires public endpoint / DMZ / handshake
```

<br>

This inbound-from-internet requirement is the central design constraint.

---

## Customer constraint

> *"The webhook needing to be publicly accessible is not something we're
> comfortable with. Making something publicly available on the internet opens
> a whole new set of banking requirements our team has no experience with."*

**Hard requirements derived:**
- ❌ No inbound connections from the internet
- ❌ No public endpoint / DMZ exposure to stand up
- ✅ Outbound HTTPS to approved SaaS is already permitted

---

## A secondary design concern: duplicate cases

COM resends the **full resource on every state change**, and one root cause
(e.g. a PSU fault) can raise **multiple related events**.

- **Direct shim → Halo** → risk of **duplicate tickets** unless the shim
  builds its own de-duplication (lookup + update by resource/alert ID).
- **Via OBM or Aria** → native **de-duplication & correlation** collapses
  many events into **one incident** before it reaches Halo.

> If OBM/Aria is already the event hub, route through it to keep Halo clean.

---

## Option overview

| # | Option | Public inbound to bank? | Keeps COM filtering? | Custom dev burden |
|---|---|:--:|:--:|:--:|
| 1 | On-prem webhook + DMZ | ❌ Yes (rejected) | ✅ | Low |
| 2 | **Polling** the COM API | ✅ No | ❌ **Lost** | **High** (bank-owned) |
| 3 | **Cloud relay** (recommended) | ✅ No | ✅ | Low |

---

## Option 1 — On-prem webhook + DMZ

**Pattern:** Public listener in the bank DMZ receives COM webhooks directly.

- ✅ Real-time, keeps COM `eventFilter`
- ✅ Simplest data flow
- ❌ **Requires public inbound endpoint** — explicitly rejected by the customer
- ❌ New firewall / DMZ / certificate / exposure review

> **Status: ruled out** by customer security posture.

---

## Option 2 — Polling the COM API

**Pattern:** Shim inside the bank makes **outbound** calls to the COM API on a
schedule, pulling servers / alerts / jobs and diffing state.

- ✅ **Outbound-only** — no public endpoint, no handshake
- ❌ **Loses COM's server-side `eventFilter`**
- ❌ Bank must build & maintain a **custom delta/diff script**
  (track "last seen", re-implement old/new/changed logic)
- ❌ Not real-time (interval latency)

> **Status: workable but not recommended** — shifts real dev burden onto the bank.

---

## Option 3 — Cloud relay (recommended)

Keep the webhook **and its filtering**; move the public receiver into a
**customer-owned cloud tenant**. Bank reads **outbound-only**.

```
COM ──webhook (eventFilter applied in cloud)──► Customer cloud tenant
                                                (Function / API GW + queue)
                                                        │
Bank shim ──outbound HTTPS pull from queue─────────────┘
   │
   └──► OBM / Aria / HaloITSM
```

Handshake + filtering handled in the cloud; **no inbound to the bank**.

---

## Why Option 3 is the sweet spot

| Concern | How it's addressed |
|---|---|
| No public endpoint **in the bank** | Public receiver lives in customer cloud tenant |
| Keep COM **filtering** | `eventFilter` still runs server-side in COM |
| No custom diff script | Event *is* the change — no polling logic |
| Bank stays **outbound-only** | Shim drains cloud queue over outbound HTTPS |
| Handshake | Handled once by the cloud relay |
| De-duplication | Still done downstream in OBM / Aria |

**Trade-off:** one small managed cloud component in the customer's own tenant.

---

## Comparison at a glance

| Criterion | On-prem DMZ | Polling | **Cloud relay** |
|---|:--:|:--:|:--:|
| No public inbound to bank | ❌ | ✅ | ✅ |
| Real-time delivery | ✅ | ❌ | ✅ |
| Keeps COM `eventFilter` | ✅ | ❌ | ✅ |
| No custom bank-owned code | ✅ | ❌ | ✅ |
| Native de-dup (OBM/Aria) | ✅ | ✅ | ✅ |
| Extra cloud component | — | — | ⚠️ (customer tenant) |

---

## Recommendation

1. **Rule out** the on-prem public endpoint (Option 1) — matches customer stance.
2. **Prefer the cloud relay (Option 3)** — preserves real-time delivery **and**
   COM's server-side filtering, with **no inbound exposure** and **no custom
   filtering code** for the bank.
3. **Route through OBM (or Aria)** for native de-duplication / correlation so
   HaloITSM receives **one case per real issue**.
4. Fall back to **polling (Option 2)** only if no sanctioned cloud tenant exists
   — accepting the filtering/dev burden as the explicit cost.

---

## Decisions needed from the customer

- Does the bank have a **sanctioned cloud landing zone** (Azure / AWS)?
- Which platform is the **destination of record** — OBM, Aria, or Halo direct?
- Which **COM events** matter (health leaves OK, disconnect, failed jobs…)?
- Acceptable **latency** (real-time vs. minutes)?
- Who **owns/operates** the shim + secret storage (GreenLake API credentials)?

---

<!-- _class: lead -->

# Next step — Option 3 build specification

### What must be in place on each side

The cloud relay and the on-prem shim have **distinct, complementary roles**.
The following slides detail the services, features and processes required for
each, to scope the implementation work.

---

## End-to-end flow (who does what)

```
        CLOUD TENANT (customer-owned, public)         │        BANK (private, outbound-only)
                                                       │
COM ──webhook──► [1] Ingress endpoint (HTTPS)          │
                     │  answers handshake              │
                     ▼                                 │
                 [2] Auth / signature check            │
                     │                                 │
                     ▼                                 │
                 [3] Enqueue event ──► [4] Message      │
                                          queue ◄──────┼──── [5] Shim pulls (outbound HTTPS)
                                                       │            │
                                                       │            ▼
                                                       │      [6] Transform + de-dup state
                                                       │            │
                                                       │            ▼
                                                       │      [7] Forward ──► OBM / Aria / Halo
```

---

## A) Cloud tenant — services & features

**Purpose:** receive filtered COM webhooks, complete the handshake, buffer events.

| # | Component | Example (Azure / AWS) | Role |
|---|---|---|---|
| 1 | **Public HTTPS ingress** | API Management / API Gateway, or Function URL | TLS endpoint COM posts to; valid public cert |
| 2 | **Compute / handler** | Azure Function / AWS Lambda | Answers handshake, validates, enqueues |
| 3 | **Message buffer** | Service Bus / Storage Queue · SQS | Durable store the shim drains |
| 4 | **Secret store** | Key Vault / Secrets Manager | Shared-secret header, queue creds |
| 5 | **Logging / monitoring** | App Insights / CloudWatch | Delivery audit, alerting, retries |

---

## A) Cloud tenant — processes

1. **Handshake responder** — on COM `GET` with
   `x-compute-ops-mgmt-verification-challenge`, reply `200` +
   `content-type: application/json` + `{"verification":"<token>"}`.
2. **Inbound authentication** — validate the COM webhook's **static custom
   header** (shared secret) so only COM can post events.
3. **Enqueue** — write the raw event payload to the durable queue (no transform;
   keep the relay "thin").
4. **Retention & retry** — buffer events until the shim acknowledges; dead-letter
   on repeated failure.
5. **TLS / certificate lifecycle** — maintain a valid public certificate.

> The relay does **not** transform payloads or talk to Halo/OBM/Aria — it only
> receives, verifies, and buffers.

---

## B) Bank shim — services & features

**Purpose:** pull buffered events outbound-only, transform, de-dup, forward.

| # | Component | Example | Role |
|---|---|---|---|
| 5 | **Outbound queue client** | Service Bus / SQS SDK (over 443) | Drains events from cloud queue |
| 6 | **Runtime host** | Container / VM / service (systemd) | Runs the shim inside the bank |
| 6 | **State store** | Local DB / Redis / file | Correlation keys for de-dup |
| 7 | **Target connectors** | OBM WS listener · Aria API · Halo OAuth2 | Forward the transformed event |
| — | **Secret store** | Vault / bank KMS | Queue creds + target API secrets |
| — | **Logging** | Bank SIEM | Audit + operational monitoring |

---

## B) Bank shim — processes

1. **Outbound queue drain** — long-poll / subscribe to the cloud queue over
   outbound HTTPS (443); no inbound port opened.
2. **Transform** — map the COM resource JSON to the target schema
   (OBM event / Aria notification / Halo ticket fields).
3. **De-duplication / correlation** — key on COM resource + alert ID; create vs.
   update; **or** delegate de-dup to OBM/Aria downstream.
4. **Authenticated forward** — push to the target
   (Halo = OAuth2 client-credentials → short-lived bearer token).
5. **Acknowledge** — confirm to the queue only after successful forward
   (at-least-once delivery, idempotent writes).
6. **Retry / dead-letter** — handle target `429`/`5xx`; respect Halo's
   700 req / 300 s rate limit.

---

## Shared prerequisites & responsibilities

| Item | Cloud tenant | Bank shim |
|---|:--:|:--:|
| Public HTTPS + cert | ✅ owns | — |
| COM handshake | ✅ | — |
| Inbound secret validation | ✅ | — |
| Event buffering | ✅ | reads |
| Payload transform | — | ✅ |
| De-dup / correlation | — | ✅ (or OBM/Aria) |
| Target auth + forward | — | ✅ |
| Outbound HTTPS (443) only | — | ✅ |

**COM side (either party):** create webhook with `eventFilter`, set the shared
secret header, point `destination` at the cloud ingress URL, confirm `ACTIVE`.

---

## Prerequisites checklist (before build)

- [ ] Sanctioned **cloud tenant/landing zone** (Azure or AWS) confirmed
- [ ] **HPE GreenLake API client** (Client ID/Secret) for creating the webhook
- [ ] Agreed **shared-secret header** value + storage location
- [ ] **Target platform** decided (OBM / Aria / Halo) + its API credentials
- [ ] **Field-mapping** COM → target schema defined
- [ ] Bank firewall allows **outbound 443** to the cloud queue endpoint
- [ ] **De-dup owner** decided (shim logic vs. OBM/Aria native)
- [ ] Ops: logging, alerting, dead-letter and **rate-limit** handling agreed

---

<!-- _class: lead -->

# Thank you

### Questions & next steps

Proposed next step: confirm cloud-tenant availability →
design the cloud-relay flow (handshake, filtered events, outbound queue drain).
