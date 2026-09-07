"""PagerDuty adapter.

Triggers and resolves PagerDuty incidents via the **Events API v2**
(`/v2/enqueue`). PagerDuty has first-class raise/clear support built in, so this
is one of the cleanest lifecycle mappings in the project:

* raise: `event_action="trigger"` with `dedup_key = correlation_key`.
* clear: `event_action="resolve"` with the **same** `dedup_key`, which resolves
  the exact incident the raise opened.

No search/lookup is needed — PagerDuty keys the incident off `dedup_key` itself.

Env:
  PAGERDUTY_ROUTING_KEY   Integration key of an Events API v2 service (secret).
  PAGERDUTY_API_URL       Events endpoint. Default https://events.pagerduty.com/v2/enqueue
                          (use https://events.eu.pagerduty.com/v2/enqueue for the EU).
  PAGERDUTY_SOURCE        `source` label on the alert (default "HPE Compute Ops Management").
  TARGET_TIMEOUT          Per-request HTTP timeout in seconds (default 15).
"""

from __future__ import annotations

import logging
import os

import httpx

from com_event_core.normalize import ACTION_CLEAR, CanonicalEvent
from com_event_core.secrets import get_secret
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.pagerduty")

# PagerDuty severity enum is critical | error | warning | info.
_SEVERITY = {
    "critical": "critical",
    "major": "error",
    "minor": "warning",
    "warning": "warning",
    "normal": "info",
}


class PagerDutyAdapter(TargetAdapter):
    name = "pagerduty"

    def __init__(self) -> None:
        self._url = os.environ.get(
            "PAGERDUTY_API_URL", "https://events.pagerduty.com/v2/enqueue"
        )
        self._routing_key = get_secret("PAGERDUTY_ROUTING_KEY")
        self._source = os.environ.get(
            "PAGERDUTY_SOURCE", "HPE Compute Ops Management"
        )
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))

    def _to_event(self, e: CanonicalEvent) -> dict:
        dedup_key = e.correlation_key or e.dedup_key
        action = "resolve" if e.action == ACTION_CLEAR else "trigger"
        payload: dict = {
            "routing_key": self._routing_key,
            "event_action": action,
            "dedup_key": dedup_key,
        }
        if action == "trigger":
            custom = {
                "event_id": e.event_id,
                "operation": e.operation,
                "serial": e.resource_serial,
                "model": e.resource_model,
                "correlation_key": e.correlation_key,
            }
            if e.description:
                custom["description"] = e.description
            if e.resolution:
                custom["resolution"] = e.resolution
            payload["payload"] = {
                "summary": e.title,
                "source": e.resource_serial or self._source,
                "severity": _SEVERITY.get(e.severity, "warning"),
                "component": e.resource_model or "",
                "group": self._source,
                "timestamp": e.time_created or None,
                "custom_details": custom,
            }
            if e.mgmt_url:
                payload["links"] = [
                    {"href": e.mgmt_url, "text": "Open in HPE COM"}
                ]
        return payload

    def forward(self, event: CanonicalEvent) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(self._url, json=self._to_event(event))
            r.raise_for_status()
        log.info("event %s (%s) forwarded to PagerDuty", event.event_id, event.action)
