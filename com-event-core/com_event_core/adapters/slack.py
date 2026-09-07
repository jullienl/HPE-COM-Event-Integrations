"""Slack adapter.

Posts a COM event as a formatted message to a Slack **Incoming Webhook**. Good
for chat-ops visibility. Slack incoming webhooks are post-only (no edit/close),
so a **raise** and its later **clear** are posted as two messages — the clear is
rendered as a green "Resolved" notice referencing the same resource.

Env:
  SLACK_WEBHOOK_URL   Incoming Webhook URL (required, secret). Create one at
                      https://api.slack.com/messaging/webhooks — it already
                      encodes the target workspace + channel.
  SLACK_USERNAME      Optional override for the posting name.
  TARGET_TIMEOUT      Per-request HTTP timeout in seconds (default 15).
"""

from __future__ import annotations

import logging
import os

import httpx

from com_event_core.normalize import ACTION_CLEAR, CanonicalEvent
from com_event_core.secrets import get_secret
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.slack")

# Severity -> attachment colour (hex bar down the left of the message).
_COLOR = {
    "critical": "#D32F2F",
    "major": "#F44336",
    "minor": "#FB8C00",
    "warning": "#FFB300",
    "normal": "#43A047",
}
_RESOLVED_COLOR = "#43A047"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


class SlackAdapter(TargetAdapter):
    name = "slack"

    def __init__(self) -> None:
        self._url = get_secret("SLACK_WEBHOOK_URL")
        self._username = os.environ.get("SLACK_USERNAME")
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))

    def _to_message(self, e: CanonicalEvent) -> dict:
        resolved = e.action == ACTION_CLEAR
        color = _RESOLVED_COLOR if resolved else _COLOR.get(e.severity, "#757575")
        prefix = "\u2705 Resolved" if resolved else f"\U0001f6a8 {e.severity.upper()}"
        header = _clip(f"{prefix} — {e.title}", 150)

        fields = [
            {"type": "mrkdwn", "text": f"*Severity:*\n{e.severity}"},
            {"type": "mrkdwn", "text": f"*Action:*\n{e.action}"},
            {"type": "mrkdwn",
             "text": f"*Resource:*\n{e.resource_name or e.resource_serial or 'unknown'}"},
            {"type": "mrkdwn", "text": f"*Model:*\n{e.resource_model or 'n/a'}"},
        ]
        blocks: list[dict] = [
            {"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}},
            {"type": "section", "fields": fields},
        ]
        if e.description:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": _clip(e.description, 2900)},
            })
        if e.mgmt_url:
            blocks.append({
                "type": "actions",
                "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open in COM"},
                    "url": e.mgmt_url,
                }],
            })

        message: dict = {
            "text": header,  # fallback for notifications / no-block clients
            "attachments": [{"color": color, "blocks": blocks}],
        }
        if self._username:
            message["username"] = self._username
        return message

    def forward(self, event: CanonicalEvent) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(self._url, json=self._to_message(event))
            r.raise_for_status()
        log.info("event %s (%s) forwarded to Slack", event.event_id, event.action)
