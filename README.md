# HPE COM Event Integrations

Securely integrate **HPE Compute Ops Management (COM)** webhook events with **ITSM, ITOM, SIEM, SOAR, ChatOps, incident-response, and observability platforms** such as **HaloITSM**, **Jira Service Management, Splunk, Microsoft Sentinel, OBM, DataDog, Microsoft Teams, Slack, or any webhook-compatible target**.

The framework provides webhook validation, authentication, payload normalization, de-duplication, reliable delivery, raise/clear event correlation, and multiple deployment options — including an architecture that requires **no inbound network ports to be opened**.

The framework uses a shared `CanonicalEvent` model and a **rich, growing library of pluggable target adapters** — small, reusable components that translate normalized COM events into the API or webhook format expected by each destination. A single COM event can be **delivered simultaneously to multiple targets**, allowing the same event to trigger different workflows across ITSM, SIEM, monitoring, and collaboration platforms. COM-specific processing is implemented only once, while the same normalization, correlation, retry, and delivery logic is reused across all configured destinations. **New adapters can typically be added in minutes rather than days, with only a small amount of target-specific code.**

It ships as **ready-to-run, multi-architecture container images** published to **GitHub Container Registry (GHCR)** and configured entirely through environment variables and secrets — deploy quickly by passing your own parameters, with no code changes and nothing to build first.


> **Reference implementation**
>
> This repository provides open-source reference/sample implementations for HPE Compute Ops Management event integrations. It is **not an officially supported HPE product**. It is intended to demonstrate, accelerate, and simplify COM integration patterns and can be forked or adapted for specific environments.


---

## At a glance

<img src="docs/images/at-glance-diagram.png" alt="At glance architecture" width="900" />

Two deployment models are available:

- **Relay + Shim** — use a managed public cloud edge and keep the customer network outbound-only.
- **Bridge** — run a single all-in-one receiver when you can expose an HTTPS endpoint that COM can reach.

Both models share the same normalisation, de-duplication, correlation, and target-adapter logic from `com-event-core`.

---

## Contents

- [Why this project?](#why-this-project)
- [What it provides](#what-it-provides)
- [How it works](#how-it-works)
- [Typical use cases](#typical-use-cases)
- [Quick start](#quick-start)
- [Deployment models](#deployment-models)
- [Which deployment should I choose?](#which-deployment-should-i-choose)
- [Supported integrations](#supported-integrations)
- [When should I use a native COM integration?](#when-should-i-use-a-native-com-integration)
- [COM event lifecycle](#com-event-lifecycle)
- [Projects in this repository](#projects-in-this-repository)
- [Container images](#container-images)
- [Documentation](#documentation)
- [Roadmap](#roadmap)
- [License](#license)

---

# Why this project?

COM can send server-health transitions and alerts to HTTPS endpoints through webhooks.

In theory, a target that supports webhooks could receive those events directly. In practice, enterprise integrations usually require more than simply forwarding an HTTP payload.

| Challenge | Why direct webhook delivery is often insufficient |
|---|---|
| **Compatibility** | A target may support webhooks without supporting COM's verification handshake, shared-secret authentication, or payload structure. |
| **Payload transformation** | ITSM, ITOM, SIEM, monitoring, and incident-response products usually expect their own API or event schema. |
| **Reliability** | Production delivery commonly requires retry, buffering, retention, de-duplication, and safe handling of target outages. |
| **Event lifecycle** | A fault and its later recovery need to be correlated so the item opened by the raise can be resolved or closed. |
| **Security** | Hosting a public receiver can require inbound firewall access, certificate management, credential protection, and clear operational ownership. |
| **Fan-out** | The same COM event may need to create an incident, feed a SIEM, and notify an operations channel at the same time. |

This repository provides that integration layer once, with shared logic that can be reused across all supported targets.

---

# What it provides

## COM webhook compatibility

The receiver handles the COM webhook contract, including:

- verification handshake
- shared-secret authentication
- request validation and input hardening
- COM payload parsing

## Normalised event model

Every COM event is first converted into a neutral `CanonicalEvent`, and only then handed to a target adapter for delivery:

```text
COM-specific processing
        |
        v
  CanonicalEvent
        |
        v
Target-specific adapter
```

This keeps COM-specific parsing in one place and isolates it from the API details of each destination platform (ServiceNow, Jira, OpsRamp, Splunk, Datadog, and others). Each adapter only needs to understand the `CanonicalEvent`, not the COM webhook format.

## Pluggable adapters

A target adapter is a small component that translates a `CanonicalEvent` into the request expected by a specific destination platform. Adding an integration is primarily that mapping:

```text
CanonicalEvent -> target API
```

The COM receiver does not need to be redesigned for every new target.

## De-duplication

Repeated or redelivered COM events are suppressed so retries do not create duplicate tickets or alerts.

## Raise / clear correlation

A stable `correlation_key` ties a problem event to its recovery event.

Targets that support stateful objects can therefore close or resolve the object opened by the original raise.

## Reliable delivery

Depending on the deployment model:

- **Relay + Shim** uses Azure Service Bus or AWS SQS as the durable queue.
- **Bridge** can use an on-disk spool.

This allows receive and delivery to be decoupled and provides retry when a target is temporarily unavailable.

## Multi-target fan-out

A single COM event can be delivered independently to several different target adapters.

## Ready-to-deploy containerized solution

The Relay, Shim, and Bridge are packaged as ready-to-run container images, so deployment is quick and consistent across environments. All settings are supplied through configuration — the same images run everywhere without code changes.

- **Fast deployment** — prebuilt images start with a single run command; nothing to compile or package first.
- **Consistent runtime** — the same image behaves identically across development, test, and production.
- **Minimal host dependencies** — no language runtime or libraries to install on the host; only a container engine is required.
- **Simple upgrades** — move to a new version by pulling an updated image tag.
- **Easy rollback** — return to a previous version by redeploying an earlier image tag.
- **Portable deployment** — the same images run on-premises or on supported cloud platforms; only the supplied configuration changes.
- **Automation-friendly** — configuration through environment variables and secrets fits naturally into CI/CD and infrastructure-as-code workflows.

All deployment-specific behavior is supplied through configuration, including:

- COM shared secret
- deployment mode
- Azure / AWS queue settings
- target selection
- target credentials
- server conditions to monitor
- de-duplication settings
- delivery and timeout settings


## Secure outbound-only option

With **Relay + Shim**, the public endpoint lives in Azure or AWS and the on-premises shim consumes from the queue using outbound connectivity.

With this model, **no inbound network path is required into the customer environment**. (The **Bridge** model does require inbound HTTPS, since COM connects to it directly.)

---

# How it works

The core architecture intentionally separates COM-specific logic from target-specific logic.

<img src="docs/images/how-it-works-diagram.png" alt="At glance architecture" width="900" />

A new integration generally does **not** require changing the COM webhook receiver.

Instead, a target adapter maps:

<img src="docs/images/target-adapter-map-diagram.png" alt="At glance architecture" width="400" />


This keeps the COM contract, de-duplication, correlation, and delivery behavior consistent across adapters.

---

# Typical use cases

- Create or update a ticket, incident, or issue in an ITSM or incident-response platform (ServiceNow, Jira, PagerDuty, GitHub, and others) when a COM-managed server becomes unhealthy.
- Automatically resolve or close that item when COM reports the condition has recovered.
- Forward COM hardware and server events to a SIEM, ITOM/AIOps, or observability platform (Splunk, OpsRamp, Datadog, Dynatrace, Grafana, and others) for search, correlation, audit, or operational monitoring.
- Post operational notifications to a ChatOps channel such as Microsoft Teams or Slack.
- Integrate COM with a platform that does not natively understand the COM webhook contract.
- Deliver COM events to an internal application **without opening inbound firewall ports**.
- Fan one COM event stream out to several operational systems at once — for example, open a ServiceNow incident, feed the same event to Splunk for audit, and post a Slack notification to the operations channel.
- Insert custom transformation, filtering, authentication, or enrichment between COM and the target.

---

# Quick start

**Need the simplest deployment?**

→ [`com-event-bridge`](com-event-bridge/)

**Need an outbound-only customer architecture with no inbound port into the customer network?**

→ [`com-event-relay`](com-event-relay/)

**Want to understand event normalisation or add a target adapter?**

→ [`com-event-core`](com-event-core/)

**Want the fastest path from an empty environment to a working COM-to-target pipeline?**

Follow an end-to-end deployment runbook:

- [Azure relay + on-prem shim](com-event-relay/docs/Deploy-End-to-End-to-Azure.md)
- [AWS relay + on-prem shim](com-event-relay/docs/Deploy-End-to-End-to-AWS.md)
- [Bridge (single-box on-prem)](com-event-bridge/docs/Deploy-End-to-End-On-Prem.md)

---

# Deployment models

## 1. Relay + Shim

Recommended when the target resides in a restricted or private customer network.

<img src="docs/images/com-event-relay-architecture.png" alt="COM Event Relay architecture" width="800" />

### Benefits

- No inbound port into the customer network
- Cloud-managed public HTTPS edge
- Durable cloud queue
- Receive and delivery are decoupled
- Target outages do not require COM to retry directly
- Public receive and target delivery are decoupled and can be operated independently
- Suitable for private/internal targets

### Trade-offs

- Requires Azure or AWS
- Requires a cloud queue
- Two components are deployed: relay and shim
- Cloud resources introduce some operational cost

See [`com-event-relay`](com-event-relay/) for deployment details and [`end-to-end Azure deployment runbook`](com-event-relay/docs/Deploy-End-to-End-to-Azure.md) and [`end-to-end AWS deployment runbook`](com-event-relay/docs/Deploy-End-to-End-to-AWS.md) for step-by-step instructions to deploy the solution from scratch.

---

## 2. Bridge

Recommended when you can expose a public HTTPS endpoint that COM can reach and want the smallest deployment footprint.

<img src="docs/images/com-event-bridge-architecture.png" alt="COM Event Bridge architecture" width="800" />

The bridge performs the complete pipeline in one process:

```text
receive
  -> authenticate
  -> normalise
  -> de-duplicate
  -> correlate
  -> transform
  -> forward
```

### Benefits

- Single container
- No managed-cloud dependency
- Simple deployment model
- Optional on-disk spool for durable retry
- Full delivery path remains under your control

### Trade-offs

- You operate the public edge
- Requires public DNS
- Requires a CA-signed TLS certificate
- Requires inbound HTTPS connectivity to the bridge
- You own host and certificate lifecycle
- A single bridge does not provide the same queue-level resilience or horizontal receive/deliver scaling as Relay + Shim

The repository includes nginx + certbot support to help operate the public TLS edge.

See [`com-event-bridge`](com-event-bridge/) for deployment details and [`end-to-end deployment runbook`](com-event-bridge/docs/Deploy-End-to-End-On-Prem.md) for step-by-step instructions to deploy the Bridge solution from scratch.


---

# Which deployment should I choose?

<img src="docs/images/com-event-deployment-decision-tree.png" alt="COM Event Deployment Decision Tree" width="900" />

| Option | Use it when |
|---|---|
| **Relay + Shim** | You want the public edge in Azure/AWS, a durable queue, or no inbound network path into the customer environment. |
| **Bridge** | You can host the public HTTPS endpoint yourself and prefer a single all-in-one deployment. |
| **Native COM integration** | COM already provides a native integration for the target and that integration meets the requirement. |

---

## Why host the relay in Azure or AWS?

COM is a SaaS service and must send its webhook to a publicly reachable HTTPS endpoint.

That endpoint normally requires:

| Requirement | Managed-cloud relay |
|---|---|
| Public HTTPS URL | Provided by the managed application service |
| Valid TLS certificate | Managed by the platform |
| HTTPS ingress | Managed by the platform |
| Public host | Managed service rather than a customer-operated Internet-facing server |
| Scaling | Platform-managed |
| Durable buffering | Azure Service Bus or AWS SQS |

With the **Relay + Shim** architecture, only the cloud relay is publicly exposed. The on-premises shim connects outward to consume queued events and deliver them to the internal target.

This means there is **no inbound connection from COM into the customer network**.

---

## Which component does what?

| Feature | Bridge | Cloud Relay | On-prem Shim |
|---|---:|---:|---:|
| COM verification handshake | ✅ | ✅ | — |
| Shared-secret validation | ✅ | ✅ | — |
| Request/input hardening | ✅ | ✅ | — |
| Enqueue to durable cloud queue | — | ✅ | — |
| Consume from cloud queue | — | — | ✅ |
| Normalise to `CanonicalEvent` | ✅ | — | ✅ |
| De-duplication | ✅ | — | ✅ |
| Raise / clear correlation | ✅ | Event identity only | ✅ |
| Target adapter execution | ✅ | — | ✅ |
| Retry | ✅ via spool | Queue-driven | ✅ via redelivery |
| Durable buffer | Local spool | Cloud queue | Consumes cloud queue |
| Public HTTPS endpoint | ✅ you operate | ✅ platform-managed | — |
| Inbound customer-network path | Required | — | **Not required** |

---

# Supported integrations

This project ships with a rich set of built-in target adapters spanning **ITSM**, **ITOM / AIOps**, **SIEM**, **observability / monitoring**, and **incident-response / ChatOps**, plus a **generic webhook** — so a single COM event stream can drive service-management, operations, security, and collaboration platforms at the same time.

## Where the adapters live

All target adapters are implemented once in [`com-event-core`](com-event-core/) and reused by both deployment models — `com-event-relay` and `com-event-bridge` — so a fix or new mapping is inherited by both.

## Supported platforms

For the full list of supported platforms and their per-adapter details — category, role, authentication, whether COM offers a native integration, how a clear is delivered, and validation status — see the [supported-adapters table in `com-event-core`](com-event-core/README.md#supported-adapters), the single source of truth.

> ⚠️ **Reference implementations.** Every adapter is fully implemented against its target's API — connectivity, field mapping, authentication, and raise/clear handling are all in place. What is still pending for most is **validation against a live product**: the **GitHub**, **Slack**, and **Teams** adapters have been exercised end-to-end against a real target so far; the others have not yet been tested against a live instance (standing up every one of these platforms in a lab isn't feasible, and several also require paid licenses). Validate each adapter against your own environment before production use.
>
> 🙋 Contributions and live-tenant validation feedback are welcome. 

> 🚨 If you face any issue with an adapter integration, please [open an issue](https://github.com/jullienl/HPE-COM-Event-Integrations/issues) in the project.


**Target not listed?** You have two options:

- **Use the generic `webhook` adapter** to POST the `CanonicalEvent` as JSON to any HTTP endpoint — no code required.
- **Add your own adapter** if the target needs a specific API or payload shape — see [adding a new target](com-event-core/README.md#adding-a-new-target).


## Selecting one or more targets

Target adapters are selected with the `TARGETS` environment variable. Specify a single adapter or a comma-separated list to deliver each COM event to multiple destinations:

```bash
TARGETS=<name>
TARGETS=<name>,<name>,<name>
```

For example, to deliver events to Splunk, Microsoft Teams, and Jira Service Management:

```bash
TARGETS=splunk,teams,jira
```

Each adapter reads its own connection settings — URLs, credentials, tokens, and other target-specific parameters — from environment variables or mounted secret files. See [`com-event-core`](com-event-core/) and the individual project READMEs for configuration details.

### Multi-target delivery behavior

Each target is processed independently. De-duplication and raise/clear state are tracked per adapter, so the failure of one destination does not cause successful deliveries to be repeated.

If one target is temporarily unavailable:

1. deliveries to the other targets remain successful
2. the failed adapter is retried
3. successful deliveries are not duplicated

For reliable multi-target delivery, use the **Relay + Shim** queue or **Bridge spool mode**. Bridge `sync` mode is best-effort and does not provide durable retry.

> **Current limitation:** Multiple instances of the same adapter type — for example, two generic webhook targets or two Slack destinations — are not yet supported because adapters currently use global environment-variable names. Per-instance adapter configuration is listed in the [Roadmap](#roadmap).

---

# When should I use a native COM integration?

If COM already provides a native integration for the target **and it meets your requirements**, that path is normally the simplest option.

Today, this is particularly relevant for **ServiceNow** and **OpsRamp**.

Use the native integration when:

- the native integration's connectivity and workflow requirements meet your needs
- the native payload and behavior meet your requirements
- you do not need an additional buffering or transformation layer

Use this framework when you need capabilities such as:

- **no inbound firewall port into the customer environment**
- durable buffering and retry
- custom payload transformation or enrichment
- de-duplication
- raise/clear lifecycle correlation
- fan-out to several targets
- a target without native COM support
- a single shared integration architecture across multiple target products

Even for ServiceNow or OpsRamp, Relay + Shim can be useful when the internal endpoint must remain private.

---

# COM event lifecycle

The framework currently normalises server events, alert events, and a generic fallback.

| Resource | Delivered COM `type` | Behavior |
|---|---|---|
| Server | `compute-ops-mgmt/server` | Evaluates configured conditions such as health, power, connection, and subscription |
| Alert | `compute-ops-mgmt/alert` | Raises on alert creation and clears when the alert is resolved/deleted |
| Generic | Other resource types | Delivered as a generic raise event |

## Server conditions

Server snapshots can evaluate one or more conditions through:

```bash
SERVER_MONITORS=health,power,connection,subscription
```

`health` is the default.

Each condition receives its own correlation identity so, for example, recovery of a power condition cannot accidentally close a health incident for the same server.

## Raise and clear

For lifecycle-aware integrations, configure COM to send both:

1. the **problem transition**
2. the **recovery transition**

to the same relay or bridge endpoint.

The framework then assigns a stable correlation key, conceptually:

```text
server:<serial>:<condition>
```

or:

```text
alert:<id>
```

A later clear can therefore resolve the exact object created by the raise.

De-duplication also includes the event action, so a raise and its clear are never mistaken for the same delivery.

For the exact COM webhook filters and lifecycle examples, see the project documentation and [`com-event-core`](com-event-core/).

---

# Projects in this repository

- [`com-event-relay`](com-event-relay/) — cloud relay + outbound-only shim; use when the internal target must **not** be directly reachable from COM.
- [`com-event-bridge`](com-event-bridge/) — single-box all-in-one receiver; use when you can host the public HTTPS endpoint yourself and want the smallest footprint.
- [`com-event-core`](com-event-core/) — shared package (`CanonicalEvent`, normalisation, de-duplication, raise/clear correlation, all built-in adapters) used by both deployment models, so a fix or new mapping is made once and inherited by both.

---

# Container images

CI builds multi-architecture images for `amd64` and `arm64` and publishes them to GHCR.

```text
ghcr.io/jullienl/com-event-relay
ghcr.io/jullienl/com-event-shim
ghcr.io/jullienl/com-event-bridge
```

The images are self-contained. Building from the repository root includes the local `com-event-core` package, so a separate package publication step is not required.

---

# Documentation

## End-to-end deployment runbooks

Use the deployment runbooks for the fastest path from an empty environment to a working COM-to-target pipeline:

- [`Azure end-to-end deployment`](com-event-relay/docs/Deploy-End-to-End-to-Azure.md) — Azure relay + on-prem shim
- [`AWS end-to-end deployment`](com-event-relay/docs/Deploy-End-to-End-to-AWS.md) — AWS relay + on-prem shim
- [`Bridge end-to-end deployment`](com-event-bridge/docs/Deploy-End-to-End-On-Prem.md) — single-box on-prem deployment

Each flow covers the public receiver, COM webhooks, raise/clear behavior, and target delivery.

## Reference documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — implementation-level architecture and process mapping
- [`com-event-core/README.md`](com-event-core/README.md) — adapter framework and adding new targets
- [`com-event-relay/README.md`](com-event-relay/README.md) — relay/shim architecture and configuration
- [`com-event-bridge/README.md`](com-event-bridge/README.md) — bridge configuration and deployment
- [`com-event-bridge/HARDENING.md`](com-event-bridge/HARDENING.md) — on-prem hardening guidance

---

# Roadmap

Planned or candidate improvements include:

- **Multiple instances of the same adapter type**  
  Add per-instance configuration namespaces so two webhooks, two Slack targets, or two instances of another adapter can coexist.

- **Additional live-tenant validation**  
  Exercise more built-in adapters end-to-end against real target environments.

- **More target adapters**  
  New integrations remain small `CanonicalEvent -> target API` mappings in `com-event-core`.

- **Additional shim deployment helpers**  
  Provide ready-made ways to run the on-prem outbound shim as a supervised, auto-restarting service — for example a `systemd` unit for bare-metal/VM hosts and a Compose/Kubernetes manifest for container hosts — so it survives reboots and crashes without manual intervention.

- **Infrastructure-as-code templates**  
  Add declarative templates (Azure Bicep, AWS CloudFormation) that create the cloud relay stack — the receiver, the durable queue, and the required roles — in one reproducible command, plus a simplified parameter-driven entry point instead of running many individual CLI steps.

Contributions and real-world adapter feedback are welcome.

---

# License

MIT — see [`LICENSE`](LICENSE).
