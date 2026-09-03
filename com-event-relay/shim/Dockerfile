FROM python:3.12-slim

WORKDIR /app

# Monorepo build: the build context is the repository ROOT (see the CI workflow
# and docker-compose). All COPY paths are relative to the repo root so the image
# is self-contained — `git clone` + `docker build` works with no extra steps.

# Shared logic (normaliser, dedup, target adapters). Installed from local source
# so no PyPI publish is required; this also brings httpx transitively.
COPY com-event-core ./com-event-core
RUN pip install --no-cache-dir ./com-event-core

# Install shim dependencies first for better layer caching
COPY com-event-relay/shim/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (worker + shim-specific core: queue backends)
COPY com-event-relay/shim/worker.py .
COPY com-event-relay/shim/core ./core

# Run as a non-root user
RUN useradd --create-home --uid 10001 shim
USER shim

# Outbound-only consumer; no ports exposed.
CMD ["python", "worker.py"]
