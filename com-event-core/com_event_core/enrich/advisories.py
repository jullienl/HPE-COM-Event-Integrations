"""HPE firmware-bundle advisory fetch + defensive HTML parsing.

The authoritative, machine-readable entry point is the COM API: a server's
``firmwareBundleUri`` -> ``GET /compute-ops-mgmt/v1/firmware-bundles/{id}`` ->
that bundle's ``advisories`` field, which is a URL into an HPE SPP/support
document. This module fetches *only* that official URL — never a broad HPE
Support Center search — and extracts the two sections a bundle's advisory page
carries: **Open Customer Advisories** (CAs not yet resolved by any bundle) and
**Resolved Customer Advisories** (CAs this bundle's firmware fixes).

Two extraction strategies, tried in order
------------------------------------------
1. **The static per-bundle JSON asset** (`fetch_advisories_json`). The
   advisories page is an Angular app that itself fetches a same-origin static
   JSON file to render its Open/Resolved CA tables — verified live (2026-09,
   via browser devtools Network tab) at
   ``.../spp/assets/<version>.json`` (sibling to ``.../spp/index.aspx?
   version=<version>``), containing a clean ``Advisories.OpendCAs`` /
   ``Advisories.ResolvedCAs`` array (``CA``, ``Description``, ``CALink``,
   ``FixedSPPVersion``, ``Date`` fields). This needs no JS engine — a plain
   GET returns the real data — but it is an **undocumented, unversioned
   static asset path**, not a published API, so it is used as a best-effort
   optimisation only, never as the sole path.
2. **Static HTML heading parse** (`parse_advisories_html`, below) — the
   fallback safety net if the JSON asset can't be derived, fetched, or
   doesn't have the expected shape. Verified live: the raw HTML of the
   advisories page itself contains no Open/Resolved CA markup at all (it's an
   empty ``<div id="root">`` app shell), so this fallback currently returns
   `parsed_ok: False` for real pages — kept anyway in case a future/older
   doc-generation template serves the sections as static HTML instead.

Both paths degrade to an empty, ``parsed_ok=False`` result (logged once)
rather than raising or returning stale/mixed data on any unexpected shape or
transport failure. Callers must treat that the same as "no advisories" — not
as an error worth retrying.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse, urlunparse

import httpx

log = logging.getLogger("com_event_core.enrich.advisories")

# Section headings are matched loosely: HPE varies "Customer Advisories" vs.
# "Customer Advisory" vs. "CAs" across doc generations.
_OPEN_HEADING = re.compile(r"open\s+customer\s+advisor", re.IGNORECASE)
_RESOLVED_HEADING = re.compile(r"resolved\s+customer\s+advisor", re.IGNORECASE)
_HEADING_TAGS = {"h1", "h2", "h3", "h4"}
_ENTRY_TAGS = {"li", "tr"}

# "<id>: <title>" / "<id> - <title>" / "<id> – <title>" — HPE advisory listings
# commonly lead with a short alphanumeric-with-dashes id before the title.
_ID_PREFIX = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]{2,})\s*[:\-\u2013]\s*(.+)$")
# "[Component] rest of title" — an optional bracketed component/category tag.
_COMPONENT_PREFIX = re.compile(r"^\s*\[([^\]]+)\]\s*(.+)$")


def fetch_advisories_page(url: str, *, timeout: float = 20.0) -> str:
    """GET the advisory document `url` and return its HTML body.

    Raises on any transport/HTTP error; the caller (the enricher) treats that
    as "no advisory evidence this time" and fails open, same as every other
    step in the enrichment pipeline.
    """
    r = httpx.get(url, timeout=timeout, follow_redirects=True,
                  headers={"Accept": "text/html"})
    r.raise_for_status()
    return r.text


def _derive_json_asset_url(advisories_url: str) -> str | None:
    """Derive the static per-bundle JSON asset URL from an advisories page URL.

    Verified live (2026-09): ``.../spp/index.aspx?version=<v>`` and
    ``.../spp/assets/<v>.json`` are siblings under the same directory, so the
    asset path is built by swapping the page's filename for
    ``assets/<version>.json`` using the URL's own ``version`` query param —
    nothing about a specific generation/version is hardcoded here. Returns
    None (not an error) when the URL has no scheme or no ``version`` param, so
    an unexpected advisories URL shape just skips this optimisation.
    """
    parsed = urlparse(advisories_url)
    if not parsed.scheme or not parsed.path:
        return None
    version = (parse_qs(parsed.query).get("version") or [None])[0]
    if not version:
        return None
    directory = parsed.path.rsplit("/", 1)[0]
    asset_path = f"{directory}/assets/{version}.json"
    return urlunparse((parsed.scheme, parsed.netloc, asset_path, "", "", ""))


def _cas_from_json(entries: list[dict] | None) -> list[dict]:
    """Map the JSON asset's CA objects onto this module's common CA shape."""
    out: list[dict] = []
    for item in entries or []:
        out.append({
            "id": item.get("CA"),
            "component": None,
            "title": item.get("Description"),
            "url": item.get("CALink"),
        })
    return out


def fetch_advisories_json(url: str, *, timeout: float = 20.0) -> dict | None:
    """Best-effort fetch of the static JSON asset backing an advisories page.

    Returns ``None`` — never raises — when the asset path can't be derived,
    the fetch fails, or the body doesn't have the expected ``Advisories`` key,
    so every caller falls back to `fetch_advisories_page`/
    `parse_advisories_html` in every one of those cases. This is an
    undocumented, unversioned asset path (see module docstring), so treat it
    as an optimisation, never the only source of truth.
    """
    asset_url = _derive_json_asset_url(url)
    if not asset_url:
        return None
    try:
        r = httpx.get(asset_url, timeout=timeout, follow_redirects=True,
                      headers={"Accept": "application/json"})
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        log.info(
            "advisories JSON asset fetch failed (%s); falling back to the "
            "static HTML parse", e,
        )
        return None

    advisories = body.get("Advisories") if isinstance(body, dict) else None
    if not isinstance(advisories, dict):
        log.info(
            "advisories JSON asset at %s has no 'Advisories' object; falling "
            "back to the static HTML parse", asset_url,
        )
        return None

    return {
        "open": _cas_from_json(advisories.get("OpendCAs")),
        "resolved": _cas_from_json(advisories.get("ResolvedCAs")),
        "parsed_ok": True,
    }


def fetch_and_parse_advisories(url: str, *, timeout: float = 20.0) -> dict:
    """Resolve Open/Resolved CAs for an advisories URL: JSON asset first, the
    static HTML heading parse as the fallback safety net.

    Both paths return the same shape: ``{"open": [...], "resolved": [...],
    "parsed_ok": bool}``. Only a transport/HTTP failure on the HTML fallback
    itself propagates (matching `fetch_advisories_page`'s existing contract);
    every JSON-path failure is absorbed here by falling through.
    """
    result = fetch_advisories_json(url, timeout=timeout)
    if result is not None:
        return result
    html = fetch_advisories_page(url, timeout=timeout)
    return parse_advisories_html(html)


class _AdvisorySectionParser(HTMLParser):
    """Splits advisory-listing entries into an 'open' / 'resolved' bucket.

    Tracks the most recent heading to know which section subsequent `<li>`/
    `<tr>` entries belong to, and the first `href` seen inside an entry as that
    entry's link. Entries outside both sections (before the first matching
    heading, or under an unrelated heading) are ignored.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._section: str | None = None  # None | "open" | "resolved"
        self._in_heading = False
        self._heading_text: list[str] = []
        self._entry_depth = 0
        self._entry_text: list[str] = []
        self._entry_href: str | None = None
        self._entry_is_header_row = False
        self.saw_open_heading = False
        self.saw_resolved_heading = False
        self.entries: list[tuple[str, str, str | None]] = []  # (section, text, href)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HEADING_TAGS:
            self._in_heading = True
            self._heading_text = []
        elif tag in _ENTRY_TAGS and self._section is not None:
            self._entry_depth += 1
            if self._entry_depth == 1:
                self._entry_text = []
                self._entry_href = None
                self._entry_is_header_row = False
        elif tag == "th" and self._entry_depth > 0:
            # A <tr> containing a <th> is a table header row, not an advisory.
            self._entry_is_header_row = True
        elif tag == "a" and self._entry_depth > 0 and self._entry_href is None:
            href = dict(attrs).get("href")
            if href:
                self._entry_href = href

    def handle_endtag(self, tag: str) -> None:
        if tag in _HEADING_TAGS and self._in_heading:
            self._in_heading = False
            text = " ".join(self._heading_text).strip()
            if _OPEN_HEADING.search(text):
                self._section = "open"
                self.saw_open_heading = True
            elif _RESOLVED_HEADING.search(text):
                self._section = "resolved"
                self.saw_resolved_heading = True
            # Any other heading just keeps the current section (sub-headings
            # inside a section, e.g. per-component grouping, are common).
        elif tag in _ENTRY_TAGS and self._entry_depth > 0:
            self._entry_depth -= 1
            if self._entry_depth == 0 and self._section is not None:
                text = " ".join(self._entry_text).split()
                text = " ".join(text)
                if text and not self._entry_is_header_row:
                    self.entries.append((self._section, text, self._entry_href))

    def handle_data(self, data: str) -> None:
        if self._in_heading:
            self._heading_text.append(data.strip())
        elif self._entry_depth > 0:
            stripped = data.strip()
            if stripped:
                self._entry_text.append(stripped)


def _split_entry(text: str, href: str | None) -> dict:
    """Best-effort split of one raw entry line into id / component / title."""
    component = None
    match = _COMPONENT_PREFIX.match(text)
    if match:
        component, text = match.group(1).strip(), match.group(2).strip()

    advisory_id = None
    match = _ID_PREFIX.match(text)
    if match:
        advisory_id, text = match.group(1).strip(), match.group(2).strip()

    return {
        "id": advisory_id,
        "component": component,
        "title": text,
        "url": href,
    }


def parse_advisories_html(html: str) -> dict:
    """Extract Open/Resolved CA entries from an advisory document's HTML.

    Returns ``{"open": [...], "resolved": [...], "parsed_ok": bool}``. Each
    entry is ``{"id", "component", "title", "url"}`` (any of which may be
    `None`/absent when not present in the source line). ``parsed_ok`` is False
    when neither expected section heading was found — the page's structure did
    not match what this parser understands, so the caller should treat the
    result as empty rather than trust a partial/misaligned extraction.
    """
    parser = _AdvisorySectionParser()
    try:
        parser.feed(html)
    except Exception as e:  # malformed markup must never propagate as a crash
        log.warning("advisory page failed to parse: %s", e)
        return {"open": [], "resolved": [], "parsed_ok": False}

    if not (parser.saw_open_heading or parser.saw_resolved_heading):
        log.warning(
            "advisory page has neither an 'Open Customer Advisories' nor a "
            "'Resolved Customer Advisories' heading; page structure may have "
            "changed. Treating as no advisories."
        )
        return {"open": [], "resolved": [], "parsed_ok": False}

    open_cas = [_split_entry(t, h) for sec, t, h in parser.entries if sec == "open"]
    resolved_cas = [_split_entry(t, h) for sec, t, h in parser.entries if sec == "resolved"]
    return {"open": open_cas, "resolved": resolved_cas, "parsed_ok": True}
