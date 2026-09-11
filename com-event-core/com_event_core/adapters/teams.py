"""Microsoft Teams adapter.

Posts a COM event as an **Adaptive Card** to a Teams channel via an incoming
webhook created with **Workflows** (Power Automate) — the current, non-retired
way to push into Teams. Like Slack, the webhook is post-only, so a **raise** and
its later **clear** are two messages (the clear is a green "Resolved" card).

What this adapter POSTs to TEAMS_WEBHOOK_URL is the Workflows message envelope
with the Adaptive Card nested inside — there is **no** top-level ``text`` field:

    {
      "type": "message",
      "attachments": [
        {
          "contentType": "application/vnd.microsoft.card.adaptive",
          "content": { ...the Adaptive Card (title, FactSet, buttons)... }
        }
      ]
    }

So in Power Automate the card lives at
``triggerBody()?['attachments']?[0]?['content']`` and ``triggerBody()?['text']``
is always empty — do NOT bind a "Post message" action to ``['text']``.

Setting up the Power Automate flow (the only supported path today — the old
"Incoming Webhook" Office 365 connector is retired):

  1. Teams channel -> **...** (More options) -> **Workflows** (or the standalone
     **Power Automate** portal, https://make.powerautomate.com -> **Create** ->
     **Automated cloud flow**).
  2. **Trigger:** *When a Teams webhook request is received*. Set
     **Who can trigger the flow?** to **Anyone**. Save once to generate the
     **HTTP POST URL** — this is your ``TEAMS_WEBHOOK_URL``.
  3. **Action:** *Post card in a chat or channel*
       - **Post as:** Flow bot   - **Post in:** Channel
       - **Team** / **Channel:** pick the destination.
       - **Adaptive Card** (this is the field that must match our payload):
         switch it to the expression editor and enter

             string(triggerBody()?['attachments']?[0]?['content'])

         The ``string(...)`` wrap is required because the *Adaptive Card* input
         is a string field while the extracted ``content`` is a JSON object.
  4. (Optional) Password-protect the trigger with a trigger condition on a
     custom header, e.g.
         @equals(triggerOutputs()?['headers']['X-Trigger-Secret'], 'my-secret')
     Note: this adapter does not send that header yet, so leave the condition
     off unless you also add the header to the request.
  5. Save. POST a raise/clear through the shim (or curl the envelope above) to
     confirm the card renders.

Why not bind to ``triggerBody()?['text']``? Because the adapter sends a card,
not a plain string. If you specifically need the simple *Post message in a chat
or channel* action (plain text), bind its message to the card's title instead:

    triggerBody()?['attachments']?[0]?['content']?['body']?[0]?['text']

(that yields "<prefix> - <title>" only, dropping the FactSet, colour and the
Open iLO / Open HPE GreenLake buttons).

Set up the webhook in Teams: channel -> Workflows -> "Post to a channel when a
webhook request is received" (or build the flow above). Copy the generated URL
into TEAMS_WEBHOOK_URL.

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
