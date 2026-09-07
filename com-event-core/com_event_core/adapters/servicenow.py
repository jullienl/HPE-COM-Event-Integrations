"""ServiceNow adapter.

Creates events/incidents via the ServiceNow Table API. By default it posts to
the Event table (em_event) for Event Management; set SNOW_TABLE=incident to open
incidents directly. Uses Basic auth (swap for OAuth in production if required).

Lifecycle (raise / clear)
-------------------------
* em_event (default, recommended): a clear is sent as a Clear-severity (5) event
  with the SAME message_key, so ServiceNow Event Management auto-closes the alert
  it previously raised — no lookup needed.
* incident: a clear looks up the open incident by correlation_id and resolves it.
"""

from __future__ import annotations

import logging
import os

import httpx

from com_event_core.normalize import ACTION_CLEAR, CanonicalEvent
from com_event_core.secrets import get_secret
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
# ServiceNow incident state values (defaults): 6 = Resolved, 7 = Closed.
_INCIDENT_RESOLVED_STATE = "6"


class ServiceNowAdapter(TargetAdapter):
    name = "servicenow"

    def __init__(self) -> None:
        self._instance = os.environ["SNOW_INSTANCE"].rstrip("/")  # https://acme.service-now.com
        self._table = os.environ.get("SNOW_TABLE", "em_event")
        self._url = f"{self._instance}/api/now/table/{self._table}"
        self._auth = (os.environ["SNOW_USER"], get_secret("SNOW_PASSWORD"))
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))
        self._resolved_state = os.environ.get("SNOW_RESOLVED_STATE", _INCIDENT_RESOLVED_STATE)

    def _to_event(self, e: CanonicalEvent) -> dict:
        # em_event: a clear is a Clear-severity (5) event with the same
        # message_key, which auto-closes the correlated alert.
        severity = "5" if e.action == ACTION_CLEAR else _SEVERITY_NUM.get(e.severity, "4")
        return {
            "source": "HPE COM",
            "event_class": "compute-ops-management",
            "resource": e.resource_model or "",
            "node": e.resource_serial or "",
            "severity": severity,
            "description": e.description or e.title,
            # Stable per-problem key so the raise and its later clear correlate.
            "message_key": e.correlation_key or e.dedup_key,
            "additional_info": ";".join(f"{k}={v}" for k, v in e.tags.items()),
        }

    def _to_incident(self, e: CanonicalEvent) -> dict:
        return {
            "short_description": e.title,
            "description": e.description or f"COM operation {e.operation} on {e.resource_serial}",
            "cmdb_ci": e.resource_serial or "",
            "correlation_id": e.correlation_key or e.dedup_key,
        }

    def forward(self, event: CanonicalEvent) -> None:
        if self._table == "em_event":
            self._post(self._to_event(event))
        elif event.action == ACTION_CLEAR:
            self._resolve_incident(event)
        else:
            self._post(self._to_incident(event))
        log.info("event %s (%s) forwarded to ServiceNow (%s)",
                 event.event_id, event.action, self._table)

    def _post(self, payload: dict) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(
                self._url, json=payload, auth=self._auth,
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()

    def _resolve_incident(self, e: CanonicalEvent) -> None:
        """Find the open incident for this problem and mark it resolved."""
        correlation_id = e.correlation_key or e.dedup_key
        with httpx.Client(timeout=self._timeout) as client:
            q = client.get(
                self._url,
                params={
                    "sysparm_query": f"correlation_id={correlation_id}^active=true",
                    "sysparm_fields": "sys_id",
                    "sysparm_limit": "10",
                },
                auth=self._auth,
                headers={"Accept": "application/json"},
            )
            q.raise_for_status()
            rows = q.json().get("result", [])
            if not rows:
                log.info("no open incident for %s; nothing to resolve", correlation_id)
                return
            for row in rows:
                r = client.patch(
                    f"{self._url}/{row['sys_id']}",
                    json={
                        "state": self._resolved_state,
                        "close_code": "Resolved by caller",
                        "close_notes": f"COM reported recovery: {e.title}",
                    },
                    auth=self._auth,
                    headers={"Accept": "application/json"},
                )
                r.raise_for_status()
