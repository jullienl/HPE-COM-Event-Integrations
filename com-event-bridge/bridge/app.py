"""
COM event bridge — single-box, on-prem, no queue, no cloud.

Folds the whole COM -> target path into ONE process:

  1. Answers the COM verification handshake (echoes the challenge token).
  2. Authenticates events via a shared-secret header (only COM knows it).
  3. Normalises the COM payload into a CanonicalEvent.
  4. Delivers it to the selected TARGET adapter, either:
       - DELIVERY_MODE=sync  : forward inline; on failure return 503 so COM
                               retries (simplest; relies on COM's retry window).
       - DELIVERY_MODE=spool : persist to a local on-disk spool, ack COM with
                               202 immediately, and let a background worker drain
                               + retry (survives target outages, no cloud queue).

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
DELIVERY_MODE = os.environ.get("DELIVERY_MODE", "sync").strip().lower()  # sync | spool

CHALLENGE_HEADER = "x-compute-ops-mgmt-verification-challenge"

if DELIVERY_MODE not in ("sync", "spool"):
    raise ValueError(f"DELIVERY_MODE must be 'sync' or 'spool', got '{DELIVERY_MODE}'.")

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
    """Inline delivery: forward now; a target failure becomes 503 so COM retries."""
    event = normalize(payload)

    if dedup.is_duplicate(event.dedup_key):
        log.info("duplicate event; skipping",
                 extra={"event_id": event.event_id, "status": 200, "mode": "sync"})
        return Response(status_code=200)

    try:
        adapter.forward(event)
    except Exception:
        log.exception("forward failed; asking COM to retry",
                      extra={"event_id": event.event_id, "status": 503, "mode": "sync"})
        raise HTTPException(status_code=503, detail="target temporarily unavailable")

    log.info("event forwarded",
             extra={"event_id": event.event_id, "event_type": event_type,
                    "status": 202, "mode": "sync"})
    return Response(status_code=202, headers={"x-bridge-event-id": event.event_id})


def _accept_to_spool(body: bytes, event_type: str) -> Response:
    """Durable delivery: persist and ack immediately; the worker drains + retries.

    If the spool is over budget (prolonged target outage), return 503 so COM
    holds and retries rather than us dropping the event or filling the disk.
    """
    assert spool is not None  # created in spool mode at startup
    try:
        row_id = spool.put(body, {"event_type": event_type, "source": "com-event-bridge"})
    except SpoolFull:
        log.error("spool full; asking COM to retry", extra={"status": 503, "mode": "spool"})
        raise HTTPException(status_code=503, detail="spool full; retry later")

    log.info("event spooled",
             extra={"event_type": event_type, "bytes": len(body),
                    "status": 202, "mode": "spool"})
    return Response(status_code=202, headers={"x-bridge-event-id": str(row_id)})
