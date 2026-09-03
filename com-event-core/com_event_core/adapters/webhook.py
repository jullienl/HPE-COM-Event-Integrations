"""Generic webhook adapter.

Catch-all target: POSTs the canonical event as JSON to any URL. Covers the long
tail of systems (including HaloITSM/HaloPSA via an inbound webhook, custom
middleware, iPaaS, etc.) without a bespoke adapter. Optionally attaches a static
auth header.
"""

from __future__ import annotations

import dataclasses
import logging
import os

import httpx

from com_event_core.normalize import CanonicalEvent
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.webhook")


class WebhookAdapter(TargetAdapter):
    name = "webhook"

    def __init__(self) -> None:
        self._url = os.environ["WEBHOOK_URL"]
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))
        # Optional static auth header, e.g. "Authorization: Bearer xyz" or a
        # shared secret expected by the receiver.
        self._header_name = os.environ.get("WEBHOOK_AUTH_HEADER")
        self._header_value = os.environ.get("WEBHOOK_AUTH_VALUE")

    def forward(self, event: CanonicalEvent) -> None:
        headers = {"Content-Type": "application/json"}
        if self._header_name and self._header_value:
            headers[self._header_name] = self._header_value

        # Send the full canonical event so downstream systems get everything.
        body = dataclasses.asdict(event)
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(self._url, json=body, headers=headers)
            r.raise_for_status()
        log.info("event %s forwarded to webhook", event.event_id)
