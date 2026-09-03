# HPE COM Event Integrations

Reference implementations that forward **HPE Compute Ops Management (COM)**
webhook events to your operational tooling — OBM, ServiceNow, Splunk, or any
generic webhook. Pick the deployment shape that fits your constraints; the event
normalisation, de-duplication, and target adapters are **shared** across all of
them.

> These are **reference/sample** implementations meant to be forked and adapted,
> not a supported HPE product.

## Which project do I use?

```
                    Can you receive COM's HTTPS webhook on a public endpoint?
                                          │
             ┌────────────────────────────┴────────────────────────────┐
            no / prefer managed cloud                          yes, I have an edge
                     │                                                   │
        ┌────────────┴───────────┐                        ┌─────────────┴─────────────┐
   Use a managed cloud?                                  Simplest possible footprint?
        │            │                                    │                          │
       yes          no, fully on-prem                    yes                    already using
        │            │                                    │                     OpsRamp / ServiceNow?
   com-event-relay   com-event-bridge                com-event-bridge                 │
   (+ shim)          (single box)                    (single box)              use the NATIVE
                                                                                COM integration
                                                                                (no code needed)
```

| Option | Project | When to use |
|--------|---------|-------------|
| **Cloud relay + on-prem shim** | [com-event-relay](com-event-relay) | You want a managed public receiver (Azure Container Apps / AWS App Runner) that enqueues events, drained by an outbound-only shim running next to your target. No inbound ports on-prem. |
| **Single on-prem box** | [com-event-bridge](com-event-bridge) | No cloud allowed, or you just want the smallest footprint. One container receives, transforms, and forwards in a single process, with an optional local disk spool for durability. |
| **Native integration** | — (product) | If you already run **OpsRamp** or **ServiceNow**, both have a **built-in COM integration** — no shim/relay needed. See the relay README's "native integrations" note. |

## Projects in this repo

- **[com-event-relay](com-event-relay)** — cloud relay (`relay/`, COM → queue) plus
  the outbound consumer (`shim/`, queue → target). Multi-cloud (Azure Service Bus
  or AWS SQS), container-first, with deploy scripts for ACA and App Runner.
- **[com-event-bridge](com-event-bridge)** — single-box on-prem all-in-one: one
  container that folds handshake + auth + transform + forward into a single
  process, with an optional on-disk spool. Ships with an nginx + certbot compose
  stack for the public TLS edge.
- **[com-event-core](com-event-core)** — the shared package used by the shim and
  the bridge: the COM event **normaliser** (`CanonicalEvent`), the **de-dup**
  store, and all **target adapters** (`obm`, `servicenow`, `splunk`, `webhook`).
  A mapping or adapter fix is made once here and both consumers get it.

## Targets supported

`obm` · `servicenow` · `splunk` · `webhook` — selected per deployment with
`TARGET=<name>`. Adding a new target is a small adapter in `com-event-core`
(map `CanonicalEvent` → the target's API); see that project's README.

## Images

CI builds and publishes multi-arch (amd64 + arm64) images to GHCR:

```
ghcr.io/<owner>/com-event-relay
ghcr.io/<owner>/com-event-shim
ghcr.io/<owner>/com-event-bridge
```

The images are **self-contained** — `git clone` + `docker build` (from the repo
root) works with nothing to publish first, because the shared `com-event-core`
package is installed into the shim/bridge images from local source.

## Documentation

- Each project has its own README with quick start, configuration, and deployment.
- On-prem hardening for the single box: [com-event-bridge/HARDENING.md](com-event-bridge/HARDENING.md).
- Design/background decks and appendices: [com-event-relay/docs](com-event-relay/docs).
- Publishing, CI/CD, and releasing: [MAINTAINERS.md](MAINTAINERS.md).

## License

MIT — see [LICENSE](LICENSE).
