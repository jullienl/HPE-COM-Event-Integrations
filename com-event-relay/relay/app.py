"""
COM cloud relay — container-first, cloud-agnostic.

A thin, stateless public receiver that:
  1. Answers the COM verification handshake (echoes the challenge token).
  2. Authenticates events via a shared-secret header (only COM knows it).
  3. Enqueues the raw event onto a durable queue for an outbound-only shim
     to drain — on Azure (Service Bus) or AWS (SQS), chosen by QUEUE_BACKEND.

It performs NO payload transformation and does NOT contact any target system.

AI-generated reference implementation. Review and harden before production use.

Run locally:
    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8080
"""

import hmac
import json
import logging
import os
import time
import uuid

from fastapi import FastAPI, HTTPException, Request, Response

from core.queue import get_publisher


# --- Structured (JSON) logging -------------------------------------------
class JsonLogFormatter(logging.Formatter):
    """Emit one JSON object per log line so log pipelines can parse fields
    (App Insights / CloudWatch / any OTLP collector)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Attach any extra fields passed via logger.*(..., extra={...}).
        for key in ("relay_event_id", "event_type", "status", "bytes"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload)


_handler = logging.StreamHandler()
_handler.setFormatter(JsonLogFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
log = logging.getLogger("com-event-relay")

# --- Config (fail fast if anything mandatory is missing) -----------------
COM_SECRET = os.environ["COM_SHARED_SECRET"]
SECRET_HEADER = os.environ.get("SHARED_SECRET_HEADER", "x-shim-secret").lower()
# Cap request bodies to protect a public endpoint from oversized-payload abuse.
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(256 * 1024)))  # 256 KB

CHALLENGE_HEADER = "x-compute-ops-mgmt-verification-challenge"

# Select + validate the queue backend (servicebus | sqs) once, at startup.
publisher = get_publisher()

app = FastAPI(title="COM event relay")


@app.get("/healthz")
def liveness():
    """Liveness: the process is up. Does not touch the queue backend."""
    return {"status": "ok"}


@app.get("/readyz")
def readiness():
    """Readiness: only 'ready' if the queue backend is reachable, so a broken
    connection stops the platform from routing traffic to this instance."""
    if publisher.health():
        return {"status": "ready"}
    raise HTTPException(status_code=503, detail="queue backend unreachable")


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

    # Only POST carries events beyond this point.
    if request.method != "POST":
        raise HTTPException(status_code=400, detail="missing challenge header")

    # 2) Authenticate the event via the shared-secret header (constant-time).
    received = request.headers.get(SECRET_HEADER, "")
    if not received or not hmac.compare_digest(received, COM_SECRET):
        log.warning("rejected event: missing or invalid shared secret",
                    extra={"status": 401})
        raise HTTPException(status_code=401, detail="unauthorized")

    # 3) Enforce a body size limit — reject early via Content-Length when
    #    present, then hard-guard after reading in case the header lied.
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    # 4) Stamp a correlation id and enqueue the raw body with metadata.
    #    COM webhooks are fire-and-forget (one POST, no retries), so the relay's
    #    whole job is to capture the event into the durable queue immediately. A
    #    transient enqueue failure returns 503, but COM will NOT resend it AND a
    #    5xx counts against webhook health (10 consecutive failures -> webhook
    #    DISABLED). A highly available managed queue keeps enqueue failures rare.
    relay_event_id = str(uuid.uuid4())
    event_type = request.headers.get("x-compute-ops-mgmt-event-type", "unknown")
    properties = {
        "relay_event_id": relay_event_id,
        "event_type": event_type,
        "received_at": str(int(time.time())),
        "source": "com-event-relay",
    }
    try:
        publisher.publish(body, properties)
    except Exception:
        log.exception("enqueue failed; returning 503 (COM does not retry — event lost)",
                      extra={"relay_event_id": relay_event_id, "status": 503})
        raise HTTPException(status_code=503, detail="temporarily unable to enqueue")

    log.info("event enqueued",
             extra={"relay_event_id": relay_event_id,
                    "event_type": event_type, "bytes": len(body), "status": 202})
    return Response(status_code=202, headers={"x-relay-event-id": relay_event_id})
