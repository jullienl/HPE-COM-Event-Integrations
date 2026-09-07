"""BMC Helix ITSM adapter.

Creates a BMC Helix ITSM (Remedy) incident on a COM **raise** and resolves it on
the matching **clear**, via the AR System / Innovation Suite REST API. BMC Helix
has no native COM path, so this adapter does the correlation explicitly.

Auth
----
BMC Helix uses a short-lived **JWT**: POST the username + password to
``/api/jwt/login`` (form-encoded), which returns the raw token; every subsequent
call sends ``Authorization: AR-JWT <token>``. The token is fetched per delivery
and released with ``/api/jwt/logout`` afterwards.

Lifecycle (raise / clear)
-------------------------
BMC has no built-in event correlation, so this adapter embeds the COM
correlation key as a marker (``[COM:<key>]``) in the incident's detailed
description:

* raise: create an incident on ``HPD:IncidentInterface_Create`` carrying the
  marker; impact/urgency are mapped from the COM severity.
* clear: query ``HPD:IncidentInterface`` for open incidents whose detailed
  description contains the marker and drive each to the resolved status.

Because the resolve is a **status write**, set ``BMC_HELIX_STATUS_RESOLVED`` to a
status value that exists in your workflow (commonly "Resolved").

Env:
  BMC_HELIX_URL             REST base URL, e.g. https://acme-restapi.onbmc.com
  BMC_HELIX_USER            AR System user (JWT login username).
  BMC_HELIX_PASSWORD        Password for that user (secret).
  BMC_HELIX_FIRST_NAME      Requester first name on new incidents (default "HPE").
  BMC_HELIX_LAST_NAME       Requester last name on new incidents (default "COM").
  BMC_HELIX_SERVICE_TYPE    "Service_Type" value (default "Infrastructure Event").
  BMC_HELIX_REPORTED_SOURCE "Reported Source" value (default "Systems Management").
  BMC_HELIX_ASSIGNED_GROUP  Optional "Assigned Group" for routing.
  BMC_HELIX_STATUS_RESOLVED Status used to close on a clear (default "Resolved").
  TARGET_TIMEOUT            Per-request HTTP timeout in seconds (default 15).
"""

from __future__ import annotations

import logging
import os

import httpx

from com_event_core.normalize import ACTION_CLEAR, CanonicalEvent
from com_event_core.secrets import get_secret
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.bmc_helix")

# COM severity -> BMC Helix (Impact, Urgency) selection values.
_IMPACT = {
    "critical": "1-Extensive/Widespread",
    "major": "2-Significant/Large",
    "minor": "3-Moderate/Limited",
    "warning": "4-Minor/Localized",
    "normal": "4-Minor/Localized",
}
_URGENCY = {
    "critical": "1-Critical",
    "major": "2-High",
    "minor": "3-Medium",
    "warning": "3-Medium",
    "normal": "4-Low",
}


class BmcHelixAdapter(TargetAdapter):
    name = "bmc_helix"

    def __init__(self) -> None:
        self._base = os.environ["BMC_HELIX_URL"].rstrip("/")
        self._user = os.environ["BMC_HELIX_USER"]
        self._password = get_secret("BMC_HELIX_PASSWORD")
        self._first_name = os.environ.get("BMC_HELIX_FIRST_NAME", "HPE")
        self._last_name = os.environ.get("BMC_HELIX_LAST_NAME", "COM")
        self._service_type = os.environ.get(
            "BMC_HELIX_SERVICE_TYPE", "Infrastructure Event"
        )
        self._reported_source = os.environ.get(
            "BMC_HELIX_REPORTED_SOURCE", "Systems Management"
        )
        self._assigned_group = os.environ.get("BMC_HELIX_ASSIGNED_GROUP", "")
        self._resolved_status = os.environ.get("BMC_HELIX_STATUS_RESOLVED", "Resolved")
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))

    def _marker(self, e: CanonicalEvent) -> str:
        return f"[COM:{e.correlation_key or e.dedup_key}]"

    def _detail(self, e: CanonicalEvent) -> str:
        lines = [
            f"COM operation {e.operation} on {e.resource_serial or 'unknown'} "
            f"({e.resource_model or 'unknown model'}).",
            f"Event id: {e.event_id}",
            f"Severity: {e.severity}",
            f"Time: {e.time_created or 'n/a'}",
        ]
        if e.mgmt_url:
            lines.append(f"Management URL: {e.mgmt_url}")
        if e.description:
            lines.append(e.description)
        if e.resolution:
            lines.append(f"Suggested resolution: {e.resolution}")
        # Correlation marker on its own line so a clear can find the incident.
        lines.append(self._marker(e))
        return "\n".join(lines)

    def _login(self, client: httpx.Client) -> str:
        r = client.post(
            f"{self._base}/api/jwt/login",
            data={"username": self._user, "password": self._password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        return r.text.strip()

    def _logout(self, client: httpx.Client, token: str) -> None:
        try:
            client.post(
                f"{self._base}/api/jwt/logout",
                headers={"Authorization": f"AR-JWT {token}"},
            )
        except httpx.HTTPError:  # best-effort; token expires on its own anyway
            log.debug("BMC Helix logout failed (ignored)")

    def forward(self, event: CanonicalEvent) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            token = self._login(client)
            headers = {
                "Authorization": f"AR-JWT {token}",
                "Content-Type": "application/json",
            }
            try:
                if event.action == ACTION_CLEAR:
                    self._resolve(client, headers, event)
                else:
                    self._create(client, headers, event)
            finally:
                self._logout(client, token)
        log.info(
            "event %s (%s) forwarded to BMC Helix ITSM", event.event_id, event.action
        )

    def _create(
        self, client: httpx.Client, headers: dict, e: CanonicalEvent
    ) -> None:
        values = {
            "First_Name": self._first_name,
            "Last_Name": self._last_name,
            "Description": e.title[:100],
            "Detailed_Decription": self._detail(e),  # BMC's field spelling
            "Impact": _IMPACT.get(e.severity, "3-Moderate/Limited"),
            "Urgency": _URGENCY.get(e.severity, "3-Medium"),
            "Status": "New",
            "Reported Source": self._reported_source,
            "Service_Type": self._service_type,
            "z1D_Action": "CREATE",
        }
        if self._assigned_group:
            values["Assigned Group"] = self._assigned_group
        r = client.post(
            f"{self._base}/api/arsys/v1/entry/HPD:IncidentInterface_Create",
            json={"values": values},
            headers=headers,
        )
        r.raise_for_status()

    def _resolve(
        self, client: httpx.Client, headers: dict, e: CanonicalEvent
    ) -> None:
        marker = self._marker(e)
        qualification = (
            f"'Detailed_Decription' LIKE \"%{marker}%\" AND 'Status' < \"Resolved\""
        )
        r = client.get(
            f"{self._base}/api/arsys/v1/entry/HPD:IncidentInterface",
            params={"q": qualification, "fields": "values(Request ID)"},
            headers={"Authorization": headers["Authorization"]},
        )
        r.raise_for_status()
        entries = r.json().get("entries", [])
        if not entries:
            log.info("no open BMC Helix incident for %s; nothing to resolve", marker)
            return
        for entry in entries:
            request_id = entry["values"]["Request ID"]
            self._set_resolved(client, headers, request_id, e)

    def _set_resolved(
        self, client: httpx.Client, headers: dict, request_id: str, e: CanonicalEvent
    ) -> None:
        values = {
            "Status": self._resolved_status,
            "Status_Reason": "Automated Resolution Reported",
            "Resolution": f"COM reported recovery: {e.title}",
        }
        r = client.put(
            f"{self._base}/api/arsys/v1/entry/HPD:IncidentInterface/{request_id}",
            json={"values": values},
            headers=headers,
        )
        r.raise_for_status()
