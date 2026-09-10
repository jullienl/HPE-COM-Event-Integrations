"""Microsoft Teams adapter.

Posts a COM event as an **Adaptive Card** to a Teams channel via an incoming
webhook created with **Workflows** (Power Automate) — the current, non-retired
way to push into Teams. Like Slack, the webhook is post-only, so a **raise** and
its later **clear** are two messages (the clear is a green "Resolved" card).

Set up the webhook in Teams: channel → Workflows → "Post to a channel when a
webhook request is received". Copy the generated URL into TEAMS_WEBHOOK_URL.

Env:
  TEAMS_WEBHOOK_URL   Workflows webhook URL (required, secret).
  TARGET_TIMEOUT      Per-request HTTP timeout in seconds (default 15).

The payload is the documented envelope for posting an Adaptive Card to the
Workflows trigger: {"type":"message","attachments":[{contentType, content}]}.
"""

from __future__ import annotations

import logging
import os

import httpx

from com_event_core.normalize import ACTION_CLEAR, CanonicalEvent
from com_event_core.secrets import get_secret
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.teams")

# Severity -> Adaptive Card text colour keyword.
_COLOR = {
    "critical": "attention",
    "major": "attention",
    "minor": "warning",
    "warning": "warning",
    "normal": "good",
}

# HPE GreenLake / Compute Ops Management console (same URL for every tenant).
_GREENLAKE_URL = "https://common.cloud.hpe.com/"


class TeamsAdapter(TargetAdapter):
    name = "teams"

    def __init__(self) -> None:
        self._url = get_secret("TEAMS_WEBHOOK_URL")
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))

    def _card(self, e: CanonicalEvent) -> dict:
        resolved = e.action == ACTION_CLEAR
        color = "good" if resolved else _COLOR.get(e.severity, "default")
        prefix = "\u2705 Resolved" if resolved else f"\U0001f6a8 {e.severity.upper()}"

        facts = [
            {"title": "Severity", "value": e.severity},
            {"title": "Action", "value": e.action},
            {"title": "Resource",
             "value": e.resource_name or e.resource_serial or "unknown"},
            {"title": "Model", "value": e.resource_model or "n/a"},
            {"title": "Serial", "value": e.resource_serial or "n/a"},
            {"title": "Time", "value": e.time_created or "n/a"},
        ]
        body: list[dict] = [
            {"type": "TextBlock", "size": "Large", "weight": "Bolder",
             "color": color, "wrap": True, "text": f"{prefix} — {e.title}"},
            {"type": "FactSet", "facts": facts},
        ]
        if e.description:
            body.append({"type": "TextBlock", "wrap": True, "text": e.description})
        if e.resolution:
            body.append({"type": "TextBlock", "wrap": True, "isSubtle": True,
                         "text": f"Suggested resolution: {e.resolution}"})

        card: dict = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
            "body": body,
        }
        if e.mgmt_url:
            actions = [
                {"type": "Action.OpenUrl", "title": "Open iLO", "url": e.mgmt_url}
            ]
        else:
            actions = []
        actions.append(
            {"type": "Action.OpenUrl", "title": "Open HPE GreenLake", "url": _GREENLAKE_URL}
        )
        card["actions"] = actions
        return card

    def forward(self, event: CanonicalEvent) -> None:
        envelope = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": self._card(event),
            }],
        }
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(self._url, json=envelope)
            r.raise_for_status()
        log.info("event %s (%s) forwarded to Teams", event.event_id, event.action)
