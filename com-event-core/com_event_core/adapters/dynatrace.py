"""Dynatrace adapter.

Posts COM events to the Dynatrace **Events API v2** (`/api/v2/events/ingest`).
Dynatrace events are keyed to entities and auto-age out (there is no explicit
"close"), so this is a post-only mapping:

* raise: an ``ERROR_EVENT`` carrying the COM detail as event properties.
* clear: a ``CUSTOM_INFO`` recovery event with the same ``com.correlation_key``
  property, so a Dynatrace query can thread problem and recovery together.

Auth is an API token (``Authorization: Api-Token <token>``) with the
``events.ingest`` scope.

Env:
  DYNATRACE_URL             Environment API base, e.g.
                            https://abc12345.live.dynatrace.com (SaaS) or a
                            Managed/ActiveGate URL ending in /e/<env-id>.
  DYNATRACE_API_TOKEN       API token with events.ingest scope (secret).
  DYNATRACE_ENTITY_SELECTOR Optional entity selector to attach events to,
                            e.g. type("HOST"),entityName("srv01").
  DYNATRACE_PROPERTIES      Optional comma-separated key=value pairs added as
                            event properties (e.g. "team=infra,source=hpe-com").
  TARGET_TIMEOUT            Per-request HTTP timeout in seconds (default 15).
"""

from __future__ import annotations

import logging
import os

import httpx

from com_event_core.normalize import ACTION_CLEAR, CanonicalEvent
from com_event_core.secrets import get_secret
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.dynatrace")


class DynatraceAdapter(TargetAdapter):
    name = "dynatrace"

    def __init__(self) -> None:
        base = os.environ["DYNATRACE_URL"].rstrip("/")
        self._url = f"{base}/api/v2/events/ingest"
        self._token = get_secret("DYNATRACE_API_TOKEN")
        self._entity_selector = os.environ.get("DYNATRACE_ENTITY_SELECTOR", "")
        extra = os.environ.get("DYNATRACE_PROPERTIES", "")
        self._extra_props: dict[str, str] = {}
        for pair in extra.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k.strip():
                    self._extra_props[k.strip()] = v.strip()
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))

    def _to_event(self, e: CanonicalEvent) -> dict:
        event_type = "CUSTOM_INFO" if e.action == ACTION_CLEAR else "ERROR_EVENT"
        props: dict[str, str] = dict(self._extra_props)
        props.update(
            {
                "com.event_id": e.event_id,
                "com.operation": e.operation,
                "com.action": e.action,
                "com.severity": e.severity,
                "com.correlation_key": e.correlation_key or e.dedup_key,
            }
        )
        if e.resource_serial:
            props["com.serial"] = e.resource_serial
        if e.resource_model:
            props["com.model"] = e.resource_model
        if e.mgmt_url:
            props["com.mgmt_url"] = e.mgmt_url
        if e.description:
            props["com.description"] = e.description
        if e.resolution:
            props["com.resolution"] = e.resolution

        payload: dict = {
            "eventType": event_type,
            "title": e.title,
            "properties": props,
        }
        if self._entity_selector:
            payload["entitySelector"] = self._entity_selector
        return payload

    def forward(self, event: CanonicalEvent) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(
                self._url,
                json=self._to_event(event),
                headers={
                    "Authorization": f"Api-Token {self._token}",
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
        log.info("event %s (%s) forwarded to Dynatrace", event.event_id, event.action)
