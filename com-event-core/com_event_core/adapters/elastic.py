"""Elasticsearch adapter.

Indexes each COM event as a document in Elasticsearch (the store behind the
Elastic SIEM / Observability stack) via `POST /<index>/_doc`. Works with Elastic
Cloud or a self-managed cluster; authenticate with an **API key** (preferred) or
HTTP basic. Post-only (indexing has no "close"), so a clear is indexed as its own
document with ``action="clear"`` — build Kibana/Elastic rules on the stream.

Env:
  ELASTIC_URL         Cluster base URL, e.g. https://host:9243 or https://host:9200.
  ELASTIC_INDEX       Target index (default "hpe-com-events").
  ELASTIC_API_KEY     Base64 API key (secret). If set, sent as
                      `Authorization: ApiKey <key>`.
  ELASTIC_USER        HTTP basic username (used only if ELASTIC_API_KEY is unset).
  ELASTIC_PASSWORD    HTTP basic password (secret; used with ELASTIC_USER).
  ELASTIC_VERIFY_TLS  "false" to skip TLS verification (self-signed labs). Default true.
  TARGET_TIMEOUT      Per-request HTTP timeout in seconds (default 15).
"""

from __future__ import annotations

import logging
import os

import httpx

from com_event_core.normalize import CanonicalEvent
from com_event_core.secrets import get_secret
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.elastic")


class ElasticAdapter(TargetAdapter):
    name = "elastic"

    def __init__(self) -> None:
        self._base = os.environ["ELASTIC_URL"].rstrip("/")
        self._index = os.environ.get("ELASTIC_INDEX", "hpe-com-events")
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))
        self._verify = os.environ.get("ELASTIC_VERIFY_TLS", "true").lower() != "false"

        self._headers = {"Content-Type": "application/json"}
        api_key = get_secret("ELASTIC_API_KEY", default="", required=False)
        if api_key:
            self._headers["Authorization"] = f"ApiKey {api_key}"
            self._auth: tuple[str, str] | None = None
        else:
            user = os.environ.get("ELASTIC_USER")
            if user:
                self._auth = (user, get_secret("ELASTIC_PASSWORD"))
            else:
                self._auth = None

    def _to_doc(self, e: CanonicalEvent) -> dict:
        return {
            "@timestamp": e.time_created,
            "event_id": e.event_id,
            "operation": e.operation,
            "action": e.action,
            "title": e.title,
            "severity": e.severity,
            "serial": e.resource_serial,
            "model": e.resource_model,
            "mgmt_url": e.mgmt_url,
            "correlation_key": e.correlation_key,
            "dedup_key": e.dedup_key,
            "description": e.description,
            "resolution": e.resolution,
            "category": e.category,
            "tags": e.tags,
        }

    def forward(self, event: CanonicalEvent) -> None:
        url = f"{self._base}/{self._index}/_doc"
        with httpx.Client(timeout=self._timeout, verify=self._verify) as client:
            r = client.post(
                url, json=self._to_doc(event), headers=self._headers, auth=self._auth
            )
            r.raise_for_status()
        log.info("event %s forwarded to Elasticsearch (%s)", event.event_id, self._index)
