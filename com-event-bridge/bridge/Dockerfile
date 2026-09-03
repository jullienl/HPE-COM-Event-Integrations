FROM python:3.12-slim

WORKDIR /app

# Monorepo build: the build context is the repository ROOT (see the CI workflow
# and docker-compose). All COPY paths are relative to the repo root so the image
# is self-contained — `git clone` + `docker build` works with no extra steps.

# Shared logic (normaliser, dedup, target adapters). Installed from local source
# so no PyPI publish is required; this also brings httpx transitively.
COPY com-event-core ./com-event-core
RUN pip install --no-cache-dir ./com-event-core

# Install bridge dependencies first for better layer caching
COPY com-event-bridge/bridge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (single-process app + bridge-specific core: spool)
COPY com-event-bridge/bridge/app.py .
COPY com-event-bridge/bridge/core ./core

# Persist the spool + dedup DBs on a mounted volume in production.
VOLUME ["/data"]
ENV SPOOL_PATH=/data/spool.db \
    DEDUP_DB_PATH=/data/dedup.db

# Run as a non-root user
RUN useradd --create-home --uid 10001 bridge \
    && mkdir -p /data && chown bridge /data
USER bridge

# Public receiver — exposes the app port (put a TLS reverse proxy in front).
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
