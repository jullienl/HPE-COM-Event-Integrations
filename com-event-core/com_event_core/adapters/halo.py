"""HaloITSM adapter.

Creates tickets in HaloITSM via its REST API (OAuth2 client-credentials). A
strong fit for the decoupled path: the pipeline's built-in **de-duplication**
(the SQLite TTL store) means a repeated COM event won't open a second ticket for
the same fault — you get one ticket per real problem, not one per webhook retry.

Lifecycle (raise / clear)
-------------------------
Halo has no native event correlation, so this adapter does it explicitly:
* raise: create a ticket, tagging it with `thirdpartyref = correlation_key`.
* clear: look up the open ticket(s) with that `thirdpartyref` and set them to a
  closed status (HALO_CLOSED_STATUS_ID).

The lookup query and the closed-status id are instance-specific — tune
HALO_CLOSED_STATUS_ID (and the query below) to your Halo configuration.

Env:
  HALO_API_URL          e.g. https://acme.haloitsm.com
  HALO_CLIENT_ID        OAuth2 client id (an Integration API application)
  HALO_CLIENT_SECRET    OAuth2 client secret
  HALO_TENANT           optional tenant (hosted Halo multi-tenant token param)
  HALO_TICKET_TYPE_ID   optional ticket type id (default 1)
  HALO_CLOSED_STATUS_ID optional status id used to close on clear (default 9)
  TARGET_TIMEOUT        per-request HTTP timeout in seconds (default 15)
"""

from __future__ import annotations

import logging
import os
import time

import httpx

from com_event_core.normalize import ACTION_CLEAR, CanonicalEvent
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.halo")

# Halo impact/urgency scale is 1 (highest) .. 4 (lowest). Map from canonical.
_IMPACT = {
    "critical": 1,
    "major": 2,
    "minor": 3,
    "warning": 3,
    "normal": 4,
}


class HaloAdapter(TargetAdapter):
    name = "halo"

    def __init__(self) -> None:
        self._api = os.environ["HALO_API_URL"].rstrip("/")  # https://acme.haloitsm.com
        self._client_id = os.environ["HALO_CLIENT_ID"]
        self._client_secret = os.environ["HALO_CLIENT_SECRET"]
        self._tenant = os.environ.get("HALO_TENANT")
        self._ticket_type = int(os.environ.get("HALO_TICKET_TYPE_ID", "1"))
        self._closed_status = int(os.environ.get("HALO_CLOSED_STATUS_ID", "9"))
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))
        self._tickets_url = f"{self._api}/api/Tickets"
        # Cached OAuth2 bearer token (client-credentials) + its expiry epoch.
        self._token: str | None = None
        self._token_exp: float = 0.0

    def _bearer(self, client: httpx.Client) -> str:
        # Reuse the cached token until ~30s before it expires.
        if self._token and time.time() < self._token_exp - 30:
            return self._token
        # Hosted (multi-tenant) Halo takes the tenant as a query param.
        url = f"{self._api}/auth/token"
        if self._tenant:
            url = f"{url}?tenant={self._tenant}"
        r = client.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "all",
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

    def _to_ticket(self, e: CanonicalEvent) -> dict:
        details = (
            f"COM operation {e.operation} on {e.resource_serial or 'unknown'} "
            f"({e.resource_model or 'unknown model'}).\n"
            f"Event id: {e.event_id}\n"
            f"Severity: {e.severity}\n"
            f"Management URL: {e.mgmt_url or 'n/a'}\n"
            f"Time: {e.time_created or 'n/a'}"
        )
        if e.description:
            details += f"\n\n{e.description}"
        if e.resolution:
            details += f"\n\nSuggested resolution: {e.resolution}"
        impact = _IMPACT.get(e.severity, 3)
        return {
            "summary": e.title,
            "details": details,
            "tickettype_id": self._ticket_type,
            "impact": impact,
            "urgency": impact,
            # Correlation key so a later clear can find and close this ticket.
            "thirdpartyref": e.correlation_key or e.dedup_key,
        }

    def forward(self, event: CanonicalEvent) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            token = self._bearer(client)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if event.action == ACTION_CLEAR:
                self._close(client, headers, event)
            else:
                self._create(client, headers, event)
        log.info("event %s (%s) forwarded to HaloITSM", event.event_id, event.action)

    def _create(self, client: httpx.Client, headers: dict, event: CanonicalEvent) -> None:
        # Halo's Tickets endpoint accepts an array of ticket objects.
        r = client.post(self._tickets_url, json=[self._to_ticket(event)], headers=headers)
        r.raise_for_status()

    def _close(self, client: httpx.Client, headers: dict, event: CanonicalEvent) -> None:
        """Find open ticket(s) for this problem and set them to a closed status."""
        ref = event.correlation_key or event.dedup_key
        q = client.get(
            self._tickets_url,
            params={"thirdpartyref": ref, "open_only": "true", "count": "25"},
            headers=headers,
        )
        q.raise_for_status()
        body = q.json()
        tickets = body.get("tickets", body) if isinstance(body, dict) else body
        ids = [t["id"] for t in tickets if isinstance(t, dict) and t.get("thirdpartyref") == ref]
        if not ids:
            log.info("no open Halo ticket for %s; nothing to close", ref)
            return
        updates = [
            {
                "id": tid,
                "status_id": self._closed_status,
                "note": f"COM reported recovery: {event.title}",
            }
            for tid in ids
        ]
        r = client.post(self._tickets_url, json=updates, headers=headers)
        r.raise_for_status()
