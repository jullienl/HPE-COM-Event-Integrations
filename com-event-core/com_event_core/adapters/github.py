"""GitHub Issues adapter.

Opens a GitHub issue on a COM **raise** and closes it on the matching **clear**,
so an operator sees one issue per problem that auto-resolves on recovery.

Correlation without external state: every created issue gets a **label**
``com:<correlation_key>`` (GitHub creates unknown labels automatically) and a
hidden marker line in the body. On a clear, the adapter finds the still-open
issue by that label via the Search API and closes it — no database needed.

Env:
  GITHUB_REPO       "owner/repo" slug the issues live in, e.g. jullienl/my-repo
                    (slug only, NOT a URL) (required).
  GITHUB_TOKEN      Token with issues:write on the repo (required, secret) — a
                    fine-grained PAT (Issues: Read and write) or classic PAT.
  GITHUB_API_URL    API base. Default https://api.github.com. For GitHub
                    Enterprise Server use e.g. https://ghe.example.com/api/v3.
  GITHUB_LABELS     Optional extra labels (comma-separated) added to new issues.
  TARGET_TIMEOUT    Per-request HTTP timeout in seconds (default 15).

Note: GitHub's search index updates with a short delay, so a clear that arrives
within a second or two of its raise may not find the issue yet; the shared retry
(queue redelivery / spool) re-attempts it, so it closes on a later pass.
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

log = logging.getLogger("com_event_core.adapter.github")

_MARKER = "com-correlation:"  # hidden body marker (human-visible audit trail)


class GitHubAdapter(TargetAdapter):
    name = "github"

    def __init__(self) -> None:
        self._repo = os.environ["GITHUB_REPO"].strip("/")  # owner/repo
        self._api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self._timeout = int(os.environ.get("TARGET_TIMEOUT", "15"))
        extra = os.environ.get("GITHUB_LABELS", "")
        self._extra_labels = [s.strip() for s in extra.split(",") if s.strip()]
        token = get_secret("GITHUB_TOKEN")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _corr_label(self, e: CanonicalEvent) -> str:
        return f"com:{e.correlation_key or e.dedup_key}"

    def _body(self, e: CanonicalEvent) -> str:
        lines = [
            f"**COM event** `{e.event_id}` — operation **{e.operation}**",
            "",
            f"- **Severity:** {e.severity}",
            f"- **Resource:** {e.resource_name or e.resource_serial or 'unknown'}"
            f" ({e.resource_model or 'unknown model'})",
            f"- **Serial:** {e.resource_serial or 'n/a'}",
            f"- **Time:** {e.time_created or 'n/a'}",
        ]
        if e.mgmt_url:
            lines.append(f"- [Open iLO]({e.mgmt_url})")
        lines.append(f"- [Open HPE GreenLake]({GREENLAKE_URL})")
        if e.description:
            lines += ["", e.description]
        if e.resolution:
            lines += ["", f"**Suggested resolution:** {e.resolution}"]
        # Optional AI analysis (empty unless an enricher ran successfully).
        # Built as its own bold-labelled, blank-line-separated paragraphs — not
        # the flattened analysis_lines() text — so summary / root cause /
        # confidence / actions read as distinct sections in rendered Markdown.
        if has_analysis(e):
            lines += ["", f"### {HEADING}", ""]
            if e.analysis_summary:
                lines += [e.analysis_summary, ""]
            meta = []
            if e.analysis_root_cause:
                meta.append(f"**Likely root cause:** {e.analysis_root_cause}")
            if e.analysis_confidence is not None:
                meta.append(f"**Confidence:** {e.analysis_confidence:.0%}")
            if meta:
                lines += [*meta, ""]
            if e.analysis_actions:
                lines.append("**Recommended actions:**")
                lines += [f"{i}. {a}" for i, a in enumerate(e.analysis_actions, 1)]
        # Optional HPE Customer Advisories (empty unless hpe_advisories ran and
        # matched something).
        if has_advisories(e):
            lines += ["", f"### {ADVISORY_HEADING}", ""]
            for ref in e.advisory_references:
                label = ref["status"].upper()
                title = ref.get("title") or "(untitled advisory)"
                prefix = f"**[{label}]** `{ref['id']}` " if ref.get("id") else f"**[{label}]** "
                line = f"- {prefix}{title}"
                if ref.get("url"):
                    line += f" — [advisory]({ref['url']})"
                lines.append(line)
        # Hidden marker so the correlation is auditable in the issue body.
        lines += ["", f"<!-- {_MARKER} {e.correlation_key or e.dedup_key} -->"]
        return "\n".join(lines)

    def forward(self, event: CanonicalEvent) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            if event.action == ACTION_CLEAR:
                self._close(client, event)
            else:
                self._create(client, event)
        log.info("event %s (%s) forwarded to GitHub", event.event_id, event.action)

    def _create(self, client: httpx.Client, e: CanonicalEvent) -> None:
        payload = {
            "title": e.title,
            "body": self._body(e),
            "labels": [self._corr_label(e), *self._extra_labels],
        }
        r = client.post(
            f"{self._api}/repos/{self._repo}/issues", json=payload, headers=self._headers
        )
        r.raise_for_status()

    def _close(self, client: httpx.Client, e: CanonicalEvent) -> None:
        """Find the open issue(s) for this problem by label and close them."""
        label = self._corr_label(e)
        q = f'repo:{self._repo} is:issue is:open label:"{label}"'
        r = client.get(
            f"{self._api}/search/issues", params={"q": q}, headers=self._headers
        )
        r.raise_for_status()
        numbers = [it["number"] for it in r.json().get("items", []) if "number" in it]
        if not numbers:
            log.info("no open GitHub issue for %s; nothing to close", label)
            return
        for n in numbers:
            client.post(
                f"{self._api}/repos/{self._repo}/issues/{n}/comments",
                json={"body": f"Resolved by COM clear event `{e.event_id}`."},
                headers=self._headers,
            ).raise_for_status()
            client.patch(
                f"{self._api}/repos/{self._repo}/issues/{n}",
                json={"state": "closed", "state_reason": "completed"},
                headers=self._headers,
            ).raise_for_status()
