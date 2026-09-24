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

from com_event_core.enrich.render import (
    ADVISORY_HEADING,
    GREENLAKE_URL,
    HEADING,
    has_advisories,
    has_analysis,
)
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
            {"type": "mrkdwn", "text": f"*Serial:*\n{e.resource_serial or 'n/a'}"},
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
        # Optional AI analysis (empty unless an enricher ran successfully).
        # Rendered as separate Block Kit blocks — not one joined mrkdwn blob —
        # so summary / root cause / confidence / actions stay visually distinct
        # instead of running together as a wall of text.
        if has_analysis(e):
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f":robot_face: *{HEADING}*"}],
            })
            if e.analysis_summary:
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": _clip(e.analysis_summary, 2900)},
                })
            meta_fields = []
            if e.analysis_root_cause:
                meta_fields.append({
                    "type": "mrkdwn",
                    "text": f"*Likely root cause:*\n{_clip(e.analysis_root_cause, 1500)}",
                })
            if e.analysis_confidence is not None:
                meta_fields.append({
                    "type": "mrkdwn",
                    "text": f"*Confidence:*\n{e.analysis_confidence:.0%}",
                })
            if meta_fields:
                blocks.append({"type": "section", "fields": meta_fields})
            if e.analysis_actions:
                actions_text = "\n".join(
                    f"{i}. {a}" for i, a in enumerate(e.analysis_actions, 1)
                )
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn",
                             "text": _clip(f"*Recommended actions:*\n{actions_text}", 2900)},
                })
        # Optional HPE Customer Advisories (empty unless hpe_advisories ran and
        # matched something). Rendered as its own blocks, same reasoning as the
        # AI analysis above: separate blocks keep each advisory scannable.
        if has_advisories(e):
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f":memo: *{ADVISORY_HEADING}*"}],
            })
            for ref in e.advisory_references:
                label = ref["status"].upper()
                title = ref.get("title") or "(untitled advisory)"
                text = f"*[{label}]* " + (f"`{ref['id']}` " if ref.get("id") else "") + title
                if ref.get("url"):
                    text += f"\n<{ref['url']}|View advisory>"
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": _clip(text, 2900)},
                })
        elements: list[dict] = []
        if e.mgmt_url:
            elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": "Open iLO"},
                "url": e.mgmt_url,
            })
        elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "Open HPE GreenLake"},
            "url": GREENLAKE_URL,
        })
        blocks.append({"type": "actions", "elements": elements})

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
