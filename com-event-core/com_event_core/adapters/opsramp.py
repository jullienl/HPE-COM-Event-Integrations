"""OpsRamp adapter.

Pushes COM events into OpsRamp as **alerts** via the OpsRamp Alerts REST API
(OAuth2 client-credentials). This is the *decoupled* path — use it only when you
want the queue/spool + network-unexposed delivery this project provides (e.g. to
keep the internal network closed, or to fan the same COM stream out to OpsRamp
*and* other targets). If COM can reach OpsRamp directly, prefer OpsRamp's native
COM webhook integration and skip this project.

Env:
  OPSRAMP_API_URL       e.g. https://acme.api.opsramp.com
  OPSRAMP_TENANT_ID     tenant/client id the alerts are posted under
  OPSRAMP_KEY           OAuth2 key (client_id) of an Integration API credential
  OPSRAMP_SECRET        OAuth2 secret (client_secret)
  OPSRAMP_SERVICE_NAME  optional serviceName label (default "HPE COM")
  TARGET_TIMEOUT        per-request HTTP timeout in seconds (default 15)
"""

from __future__ import annotations

import logging
import os
import time

import httpx

from com_event_core.normalize import ACTION_CLEAR, CanonicalEvent
from com_event_core.secrets import get_secret
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.opsramp")

# OpsRamp alert state scale.
_STATE = {
    "critical": "Critical",
    "major": "Critical",
    "minor": "Warning",
    "warning": "Warning",
    "normal": "Ok",
}


class OpsRampAdapter(TargetAdapter):
    name = "opsramp"

    def __init__(self) -> None:
        self._api = os.environ["OPSRAMP_API_URL"].rstrip("/")  # https://acme.api.opsramp.com
        self._tenant = os.environ["OPSRAMP_TENANT_ID"]
        self._key = get_secret("OPSRAMP_KEY")
        self._secret = get_secret("OPSRAMP_SECRET")
        self._service = os.environ.get("OPSRAMP_SERVICE_NAME", "HPE COM")
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))
        self._alerts_url = f"{self._api}/api/v2/tenants/{self._tenant}/alerts"
        # Cached OAuth2 bearer token (client-credentials) + its expiry epoch.
        self._token: str | None = None
        self._token_exp: float = 0.0

    def _bearer(self, client: httpx.Client) -> str:
        # Reuse the cached token until ~30s before it expires.
        if self._token and time.time() < self._token_exp - 30:
            return self._token
        r = client.post(
            f"{self._api}/auth/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._key,
                "client_secret": self._secret,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        r.raise_for_status()
        tok = r.json()
        self._token = tok["access_token"]
        self._token_exp = time.time() + int(tok.get("expires_in", 3600))
        return self._token

    def _to_alert(self, e: CanonicalEvent) -> dict:
        # On a clear (recovery), send currentState 'Ok' with the SAME alertKey so
        # OpsRamp auto-heals the alert it previously raised.
        state = "Ok" if e.action == ACTION_CLEAR else _STATE.get(e.severity, "Warning")
        return {
            "serviceName": self._service,
            "device": {
                # Correlate against the managed resource by serial; fall back to
                # model so the alert still lands on a device object.
                "hostName": e.resource_serial or e.resource_model or "unknown",
                "resourceName": e.resource_serial or "",
            },
            "currentState": state,
            # Stable key so the raise and its later clear map to one alert.
            "alertKey": e.correlation_key or e.dedup_key,
            "component": e.resource_model or "",
            "subject": e.title,
            "description": e.description or (
                f"COM operation {e.operation} on {e.resource_serial or 'unknown'} "
                f"(event {e.event_id})"
            ),
            "app": "HPE COM",
            "alertTime": e.time_created or "",
        }

    def forward(self, event: CanonicalEvent) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            token = self._bearer(client)
            r = client.post(
                self._alerts_url,
                json=self._to_alert(event),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            r.raise_for_status()
        log.info("event %s (%s) forwarded to OpsRamp", event.event_id, event.action)
