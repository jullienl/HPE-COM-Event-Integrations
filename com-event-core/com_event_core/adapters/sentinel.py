"""Microsoft Sentinel adapter.

Sends COM events to a Log Analytics workspace (which Microsoft Sentinel sits on
top of) via the classic **HTTP Data Collector API**. This path is self-contained
— it authenticates with the workspace id + shared key using an HMAC-SHA256
signature, so it needs no Azure AD app registration or token flow, which makes it
a good fit for the bridge (on-prem, no cloud SDK).

Events land in a custom table named ``<SENTINEL_LOG_TYPE>_CL``; build Sentinel
analytics rules on top of that table. Post-only (a SIEM ingest has no "close"),
so a clear is delivered as its own record with ``action="clear"``.

Env:
  SENTINEL_WORKSPACE_ID   Log Analytics workspace id (GUID).
  SENTINEL_SHARED_KEY     Workspace primary/secondary key (secret).
  SENTINEL_LOG_TYPE       Custom log/table name (default "HPECOMEvent"); Azure
                          appends "_CL".
  SENTINEL_API_VERSION    Data Collector API version (default "2016-04-01").
  TARGET_TIMEOUT          Per-request HTTP timeout in seconds (default 15).
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import logging
import os

import httpx

from com_event_core.normalize import CanonicalEvent
from com_event_core.secrets import get_secret
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.sentinel")


class SentinelAdapter(TargetAdapter):
    name = "sentinel"

    def __init__(self) -> None:
        self._workspace_id = os.environ["SENTINEL_WORKSPACE_ID"]
        self._shared_key = get_secret("SENTINEL_SHARED_KEY")
        self._log_type = os.environ.get("SENTINEL_LOG_TYPE", "HPECOMEvent")
        api_version = os.environ.get("SENTINEL_API_VERSION", "2016-04-01")
        self._url = (
            f"https://{self._workspace_id}.ods.opinsights.azure.com"
            f"/api/logs?api-version={api_version}"
        )
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))

    def _signature(self, date: str, content_length: int) -> str:
        """Build the SharedKey Authorization header per the Data Collector API."""
        method = "POST"
        content_type = "application/json"
        resource = "/api/logs"
        string_to_hash = (
            f"{method}\n{content_length}\n{content_type}\n"
            f"x-ms-date:{date}\n{resource}"
        )
        decoded_key = base64.b64decode(self._shared_key)
        digest = hmac.new(
            decoded_key, string_to_hash.encode("utf-8"), hashlib.sha256
        ).digest()
        encoded = base64.b64encode(digest).decode("utf-8")
        return f"SharedKey {self._workspace_id}:{encoded}"

    def _to_record(self, e: CanonicalEvent) -> dict:
        return {
            "event_id": e.event_id,
            "operation": e.operation,
            "action": e.action,
            "title": e.title,
            "severity": e.severity,
            "serial": e.resource_serial,
            "model": e.resource_model,
            "mgmt_url": e.mgmt_url,
            "time_created": e.time_created,
            "correlation_key": e.correlation_key,
            "dedup_key": e.dedup_key,
            "description": e.description,
            "resolution": e.resolution,
            "category": e.category,
        }

    def forward(self, event: CanonicalEvent) -> None:
        body = json.dumps([self._to_record(event)], default=str)
        content_bytes = body.encode("utf-8")
        # RFC 1123 date in GMT, as required by the Data Collector API.
        date = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": self._signature(date, len(content_bytes)),
            "Log-Type": self._log_type,
            "x-ms-date": date,
            "time-generated-field": "time_created",
        }
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(self._url, content=content_bytes, headers=headers)
            r.raise_for_status()
        log.info("event %s forwarded to Microsoft Sentinel", event.event_id)
