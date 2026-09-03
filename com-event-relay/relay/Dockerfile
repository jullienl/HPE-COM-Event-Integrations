FROM python:3.12-slim

WORKDIR /app

# Monorepo build: the build context is the repository ROOT (see the CI workflow
# and docker-compose). All COPY paths are relative to the repo root so the image
# is self-contained — `git clone` + `docker build` works with no extra steps.
#
# The relay does NOT depend on com-event-core (it only receives + enqueues).

# Install dependencies first for better layer caching
COPY com-event-relay/relay/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (app + queue abstraction package)
COPY com-event-relay/relay/app.py .
COPY com-event-relay/relay/core ./core

# Run as a non-root user
RUN useradd --create-home --uid 10001 relay
USER relay

EXPOSE 8080

# Container platforms (ACA, App Runner, ECS) provide public HTTPS + TLS in front.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
