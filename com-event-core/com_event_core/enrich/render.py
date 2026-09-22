"""Shared rendering of an event's AI analysis.

Adapters differ in markup (GitHub Markdown, Jira ADF, Slack mrkdwn, Teams
Adaptive Card) but not in *what* they say about an analysis or the section
order (summary / root cause+confidence / recommended actions): every adapter
renders those four `analysis_*` fields as visually distinct sections (bold
labels, separate blocks/paragraphs/nodes), not as one run-together blob — this
module owns the wording and heading text once (`HEADING`, `has_analysis()`) so
the four targets can't drift apart on that.

`analysis_lines()`/`analysis_text()` below are a plain-text fallback (blank-line
paragraph grouping) for callers with no native rich layout to build. Adapters
that have one (Slack Block Kit, Teams Adaptive Card FactSets, Jira ADF
paragraphs/lists, GitHub Markdown bold+headings) build their own structure
directly from `event.analysis_summary` / `analysis_root_cause` /
`analysis_confidence` / `analysis_actions` instead of calling these two.

Two rules every caller must respect:

* **The analysis is optional.** Enrichment is disabled by default and fail-open
  when enabled, so `has_analysis()` returning `False` is the normal case, not an
  error. Never render an empty "AI analysis" heading/section.
* **Label it as machine-generated.** The text goes into an incident ticket a
  human will act on; it must be obvious which part a model wrote.
"""

from __future__ import annotations

from ..normalize import CanonicalEvent

#: Heading used above the analysis by every adapter.
HEADING = "AI analysis (automated — verify before acting)"


def has_analysis(event: CanonicalEvent) -> bool:
    """True when the enrichment stage produced something worth rendering."""
    return bool(
        event.analysis_summary
        or event.analysis_root_cause
        or event.analysis_actions
    )


def analysis_lines(event: CanonicalEvent) -> list[str]:
    """Plain-text lines describing the analysis; empty when there is none.

    The heading is *not* included — adapters that support real headings
    (Markdown, Adaptive Card) render `HEADING` themselves in their own style.

    Lines are grouped into blank-line-separated paragraphs (summary / root
    cause+confidence / recommended actions) so a plain `"\n".join(...)` still
    reads as distinct sections instead of one dense block. Adapters with richer
    layout (Slack fields, Adaptive Card blocks) may render the underlying
    `analysis_*` attributes directly instead of this flattened form.
    """
    if not has_analysis(event):
        return []

    lines: list[str] = []
    if event.analysis_summary:
        lines.append(event.analysis_summary)

    meta: list[str] = []
    if event.analysis_root_cause:
        meta.append(f"Likely root cause: {event.analysis_root_cause}")
    if event.analysis_confidence is not None:
        meta.append(f"Confidence: {event.analysis_confidence:.0%}")
    if meta:
        if lines:
            lines.append("")
        lines += meta

    if event.analysis_actions:
        if lines:
            lines.append("")
        lines.append("Recommended actions:")
        lines += [f"{i}. {a}" for i, a in enumerate(event.analysis_actions, 1)]
    return lines


def analysis_text(event: CanonicalEvent) -> str:
    """The analysis as one newline-joined block, `""` when there is none."""
    lines = analysis_lines(event)
    return "\n".join([HEADING, *lines]) if lines else ""


#: Heading used above the advisory references by every adapter.
ADVISORY_HEADING = "HPE Customer Advisories (from installed firmware bundle)"


def has_advisories(event: CanonicalEvent) -> bool:
    """True when the hpe_advisories enricher attached anything worth rendering."""
    return bool(event.advisory_references)


def advisory_lines(event: CanonicalEvent) -> list[str]:
    """Plain-text lines describing matched advisories; empty when there are none.

    One line per advisory: its status, id (when known), and title, followed by
    its URL on the next line so it reads as a reference rather than a claim.
    The heading is *not* included here, matching `analysis_lines()`'s contract.
    """
    if not has_advisories(event):
        return []

    lines: list[str] = []
    for ref in event.advisory_references:
        label = ref["status"].upper()
        title = ref.get("title") or "(untitled advisory)"
        prefix = f"[{label}] {ref['id']}: " if ref.get("id") else f"[{label}] "
        lines.append(f"{prefix}{title}")
        if ref.get("url"):
            lines.append(f"  {ref['url']}")
    return lines


def advisory_text(event: CanonicalEvent) -> str:
    """The advisory references as one newline-joined block, `""` when none."""
    lines = advisory_lines(event)
    return "\n".join([ADVISORY_HEADING, *lines]) if lines else ""
