"""OBM (OpenText Operations Bridge Manager) adapter.

Maps a CanonicalEvent to an OBM event and POSTs it to the OBM Event REST API.
Mirrors the field mapping proven in the original OBM shim.
"""

from __future__ import annotations

import logging
import os

import httpx

from com_event_core.normalize import CanonicalEvent
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.obm")

# OBM severity vocabulary.
_SEVERITY = {
    "normal": "normal",
    "warning": "warning",
    "minor": "minor",
    "major": "major",
    "critical": "critical",
}


class ObmAdapter(TargetAdapter):
    name = "obm"

    def __init__(self) -> None:
        self._url = os.environ["OBM_EVENT_API_URL"]
        self._auth = (os.environ["OBM_USER"], os.environ["OBM_PASSWORD"])
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))

    def _to_obm(self, e: CanonicalEvent) -> dict:
        return {
            "title": e.title,
            "severity": _SEVERITY.get(e.severity, "warning"),
            "related_ci": e.resource_serial,
            "node": e.resource_model,
            "mgmt_url": e.mgmt_url,
            "time_created": e.time_created,
            "custom_attrs": ";".join(f"{k}={v}" for k, v in e.tags.items()),
            "dedup_key": e.dedup_key,
        }

    def forward(self, event: CanonicalEvent) -> None:
        payload = self._to_obm(event)
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(self._url, json=payload, auth=self._auth)
            r.raise_for_status()
        log.info("event %s forwarded to OBM", event.event_id)
