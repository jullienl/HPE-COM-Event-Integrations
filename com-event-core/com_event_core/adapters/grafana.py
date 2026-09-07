"""Grafana Cloud adapter.

Ships each COM event as a log line to **Grafana Cloud Logs (Loki)** via the Loki
push API (`POST /loki/api/v1/push`). Grafana Cloud Logs is the store behind
Grafana dashboards, Explore and alerting, so once events land you can search them
in Explore and build Grafana alert rules on the stream.

Post-only (indexing a log line has no "close"), so a clear is shipped as its own
line with ``action="clear"`` — build Grafana rules on the stream rather than
expecting an in-place close.

Auth is HTTP basic: the username is the Grafana Cloud Logs **user / instance id**
and the password is an access-policy token (or API key) with ``logs:write``.

Labels are kept **low-cardinality on purpose** (source/severity/action/category)
— high-cardinality values (serial, correlation_key, event_id) go inside the JSON
log line, not the Loki stream labels, to avoid label explosion.

Env:
  GRAFANA_LOKI_URL   Loki base URL, e.g. https://logs-prod-012.grafana.net
  GRAFANA_LOKI_USER  Loki tenant / user id (Grafana Cloud "User" numeric id).
  GRAFANA_API_TOKEN  Access-policy token / API key with logs:write (secret).
  GRAFANA_LABELS     Optional extra static stream labels, comma-separated
                     key=value (e.g. "env=prod,team=infra").
  TARGET_TIMEOUT     Per-request HTTP timeout in seconds (default 15).
"""

from __future__ import annotations

import json
import logging
import os
import time

import httpx

from com_event_core.normalize import CanonicalEvent
from com_event_core.secrets import get_secret
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.grafana")


class GrafanaAdapter(TargetAdapter):
    name = "grafana"

    def __init__(self) -> None:
        base = os.environ["GRAFANA_LOKI_URL"].rstrip("/")
        self._url = f"{base}/loki/api/v1/push"
        self._user = os.environ["GRAFANA_LOKI_USER"]
        self._token = get_secret("GRAFANA_API_TOKEN")
        self._auth = (self._user, self._token)
        extra = os.environ.get("GRAFANA_LABELS", "")
        self._extra_labels: dict[str, str] = {}
        for pair in extra.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k.strip():
                    self._extra_labels[k.strip()] = v.strip()
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))

    def _labels(self, e: CanonicalEvent) -> dict[str, str]:
        # Keep cardinality low: only coarse, bounded values become stream labels.
        labels = {
            "source": "hpe-com",
            "severity": e.severity,
            "action": e.action,
        }
        if e.category:
            labels["category"] = e.category
        labels.update(self._extra_labels)
        return labels

    def _line(self, e: CanonicalEvent) -> str:
        return json.dumps(
            {
                "event_id": e.event_id,
                "operation": e.operation,
                "action": e.action,
                "title": e.title,
                "severity": e.severity,
                "serial": e.resource_serial,
                "model": e.resource_model,
                "mgmt_url": e.mgmt_url,
                "correlation_key": e.correlation_key or e.dedup_key,
                "dedup_key": e.dedup_key,
                "description": e.description,
                "resolution": e.resolution,
                "category": e.category,
                "tags": e.tags,
            },
            default=str,
        )

    def _to_payload(self, e: CanonicalEvent) -> dict:
        # Loki expects the timestamp as a string of nanoseconds since epoch.
        ts_ns = str(time.time_ns())
        return {
            "streams": [
                {
                    "stream": self._labels(e),
                    "values": [[ts_ns, self._line(e)]],
                }
            ]
        }

    def forward(self, event: CanonicalEvent) -> None:
        with httpx.Client(timeout=self._timeout, auth=self._auth) as client:
            r = client.post(
                self._url,
                json=self._to_payload(event),
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
        log.info("event %s (%s) forwarded to Grafana Cloud Logs",
                 event.event_id, event.action)
