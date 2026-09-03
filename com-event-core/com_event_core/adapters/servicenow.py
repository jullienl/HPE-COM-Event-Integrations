"""ServiceNow adapter.

Creates events/incidents via the ServiceNow Table API. By default it posts to
the Event table (em_event) for Event Management; set SNOW_TABLE=incident to open
incidents directly. Uses Basic auth (swap for OAuth in production if required).
"""

from __future__ import annotations

import logging
import os

import httpx

from com_event_core.normalize import CanonicalEvent
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.servicenow")

# ServiceNow Event Management severity is numeric (1 critical .. 5 clear/info).
_SEVERITY_NUM = {
    "critical": "1",
    "major": "2",
    "minor": "3",
    "warning": "4",
    "normal": "5",
}


class ServiceNowAdapter(TargetAdapter):
    name = "servicenow"

    def __init__(self) -> None:
        instance = os.environ["SNOW_INSTANCE"].rstrip("/")  # e.g. https://acme.service-now.com
        self._table = os.environ.get("SNOW_TABLE", "em_event")
        self._url = f"{instance}/api/now/table/{self._table}"
        self._auth = (os.environ["SNOW_USER"], os.environ["SNOW_PASSWORD"])
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))

    def _to_snow(self, e: CanonicalEvent) -> dict:
        if self._table == "em_event":
            return {
                "source": "HPE COM",
                "event_class": "compute-ops-management",
                "resource": e.resource_model or "",
                "node": e.resource_serial or "",
                "severity": _SEVERITY_NUM.get(e.severity, "4"),
                "description": e.title,
                "message_key": e.dedup_key,  # SNOW de-dup/correlation key
                "additional_info": ";".join(f"{k}={v}" for k, v in e.tags.items()),
            }
        # incident table
        return {
            "short_description": e.title,
            "description": f"COM operation {e.operation} on {e.resource_serial}",
            "cmdb_ci": e.resource_serial or "",
            "correlation_id": e.dedup_key,
        }

    def forward(self, event: CanonicalEvent) -> None:
        payload = self._to_snow(event)
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(
                self._url, json=payload, auth=self._auth,
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
        log.info("event %s forwarded to ServiceNow (%s)", event.event_id, self._table)
