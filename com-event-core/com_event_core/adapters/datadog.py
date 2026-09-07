"""Datadog adapter.

Posts COM events to the Datadog **Events API v1** (`/api/v1/events`). Datadog
events support an `aggregation_key` (so a problem and its recovery group into one
timeline) and an `alert_type`, giving a clean raise/clear mapping without a
lookup:

* raise: `alert_type` from the severity (error/warning), `aggregation_key =
  correlation_key`.
* clear: `alert_type="success"` with the **same** `aggregation_key`, so the
  recovery is threaded under the original problem.

Datadog events are post-only (there is no "close"), so a clear is delivered as
its own recovery event rather than mutating the raise.

Env:
  DATADOG_API_KEY   Datadog API key (secret).
  DATADOG_SITE      Datadog site, e.g. datadoghq.com (default), datadoghq.eu,
                    us3.datadoghq.com. The events URL is derived as
                    https://api.<site>/api/v1/events.
  DATADOG_TAGS      Optional comma-separated tags added to every event
                    (e.g. "team:infra,source:hpe-com").
  TARGET_TIMEOUT    Per-request HTTP timeout in seconds (default 15).
"""

from __future__ import annotations

import logging
import os

import httpx

from com_event_core.normalize import ACTION_CLEAR, CanonicalEvent
from com_event_core.secrets import get_secret
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.datadog")

# Datadog alert_type is error | warning | info | success.
_ALERT_TYPE = {
    "critical": "error",
    "major": "error",
    "minor": "warning",
    "warning": "warning",
    "normal": "info",
}


class DatadogAdapter(TargetAdapter):
    name = "datadog"

    def __init__(self) -> None:
        self._api_key = get_secret("DATADOG_API_KEY")
        site = os.environ.get("DATADOG_SITE", "datadoghq.com").strip().strip("/")
        self._url = f"https://api.{site}/api/v1/events"
        extra = os.environ.get("DATADOG_TAGS", "")
        self._tags = [t.strip() for t in extra.split(",") if t.strip()]
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))

    def _to_event(self, e: CanonicalEvent) -> dict:
        alert_type = "success" if e.action == ACTION_CLEAR else _ALERT_TYPE.get(
            e.severity, "warning"
        )
        text_lines = [
            f"COM operation {e.operation} on {e.resource_serial or 'unknown'} "
            f"({e.resource_model or 'unknown model'}).",
            f"Event id: {e.event_id}",
            f"Severity: {e.severity}",
            f"Action: {e.action}",
        ]
        if e.description:
            text_lines += ["", e.description]
        if e.resolution:
            text_lines += ["", f"Suggested resolution: {e.resolution}"]
        if e.mgmt_url:
            text_lines += ["", f"Management URL: {e.mgmt_url}"]

        tags = list(self._tags)
        tags.append(f"severity:{e.severity}")
        if e.resource_serial:
            tags.append(f"serial:{e.resource_serial}")
        if e.resource_model:
            tags.append(f"model:{e.resource_model}")

        return {
            "title": e.title,
            "text": "\n".join(text_lines),
            "alert_type": alert_type,
            "aggregation_key": e.correlation_key or e.dedup_key,
            "source_type_name": "hpe-com",
            "tags": tags,
        }

    def forward(self, event: CanonicalEvent) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(
                self._url,
                json=self._to_event(event),
                headers={
                    "DD-API-KEY": self._api_key,
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
        log.info("event %s (%s) forwarded to Datadog", event.event_id, event.action)
