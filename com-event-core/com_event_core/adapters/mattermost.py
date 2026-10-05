"""Mattermost adapter.

Posts a COM event as a formatted message to a Mattermost **Incoming Webhook**.
Mattermost incoming webhooks are post-only (no edit/close), so a **raise** and
its later **clear** are posted as two messages, the clear rendered as a green
"Resolved" notice referencing the same resource.

Mattermost accepts Slack-style *message attachments* (title, markdown text,
fields) but does not render Slack Block Kit, so this adapter builds attachments
instead of reusing the Slack payload. Sections stay separate attachments: the
event details, the optional AI analysis, and the optional HPE Customer
Advisories.

Env:
  MATTERMOST_WEBHOOK_URL  Incoming Webhook URL (required, secret), e.g.
                          http://mattermost.example.com:8065/hooks/<id>. Create
                          it under Integrations > Incoming Webhooks.
  MATTERMOST_USERNAME     Optional override for the posting name. Ignored by the
                          server unless "Enable integrations to override
                          usernames" is on.
  TARGET_TIMEOUT          Per-request HTTP timeout in seconds (default 15).
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

log = logging.getLogger("com_event_core.adapter.mattermost")

_COLOR = {
    "critical": "#D32F2F",
    "major": "#F44336",
    "minor": "#FB8C00",
    "warning": "#FFB300",
    "normal": "#43A047",
}
_RESOLVED_COLOR = "#43A047"
_ANALYSIS_COLOR = "#5C6BC0"
_ADVISORY_COLOR = "#8D6E63"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _check(r: httpx.Response) -> None:
    """Raise on a failed call, including the reason Mattermost sent back.

    ``raise_for_status()`` alone reports only the status line, but Mattermost
    explains the rejection in its JSON body (``message``), for example a webhook
    whose channel it cannot post to.
    """
    if not r.is_error:
        return
    try:
        detail = str(r.json().get("message") or "")
    except ValueError:
        detail = r.text[:500]
    raise RuntimeError(
        f"Mattermost webhook failed: HTTP {r.status_code}{' - ' + detail if detail else ''}"
    )


class MattermostAdapter(TargetAdapter):
    name = "mattermost"

    def __init__(self) -> None:
        self._url = get_secret("MATTERMOST_WEBHOOK_URL")
        self._username = os.environ.get("MATTERMOST_USERNAME")
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))

    def _to_message(self, e: CanonicalEvent) -> dict:
        resolved = e.action == ACTION_CLEAR
        color = _RESOLVED_COLOR if resolved else _COLOR.get(e.severity, "#757575")
        prefix = ":white_check_mark: Resolved" if resolved else f":rotating_light: {e.severity.upper()}"
        header = _clip(f"{prefix}: {e.title}", 300)

        details = [
            {"short": True, "title": "Severity", "value": e.severity},
            {"short": True, "title": "Action", "value": e.action},
            {"short": True, "title": "Resource",
             "value": e.resource_name or e.resource_serial or "unknown"},
            {"short": True, "title": "Model", "value": e.resource_model or "n/a"},
            {"short": True, "title": "Serial", "value": e.resource_serial or "n/a"},
        ]
        links = []
        if e.mgmt_url:
            links.append(f"[Open iLO]({e.mgmt_url})")
        links.append(f"[Open HPE GreenLake]({GREENLAKE_URL})")

        main_text = "\n\n".join(
            p for p in (_clip(e.description or "", 3000), " | ".join(links)) if p
        )
        attachments: list[dict] = [{
            "fallback": header,
            "color": color,
            "text": main_text,
            "fields": details,
        }]

        # Optional AI analysis (empty unless an enricher ran successfully).
        if has_analysis(e):
            fields = []
            if e.analysis_root_cause:
                fields.append({"short": False, "title": "Likely root cause",
                               "value": _clip(e.analysis_root_cause, 1500)})
            if e.analysis_confidence is not None:
                fields.append({"short": True, "title": "Confidence",
                               "value": f"{e.analysis_confidence:.0%}"})
            if e.analysis_actions:
                actions = "\n".join(f"{i}. {a}" for i, a in enumerate(e.analysis_actions, 1))
                fields.append({"short": False, "title": "Recommended actions",
                               "value": _clip(actions, 3000)})
            attachments.append({
                "fallback": HEADING,
                "color": _ANALYSIS_COLOR,
                "title": HEADING,
                "text": _clip(e.analysis_summary or "", 3000),
                "fields": fields,
            })

        # Optional HPE Customer Advisories (empty unless hpe_advisories matched).
        if has_advisories(e):
            lines = []
            for ref in e.advisory_references:
                label = ref["status"].upper()
                title = ref.get("title") or "(untitled advisory)"
                line = f"**[{label}]** " + (f"`{ref['id']}` " if ref.get("id") else "") + title
                if ref.get("url"):
                    line += f" ([View advisory]({ref['url']}))"
                lines.append(f"- {line}")
            attachments.append({
                "fallback": ADVISORY_HEADING,
                "color": _ADVISORY_COLOR,
                "title": ADVISORY_HEADING,
                "text": _clip("\n".join(lines), 3500),
            })

        message: dict = {"text": f"#### {header}", "attachments": attachments}
        if self._username:
            message["username"] = self._username
        return message

    def forward(self, event: CanonicalEvent) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(self._url, json=self._to_message(event))
            _check(r)
        log.info("event %s (%s) forwarded to Mattermost", event.event_id, event.action)
