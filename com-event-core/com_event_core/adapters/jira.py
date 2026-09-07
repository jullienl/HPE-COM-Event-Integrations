"""Jira Service Management adapter.

Creates a Jira issue/request on a COM **raise** and closes it on the matching
**clear**, via the Jira Cloud REST API v3 (Basic auth with an account email +
API token). Works for Jira Service Management and Jira Software projects alike —
the difference is only the project key and issue type you point it at.

Lifecycle (raise / clear)
-------------------------
Jira has no native event correlation, so this adapter does it explicitly with a
**label** derived from the correlation key (Jira labels can't contain spaces, so
``server:SER1:health`` becomes ``com-server_SER1_health``):

* raise: create an issue in ``JIRA_PROJECT_KEY`` tagged with that label.
* clear: search open issues carrying the label (JQL) and drive each through the
  ``JIRA_CLOSE_TRANSITION`` transition to a resolved/done status.

Because the close is a **named transition** (not a status write), set
``JIRA_CLOSE_TRANSITION`` to a transition that exists on your project's workflow
(commonly "Done" or "Resolve issue").

Env:
  JIRA_URL              Site base URL, e.g. https://acme.atlassian.net
  JIRA_EMAIL            Atlassian account email (Basic auth username).
  JIRA_API_TOKEN        API token for that account (secret).
  JIRA_PROJECT_KEY      Project the issues are created in, e.g. "OPS".
  JIRA_ISSUE_TYPE       Issue type name (default "Incident").
  JIRA_CLOSE_TRANSITION Transition name used to close on a clear (default "Done").
  JIRA_LABELS           Optional extra labels (comma-separated) added to new issues.
  TARGET_TIMEOUT        Per-request HTTP timeout in seconds (default 15).
"""

from __future__ import annotations

import logging
import os
import re

import httpx

from com_event_core.normalize import ACTION_CLEAR, CanonicalEvent
from com_event_core.secrets import get_secret
from .base import TargetAdapter

log = logging.getLogger("com_event_core.adapter.jira")

# Jira labels may not contain whitespace; normalise everything else to '_'.
_LABEL_SANITISE = re.compile(r"[^A-Za-z0-9_.-]")


class JiraAdapter(TargetAdapter):
    name = "jira"

    def __init__(self) -> None:
        self._api = os.environ["JIRA_URL"].rstrip("/")
        self._email = os.environ["JIRA_EMAIL"]
        self._token = get_secret("JIRA_API_TOKEN")
        self._project = os.environ["JIRA_PROJECT_KEY"]
        self._issue_type = os.environ.get("JIRA_ISSUE_TYPE", "Incident")
        self._close_transition = os.environ.get("JIRA_CLOSE_TRANSITION", "Done")
        extra = os.environ.get("JIRA_LABELS", "")
        self._extra_labels = [s.strip() for s in extra.split(",") if s.strip()]
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))
        self._auth = (self._email, self._token)
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}

    def _corr_label(self, e: CanonicalEvent) -> str:
        key = e.correlation_key or e.dedup_key
        return "com-" + _LABEL_SANITISE.sub("_", key)

    def _adf(self, e: CanonicalEvent) -> dict:
        """Build an Atlassian Document Format body (required by REST v3)."""
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
        text = "\n".join(lines)
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": text}]}
            ],
        }

    def forward(self, event: CanonicalEvent) -> None:
        with httpx.Client(timeout=self._timeout, auth=self._auth) as client:
            if event.action == ACTION_CLEAR:
                self._close(client, event)
            else:
                self._create(client, event)
        log.info("event %s (%s) forwarded to Jira", event.event_id, event.action)

    def _create(self, client: httpx.Client, e: CanonicalEvent) -> None:
        fields = {
            "project": {"key": self._project},
            "issuetype": {"name": self._issue_type},
            "summary": e.title[:255],
            "description": self._adf(e),
            "labels": [self._corr_label(e), *self._extra_labels],
        }
        r = client.post(
            f"{self._api}/rest/api/3/issue", json={"fields": fields}, headers=self._headers
        )
        r.raise_for_status()

    def _close(self, client: httpx.Client, e: CanonicalEvent) -> None:
        """Find open issue(s) with this correlation label and transition to done."""
        label = self._corr_label(e)
        jql = (
            f'project = "{self._project}" AND labels = "{label}" '
            f"AND statusCategory != Done"
        )
        r = client.post(
            f"{self._api}/rest/api/3/search",
            json={"jql": jql, "fields": ["key"], "maxResults": 50},
            headers=self._headers,
        )
        r.raise_for_status()
        keys = [it["key"] for it in r.json().get("issues", [])]
        if not keys:
            log.info("no open Jira issue for %s; nothing to close", label)
            return
        for key in keys:
            self._transition(client, key, e)

    def _transition(self, client: httpx.Client, key: str, e: CanonicalEvent) -> None:
        # Resolve the transition id for the configured close transition name.
        tr = client.get(
            f"{self._api}/rest/api/3/issue/{key}/transitions", headers=self._headers
        )
        tr.raise_for_status()
        wanted = self._close_transition.strip().lower()
        match = next(
            (t for t in tr.json().get("transitions", [])
             if t.get("name", "").strip().lower() == wanted),
            None,
        )
        if match is None:
            available = ", ".join(t.get("name", "") for t in tr.json().get("transitions", []))
            raise RuntimeError(
                f"Jira issue {key}: no transition '{self._close_transition}' "
                f"(available: {available})"
            )
        r = client.post(
            f"{self._api}/rest/api/3/issue/{key}/transitions",
            json={"transition": {"id": match["id"]}},
            headers=self._headers,
        )
        r.raise_for_status()
