"""Splunk adapter.

Sends events to the Splunk HTTP Event Collector (HEC). Simple, high-value, and
widely deployed. Requires a HEC token and endpoint.
"""

from __future__ import annotations

import logging
import os

import httpx

from com_event_core.normalize import CanonicalEvent
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.splunk")


class SplunkAdapter(TargetAdapter):
    name = "splunk"

    def __init__(self) -> None:
        # e.g. https://splunk.example.com:8088/services/collector/event
        self._url = os.environ["SPLUNK_HEC_URL"]
        self._token = os.environ["SPLUNK_HEC_TOKEN"]
        self._source = os.environ.get("SPLUNK_SOURCE", "hpe-com")
        self._sourcetype = os.environ.get("SPLUNK_SOURCETYPE", "com:event")
        self._index = os.environ.get("SPLUNK_INDEX")  # optional
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))
        # HEC certs are often self-signed in labs; allow opt-out of verification.
        self._verify = os.environ.get("SPLUNK_VERIFY_TLS", "true").lower() != "false"

    def _to_hec(self, e: CanonicalEvent) -> dict:
        payload = {
            "source": self._source,
            "sourcetype": self._sourcetype,
            "event": {
                "event_id": e.event_id,
                "operation": e.operation,
                "action": e.action,
                "title": e.title,
                "severity": e.severity,
                "serial": e.resource_serial,
                "model": e.resource_model,
                "mgmt_url": e.mgmt_url,
                "time_created": e.time_created,
                "tags": e.tags,
                "dedup_key": e.dedup_key,
                "correlation_key": e.correlation_key,
                "description": e.description,
                "resolution": e.resolution,
            },
        }
        if self._index:
            payload["index"] = self._index
        return payload

    def forward(self, event: CanonicalEvent) -> None:
        with httpx.Client(timeout=self._timeout, verify=self._verify) as client:
            r = client.post(
                self._url,
                json=self._to_hec(event),
                headers={"Authorization": f"Splunk {self._token}"},
            )
            r.raise_for_status()
        log.info("event %s forwarded to Splunk HEC", event.event_id)
