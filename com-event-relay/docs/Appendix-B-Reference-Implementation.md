# Appendix B — Reference Implementation

> **Archived design document.** The original OBM-only reference shim code (Python
> FastAPI / Node.js Express). Superseded by the production implementations in this
> repository ([relay](../README.md) + shim) and the single-box
> [com-event-bridge](../../com-event-bridge/README.md), which generalize the same
> handshake + field mapping to multiple targets. Kept for the OBM field-mapping
> reference.

Reference shim implementations for the COM → OBM handshake and event forwarding.
Two equivalent versions are provided: **Python (FastAPI)** and **Node.js (Express)**.

> These are AI-generated reference implementations intended as a starting point.
> The OBM event endpoint path and field names (`title`, `related_ci`, `node`, etc.)
> are placeholders — align them to the customer's actual OBM Event REST API /
> Operations Connector (OpsCx) schema before production use. Review and harden
> before deploying.

Required environment variables (see `.env.example`):

| Variable | Purpose |
|---|---|
| `COM_SHARED_SECRET` | Shared secret matching the `x-shim-secret` header set on the COM webhook |
| `OBM_EVENT_API_URL` | OBM Event REST API endpoint |
| `OBM_USER` / `OBM_PASSWORD` | OBM Basic auth credentials |

---

## B.1 — Python (FastAPI)

Requirements: `pip install fastapi uvicorn httpx`
Run: `uvicorn server:app --host 127.0.0.1 --port 8080`

```python
import hmac, hashlib, logging, os, uuid
from fastapi import FastAPI, Request, Response, Header, HTTPException, BackgroundTasks
import httpx

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("com-shim")

# Step 10: load secrets at startup, fail fast
COM_SECRET = os.environ["COM_SHARED_SECRET"]
OBM_URL = os.environ["OBM_EVENT_API_URL"]
OBM_AUTH = (os.environ["OBM_USER"], os.environ["OBM_PASSWORD"])

app = FastAPI()

@app.get("/healthz")
def health():
    return {"status": "ok"}

# Step 8: handshake — echo the challenge token
@app.get("/com/webhook")
async def handshake(
    x_compute_ops_mgmt_verification_challenge: str | None = Header(default=None),
):
    if not x_compute_ops_mgmt_verification_challenge:
        raise HTTPException(status_code=400, detail="missing challenge header")
    log.info("handshake received, responding with verification token")
    # exact body + content-type required by COM
    return {"verification": x_compute_ops_mgmt_verification_challenge}

# Step 9: event receiver
@app.post("/com/webhook")
async def receive_event(
    request: Request,
    background: BackgroundTasks,
    x_shim_secret: str | None = Header(default=None),
):
    # (a) validate shared secret, constant-time
    if not x_shim_secret or not hmac.compare_digest(x_shim_secret, COM_SECRET):
        log.warning("rejected event: bad/missing secret header")
        raise HTTPException(status_code=401, detail="unauthorized")

    # (b) parse payload
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    corr_id = payload.get("id") or str(uuid.uuid4())
    hw = payload.get("hardware", {}) or {}

    # (c) transform to an OBM event
    tags = payload.get("tags", {}) or {}
    obm_event = {
        "title": payload.get("name"),
        "severity": map_severity(hw.get("health", {}).get("summary")),
        "related_ci": hw.get("serialNumber"),
        "node": hw.get("model"),
        "mgmt_url": f"https://{hw.get('bmc', {}).get('ip')}" if hw.get("bmc") else None,
        "time_created": payload.get("updatedAt"),
        "custom_attrs": ";".join(f"{k}={v}" for k, v in tags.items()),
        "dedup_key": hashlib.sha1(
            f"{payload.get('id')}|{payload.get('operation')}".encode()
        ).hexdigest(),  # (d) optional dedup key
    }

    log.info("event %s type=%s op=%s -> forwarding to OBM",
             corr_id, payload.get("type"), payload.get("operation"))

    # (f) ack COM immediately, forward async
    background.add_task(forward_to_obm, obm_event, corr_id)
    return Response(status_code=200)

def map_severity(summary: str | None) -> str:
    return {
        "OK": "normal", "WARNING": "minor",
        "CRITICAL": "critical", "UNKNOWN": "warning",
    }.get(summary, "warning")

# (e) forward to OBM Event REST API
async def forward_to_obm(event: dict, corr_id: str):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(OBM_URL, json=event, auth=OBM_AUTH)
            r.raise_for_status()
        log.info("event %s forwarded to OBM ok", corr_id)
    except Exception as e:
        log.error("event %s OBM forward FAILED: %s", corr_id, e)
        # TODO: enqueue for retry / dead-letter
```

---

## B.2 — Node.js (Express)

Requirements: Node 18+ (uses global `fetch`), `npm install express`
Run: `node server.js`

```javascript
const express = require("express");
const crypto = require("crypto");
const app = express();

app.use(express.json()); // parse JSON bodies for POST events

// Step 10: load secrets/config from env (fail fast if missing)
const COM_SECRET = process.env.COM_SHARED_SECRET; // shared secret header value
const OBM_URL = process.env.OBM_EVENT_API_URL; // OBM Event REST API endpoint
const OBM_USER = process.env.OBM_USER;
const OBM_PASSWORD = process.env.OBM_PASSWORD;

for (const [k, v] of Object.entries({ COM_SECRET, OBM_URL, OBM_USER, OBM_PASSWORD })) {
  if (!v) {
    console.error(`missing required env var: ${k}`);
    process.exit(1);
  }
}

// Health check for monitoring / load balancer
app.get("/healthz", (req, res) => res.status(200).json({ status: "ok" }));

// Handshake — GET
app.get("/com/webhook", (req, res) => {
  const token = req.get("x-compute-ops-mgmt-verification-challenge");
  if (!token) return res.status(400).json({ error: "missing challenge header" });
  console.log("handshake received, echoing verification token");
  return res.status(200).json({ verification: token });
});

// Events — POST (secret-validated)
app.post("/com/webhook", (req, res) => {
  const secret = req.get("x-shim-secret") || "";
  const ok =
    secret.length === COM_SECRET.length &&
    crypto.timingSafeEqual(Buffer.from(secret), Buffer.from(COM_SECRET));
  if (!ok) {
    console.warn("rejected event: bad/missing secret header");
    return res.status(401).json({ error: "unauthorized" });
  }

  // ack COM fast; transform + forward to OBM asynchronously
  const corrId = req.body?.id || crypto.randomUUID();
  console.log(
    `event ${corrId} type=${req.body?.type} op=${req.body?.operation} -> forwarding to OBM`
  );
  res.status(200).end();
  setImmediate(() => forwardToObm(req.body, corrId));
});

// Map COM health summary -> OBM severity
function mapSeverity(summary) {
  return (
    {
      OK: "normal",
      WARNING: "minor",
      CRITICAL: "critical",
      UNKNOWN: "warning",
    }[summary] || "warning"
  );
}

// Transform COM payload -> OBM event, then POST to the OBM Event REST API
async function forwardToObm(payload, corrId) {
  const hw = payload?.hardware || {};
  const tags = payload?.tags || {};

  const obmEvent = {
    title: payload?.name,
    severity: mapSeverity(hw?.health?.summary),
    related_ci: hw?.serialNumber,
    node: hw?.model,
    mgmt_url: hw?.bmc?.ip ? `https://${hw.bmc.ip}` : undefined,
    time_created: payload?.updatedAt,
    // flatten tags {k:v} -> "k=v;k2=v2" for OBM custom attributes
    custom_attrs: Object.entries(tags)
      .map(([k, v]) => `${k}=${v}`)
      .join(";"),
    // optional dedup key (OBM also de-dups)
    dedup_key: crypto
      .createHash("sha1")
      .update(`${payload?.id}|${payload?.operation}`)
      .digest("hex"),
  };

  try {
    const auth =
      "Basic " + Buffer.from(`${OBM_USER}:${OBM_PASSWORD}`).toString("base64");

    // Node 18+ has global fetch; add a timeout via AbortController
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);

    const r = await fetch(OBM_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: auth,
      },
      body: JSON.stringify(obmEvent),
      signal: controller.signal,
    });
    clearTimeout(timeout);

    if (!r.ok) {
      throw new Error(`OBM responded ${r.status} ${r.statusText}`);
    }
    console.log(`event ${corrId} forwarded to OBM ok`);
  } catch (err) {
    console.error(`event ${corrId} OBM forward FAILED: ${err.message}`);
    // TODO: enqueue for retry / dead-letter
  }
}

app.listen(8080, "127.0.0.1", () =>
  console.log("COM shim listening on 127.0.0.1:8080")
);
```
