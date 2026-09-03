# Maintainer guide — HPE-COM-Event-Integrations

Internal notes for **publishing and maintaining** this monorepo. None of this is
needed by customers who just deploy an image — it covers the repo layout, how the
images are built, CI/CD, and releasing.

This repository holds three related projects plus one shared package:

| Path                | What it is                              | Image                     |
|---------------------|-----------------------------------------|---------------------------|
| `com-event-core/`   | Shared package (normaliser, dedup, adapters) — no image | — (installed into the images from source) |
| `com-event-relay/`  | Cloud relay (`relay/`) + outbound consumer (`shim/`)    | `com-event-relay`, `com-event-shim` |
| `com-event-bridge/` | Single-box on-prem all-in-one           | `com-event-bridge`        |

They share code (`com-event-core`) and version together, which is why they live
in one repo.

## 1. Repository layout

```
HPE-COM-Event-Integrations/
  .github/workflows/
    build-relay.yml      # CI: relay image
    build-shim.yml       # CI: shim image  (rebuilds on com-event-core changes too)
    build-bridge.yml     # CI: bridge image (rebuilds on com-event-core changes too)
  com-event-core/        # shared package (com_event_core/)
  com-event-relay/       # relay/ + shim/ + deploy/ + docs/ + docker-compose.yml
  com-event-bridge/      # bridge/ + deploy/ + docker-compose.yml
  README.md              # top-level overview + "which project do I use?"
  MAINTAINERS.md         # this file
  .gitignore
```

## 2. Build model — self-contained images (no PyPI)

The shared `com-event-core` package is **not published to PyPI**. Instead, every
image is built with the **repository root as the Docker build context**, and the
shim/bridge Dockerfiles `COPY com-event-core` and `pip install ./com-event-core`
from local source. This means **`git clone` + `docker build` just works** — there
is nothing to publish first.

Consequences maintainers must respect:

- The three Dockerfiles use **repo-root-relative** `COPY` paths
  (`com-event-relay/relay/...`, `com-event-core`, etc.). Do not change them to
  sub-folder-relative paths.
- The CI workflows and both `docker-compose.yml` files set the build **context to
  the repo root** (`.` in CI, `..` in compose since the compose files live one
  level down).
- `com-event-core` is **not** listed in `shim/requirements.txt` or
  `bridge/requirements.txt` (it is installed from source in the Dockerfile).
  `httpx` comes transitively from `com-event-core`.

## 3. How the CI/CD pipeline works

Three workflows in [.github/workflows](.github/workflows) build and push images to
the **GitHub Container Registry** (`ghcr.io`):

- [build-relay.yml](.github/workflows/build-relay.yml) → `com-event-relay`,
  from `com-event-relay/relay/Dockerfile`.
- [build-shim.yml](.github/workflows/build-shim.yml) → `com-event-shim`,
  from `com-event-relay/shim/Dockerfile`.
- [build-bridge.yml](.github/workflows/build-bridge.yml) → `com-event-bridge`,
  from `com-event-bridge/bridge/Dockerfile`.

All three:

- **Trigger** on pushes to `main`, on version tags (`v1.2.3`), and on pull
  requests (PRs build only — no push). Each is **path-filtered** so only the
  affected image rebuilds. The shim and bridge filters **also include
  `com-event-core/**`**, so a change to the shared package rebuilds both consumer
  images.
- **Authenticate** to GHCR with the automatic `GITHUB_TOKEN` (nothing to
  configure).
- **Build** for both `linux/amd64` and `linux/arm64`.
- **Tag** with `latest` (on `main`), the semver (`1.2.3`, `1.2`), the branch name,
  and the git SHA (via `docker/metadata-action`).
- **Cache** layers between runs.

Image names follow the repo owner: `ghcr.io/<owner>/com-event-relay`,
`.../com-event-shim`, `.../com-event-bridge`.

### Repo settings the workflow needs

- **Actions enabled:** Settings → Actions → General → allow actions to run.
- **Workflow write permission for packages:** the workflows already request
  `permissions: packages: write`. If pushes are denied, check Settings → Actions →
  General → *Workflow permissions* is set to "Read and write permissions".

## 4. One-time setup after the first successful build

1. **Make the packages public** so customers can pull without authenticating:
   GitHub profile → **Packages** → each of `com-event-relay`, `com-event-shim`,
   `com-event-bridge` → **Package settings** → visibility → **Public**.

   > GHCR packages are **private by default** even when the repo is public, and
   > they do not inherit repo visibility — do this once per image.

2. **Link each package to the repo** (optional, nice for discoverability) on the
   same Package settings page.

## 5. Cutting a release

The three images share the repo's tags. Tag a semver release to produce pinned
image tags alongside `latest`:

```bash
git tag v0.1.0
git push origin v0.1.0     # publishes :0.1.0 (+ :0.1) for each changed image, updates latest
```

Use semantic versioning across the repo: patch for fixes, minor for
backward-compatible features (new queue backend or adapter), major for breaking
changes to config/behavior or to `com-event-core`'s `CanonicalEvent` contract.

> Because the images install `com-event-core` **from source at build time**, there
> is no separate "publish com-event-core first" step — bumping the version in
> [com-event-core/pyproject.toml](com-event-core/pyproject.toml) and committing is
> enough; the next image build picks it up.

## 6. If you publish under an org instead of a user

The image path follows the repo owner automatically in CI, but the **deploy
scripts hardcode** the image path. If the owner isn't `jullienl`, update the
`IMAGE` default in:

- [com-event-relay/deploy/azure/deploy-relay-azure.sh](com-event-relay/deploy/azure/deploy-relay-azure.sh)
- [com-event-relay/deploy/aws/deploy-relay-aws.sh](com-event-relay/deploy/aws/deploy-relay-aws.sh)

## 7. Local build sanity check before pushing

Build from the **repo root** (that is the required context):

```bash
# relay
docker build -f com-event-relay/relay/Dockerfile -t com-event-relay:test .
# shim (includes com-event-core)
docker build -f com-event-relay/shim/Dockerfile -t com-event-shim:test .
# bridge (includes com-event-core)
docker build -f com-event-bridge/bridge/Dockerfile -t com-event-bridge:test .
```

Or use the per-project compose stacks (they already set the root context):

```bash
cd com-event-relay  && docker compose up --build            # relay (+ --profile shim)
cd com-event-bridge && docker compose up --build            # bridge + nginx + certbot
```

## 8. Release checklist

- [ ] `LICENSE` present; headers still say "reference implementation".
- [ ] No secrets/`.env` committed (`git ls-files | grep -Ei 'env|secret'`).
- [ ] All three `docker build`s succeed locally from the repo root.
- [ ] READMEs' quick starts still accurate (endpoints, env vars, install steps).
- [ ] `com-event-core` version bumped if its behaviour changed.
- [ ] Version tag pushed; CI green; images visible in GHCR and **public**.

## 9. Working on the shared package (`com-event-core`)

- **Local development** across the sibling projects: from a consumer subfolder
  (`com-event-relay/shim` or `com-event-bridge/bridge`) run
  `pip install -e ../../com-event-core`, or from the repo root
  `pip install -e ./com-event-core`, so edits are picked up without a rebuild.
- **Adding a target adapter** is a change to `com-event-core` only: a new module
  under `com_event_core/adapters/` implementing `TargetAdapter`, plus a line in the
  `_ADAPTERS` table in `com_event_core/adapters/__init__.py`. Both the shim and the
  bridge pick it up via `TARGET=<name>` with no other changes. Bump the
  `com-event-core` minor version.

## 10. Porting the relay/shim to another cloud (GCP, OCI, ...)

The relay/shim design is abstracted along two pluggable seams:

- **Queue backend.** Both sides depend only on an interface —
  [`QueuePublisher`](com-event-relay/relay/core/queue/base.py) (relay) and
  [`QueueConsumer`](com-event-relay/shim/core/queue/base.py) (shim), selected by
  the `QUEUE_BACKEND` env var. Adding e.g. GCP Pub/Sub is a **new pair of files**
  (a publisher + a consumer) plus a branch in each factory — `app.py` and
  `worker.py` stay unchanged.
- **Public HTTPS host.** The relay is a plain FastAPI container — run it on any
  managed container host (GCP Cloud Run, OCI Container Instances, Knative, or
  Kubernetes + Ingress + cert-manager) that provides auto-TLS and a public URL.

What's **missing today** to add a cloud: a non-Azure/AWS queue backend module and
a third deploy script (only `deploy/azure` and `deploy/aws` exist). So porting to
GCP ≈ **2 small queue modules + 1 deploy script**, not a redesign.

For a **fully on-prem, no-cloud** deployment, prefer `com-event-bridge` (single
box, no queue), or implement a self-hosted broker backend (RabbitMQ, Redis
Streams, Kafka) behind the same queue interfaces with your own public edge.
