"""
COM event bridge — single-box, on-prem, no queue, no cloud.

Folds the whole COM -> target path into ONE process:

  1. Answers the COM verification handshake (echoes the challenge token).
  2. Authenticates events via a shared-secret header (only COM knows it).
  3. Normalises the COM payload into a CanonicalEvent.
  4. Delivers it to the selected TARGET adapter, either:
       - DELIVERY_MODE=spool (default): persist to a local on-disk spool, ack COM
                               with 202 immediately, and let a background worker
                               drain + retry (survives target outages, no cloud
                               queue). This is the safe default. It REQUIRES
                               SPOOL_PATH to point at durable storage (a mounted
                               volume); the bridge refuses to start otherwise, so
                               an ephemeral path can't silently lose the backlog
                               on restart.
       - DELIVERY_MODE=sync  : forward inline (opt-in, best-effort). COM webhooks
                               are fire-and-forget (one POST, no retries), so a
                               target failure returns 5xx but the event is LOST.
                               Worse, a down target means sustained 5xx, and 10
                               consecutive webhook failures DISABLE the webhook in
                               COM (all delivery stops until manually re-enabled).

Unlike com-event-relay, this is the PUBLIC edge itself — put a TLS-terminating
reverse proxy (nginx/Caddy) in front and see HARDENING.md.

AI-generated reference implementation. Review and harden before production use.

Run locally:
    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import hmac
import json
import logging
import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response

from com_event_core import DedupStore, get_adapter, normalize
from core.spool import SpoolFull, SpoolStore, SpoolWorker


# --- Structured (JSON) logging -------------------------------------------
class JsonLogFormatter(logging.Formatter):
    """Emit one JSON object per log line so log pipelines can parse fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("event_id", "event_type", "status", "bytes", "mode"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload)


_handler = logging.StreamHandler()
_handler.setFormatter(JsonLogFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
log = logging.getLogger("com-event-bridge")

# --- Config (fail fast if anything mandatory is missing) -----------------
COM_SECRET = os.environ["COM_SHARED_SECRET"]
SECRET_HEADER = os.environ.get("SHARED_SECRET_HEADER", "x-shim-secret").lower()
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(256 * 1024)))  # 256 KB
DELIVERY_MODE = os.environ.get("DELIVERY_MODE", "spool").strip().lower()  # spool | sync

CHALLENGE_HEADER = "x-compute-ops-mgmt-verification-challenge"

if DELIVERY_MODE not in ("sync", "spool"):
    raise ValueError(f"DELIVERY_MODE must be 'sync' or 'spool', got '{DELIVERY_MODE}'.")

# spool mode is the safe default, but it's only durable if SPOOL_PATH lives on
# persistent storage. We refuse to fall back to an ephemeral default: inside a
# container that would silently discard the pending backlog on restart/redeploy,
# giving false durability. Fail fast at startup so the operator makes a choice.
if DELIVERY_MODE == "spool" and not os.environ.get("SPOOL_PATH"):
    raise ValueError(
        "DELIVERY_MODE=spool requires SPOOL_PATH to point at durable storage "
        "(a mounted volume) — e.g. /data/spool.db in the container (mount a volume "
        "at /data) or /var/lib/com-event-bridge/spool.db on bare metal. Refusing "
        "to start with an ephemeral default that would silently lose the spooled "
        "backlog on restart. Set SPOOL_PATH, or set DELIVERY_MODE=sync for "
        "best-effort inline delivery (events are lost if the target is down)."
    )

# Select + validate the target adapter once, at startup.
adapter = get_adapter()
dedup = DedupStore()

# In spool mode, a durable buffer + background drain worker are created at startup.
spool: SpoolStore | None = None
worker: SpoolWorker | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global spool, worker
    if DELIVERY_MODE == "spool":
        spool = SpoolStore()
        worker = SpoolWorker(spool, adapter, dedup)
        worker.start()
        log.info("started in spool mode; %s event(s) already pending", spool.pending())
    else:
        log.info("started in sync mode; forwarding inline to target=%s", adapter.name)
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()
        if spool is not None:
            spool.close()
        dedup.close()


app = FastAPI(title="COM event bridge", lifespan=lifespan)


@app.get("/healthz")
def liveness():
    """Liveness: the process is up."""
    return {"status": "ok"}


@app.get("/readyz")
def readiness():
    """Readiness: process is up and (spool mode) the drain worker is alive."""
    if DELIVERY_MODE == "spool" and (worker is None or not worker.is_alive()):
        raise HTTPException(status_code=503, detail="spool worker not running")
    return {"status": "ready", "mode": DELIVERY_MODE}


@app.api_route("/com/webhook", methods=["GET", "POST"])
async def webhook(request: Request):
    # 1) Handshake — respond with the exact body/content-type COM expects.
    challenge = request.headers.get(CHALLENGE_HEADER)
    if challenge is not None:
        log.info("handshake received; echoing verification token")
        return Response(
            content=f'{{"verification": "{challenge}"}}',
            media_type="application/json",
            status_code=200,
        )

    if request.method != "POST":
        raise HTTPException(status_code=400, detail="missing challenge header")

    # 2) Authenticate the event via the shared-secret header (constant-time).
    received = request.headers.get(SECRET_HEADER, "")
    if not received or not hmac.compare_digest(received, COM_SECRET):
        log.warning("rejected event: missing or invalid shared secret",
                    extra={"status": 401})
        raise HTTPException(status_code=401, detail="unauthorized")

    # 3) Enforce a body size limit (public endpoint hardening).
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    # 4) Parse JSON up front so a malformed body is rejected (not spooled).
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid json")

    event_type = request.headers.get("x-compute-ops-mgmt-event-type", "unknown")

    # 5) Deliver according to the configured mode.
    if DELIVERY_MODE == "spool":
        return _accept_to_spool(body, event_type)
    return _forward_sync(payload, event_type)


def _forward_sync(payload: dict, event_type: str) -> Response:
    """Inline delivery (best-effort): forward now. On failure we return 503, but
    COM is fire-and-forget and will NOT resend — the event is lost. A persistently
    down target also means repeated 5xx, and 10 consecutive webhook failures
    DISABLE the webhook in COM (stopping all delivery). Use spool mode if either
    is unacceptable."""
    event = normalize(payload)

    if dedup.is_duplicate(event.dedup_key):
        log.info("duplicate event; skipping",
                 extra={"event_id": event.event_id, "status": 200, "mode": "sync"})
        return Response(status_code=200)

    try:
        adapter.forward(event)
    except Exception:
        log.exception("forward failed; returning 503 (COM does not retry — event lost)",
                      extra={"event_id": event.event_id, "status": 503, "mode": "sync"})
        raise HTTPException(status_code=503, detail="target temporarily unavailable")

    log.info("event forwarded",
             extra={"event_id": event.event_id, "event_type": event_type,
                    "status": 202, "mode": "sync"})
    return Response(status_code=202, headers={"x-bridge-event-id": event.event_id})


def _accept_to_spool(body: bytes, event_type: str) -> Response:
    """Durable delivery: persist and ack immediately; the worker drains + retries.

    If the spool is over budget (prolonged target outage) we return 503 as
    backpressure rather than fill the disk. Note COM is fire-and-forget and will
    NOT resend, so an event rejected here is dropped — size SPOOL_MAX_BYTES for
    your worst-case outage so this stays a last-resort safety valve.
    """
    assert spool is not None  # created in spool mode at startup
    try:
        row_id = spool.put(body, {"event_type": event_type, "source": "com-event-bridge"})
    except SpoolFull:
        log.error("spool full; returning 503 (backpressure — COM does not retry, event dropped)",
                  extra={"status": 503, "mode": "spool"})
        raise HTTPException(status_code=503, detail="spool full; retry later")

    log.info("event spooled",
             extra={"event_type": event_type, "bytes": len(body),
                    "status": 202, "mode": "spool"})
    return Response(status_code=202, headers={"x-bridge-event-id": str(row_id)})
