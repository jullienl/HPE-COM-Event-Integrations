"""HPE Customer Advisory enrichment — attach firmware-bundle advisory context.

For a server *problem* event, this stage:

1. resolves the server's firmware bundle (preferring the id already on the
   webhook payload; falling back to a COM API lookup only if needed),
2. fetches that bundle's ``advisories`` document and parses its Open/Resolved
   Customer Advisory sections,
3. writes the full set onto ``event.advisory_evidence`` (for the analyzer) and
   a conservatively-matched subset onto ``event.advisory_references`` (for
   ticket rendering).

Where this runs
----------------
Same as `ilo_ai`: on-prem shim/bridge only, since it needs outbound reachability
to both the COM API and HPE's public support site. It opens no inbound port.

The three things that keep it safe
-----------------------------------
* **Gated.** Only ``raise`` events on server-sourced problems, at or above
  ``HPE_ADVISORIES_MIN_SEVERITY``, exactly like `ilo_ai`'s severity gate.
* **Cached** by resolved bundle reference (not by event id) with a long default
  TTL (`HPE_ADVISORIES_CACHE_TTL_SECONDS`, default 24h) — many servers in a
  fleet share one firmware bundle, and the advisory document changes rarely, so
  there is no reason to re-fetch it per event.
* **Fail-open.** A missing bundle reference, a COM API error, or an
  unparseable advisory page all end in "deliver the event unenriched" (the
  fail-open runner in `com_event_core.enrich` catches any exception this raises
  too, but every expected failure mode here is handled locally with a plain
  skip so the log reads clearly).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

from .advisories import fetch_and_parse_advisories
from .base import Enricher
from .compliance import get_ui_doorway_compliance, resolve_compliance
from ..com_client import ComClient
from ..normalize import ACTION_RAISE, CanonicalEvent

log = logging.getLogger("com_event_core.enrich.hpe_advisories")

_SEVERITY_RANK = {
    "normal": 0,
    "warning": 1,
    "minor": 2,
    "major": 3,
    "critical": 4,
}

# Words too generic to count as a match signal on their own. Includes the
# canonical severity words: normalize.py embeds them into nearly every event
# title (e.g. "Server X health WARNING"), and HPE CA titles routinely contain
# the plain English words "warning"/"critical" ("... Warning Message ...") —
# without excluding them, an event and an unrelated CA "match" purely on
# sharing their own severity word. Verified live (2026-09): a "memory DIMM
# degraded" event matched an unrelated VMware vLCM advisory for exactly this
# reason before these were added. Also excludes the boilerplate CA
# title prefixes ("Advisory:", "Notice:", "(Revision)") that appear on nearly
# every CA regardless of its actual content.
_STOPWORDS = {
    "this", "that", "with", "from", "into", "have", "has", "had", "were",
    "will", "when", "returned", "server", "healthy", "status", "hardware",
    "returned", "health", "summary", "unknown", "operation", "updated",
    "normal", "warning", "minor", "major", "critical",
    "advisory", "advisories", "notice", "revision", "message",
}
_WORD = re.compile(r"[a-z0-9]{4,}")


class _TtlCache:
    """Tiny TTL cache keyed by a resolved bundle reference."""

    def __init__(self, ttl: int, max_entries: int = 64) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, dict]] = {}

    def get(self, key: str) -> dict | None:
        if self._ttl <= 0:
            return None
        with self._lock:
            hit = self._data.get(key)
            if not hit:
                return None
            stored_at, value = hit
            if time.time() - stored_at > self._ttl:
                self._data.pop(key, None)
                return None
            return value

    def put(self, key: str, value: dict) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            if len(self._data) >= self._max:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                self._data.pop(oldest, None)
            self._data[key] = (time.time(), value)


def _keywords(*texts: str | None) -> set[str]:
    words: set[str] = set()
    for text in texts:
        if not text:
            continue
        words |= {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS}
    return words


def _relevant(event: CanonicalEvent, cas: list[dict], status: str) -> list[dict]:
    """Conservatively match CAs whose title/component shares a keyword with the
    event. A misleading CA in a ticket is worse than none, so this only ever
    narrows — it never invents a match with no textual overlap.
    """
    event_words = _keywords(event.title, event.description, event.category)
    matched: list[dict] = []
    for ca in cas:
        ca_words = _keywords(ca.get("title"), ca.get("component"))
        if event_words & ca_words:
            matched.append({
                "status": status,
                "id": ca.get("id"),
                "title": ca.get("title"),
                "component": ca.get("component"),
                "summary": None,
                "resolution": None,
                "url": ca.get("url"),
            })
    return matched


class HpeAdvisoriesEnricher(Enricher):
    """Attaches HPE Customer Advisory context from the server's firmware bundle."""

    name = "hpe_advisories"
    # Runs before evidence-consuming enrichers (e.g. ilo_ai reads
    # event.advisory_evidence into its analyzer payload) — see Enricher.priority.
    priority = 10

    def __init__(self) -> None:
        threshold = os.environ.get("HPE_ADVISORIES_MIN_SEVERITY", "warning").strip().lower()
        if threshold not in _SEVERITY_RANK:
            supported = ", ".join(_SEVERITY_RANK)
            raise ValueError(
                f"HPE_ADVISORIES_MIN_SEVERITY must be one of: {supported}. "
                f"Got '{threshold}'."
            )
        self._min_rank = _SEVERITY_RANK[threshold]
        self._page_timeout = float(os.environ.get("HPE_ADVISORIES_TIMEOUT", "20"))
        ttl = int(os.environ.get("HPE_ADVISORIES_CACHE_TTL_SECONDS", "86400"))
        self._cache = _TtlCache(ttl=ttl)
        # Separate cache: compliance is per-DEVICE, bundle/advisories is
        # per-BUNDLE — two devices can share a bundle but never share a
        # compliance record.
        self._compliance_cache = _TtlCache(ttl=ttl)
        self._check_compliance = os.environ.get(
            "HPE_ADVISORIES_CHECK_COMPLIANCE", "true"
        ).strip().lower() not in ("0", "false", "no")

    # --- gating ----------------------------------------------------------
    def wants(self, event: CanonicalEvent) -> bool:
        """Only server-sourced problems can carry firmware-bundle info at all."""
        if event.action != ACTION_RAISE:
            return False
        if _SEVERITY_RANK.get(event.severity, 0) < self._min_rank:
            return False
        if event.source_type != "server":
            log.info(
                "event %s is not server-sourced (source_type=%s); skipping "
                "advisory lookup", event.event_id, event.source_type,
            )
            return False
        return True

    # --- enrichment ------------------------------------------------------
    def enrich(self, event: CanonicalEvent) -> None:
        bundle_ref = self._resolve_bundle_ref(event)
        if not bundle_ref:
            log.info(
                "event %s: no firmware bundle reference available; skipping "
                "advisory lookup", event.event_id,
            )
            return

        cached = self._cache.get(bundle_ref)
        if cached is not None:
            log.info("event %s reusing cached advisories for bundle %s",
                     event.event_id, bundle_ref)
            result = cached
        else:
            result = self._fetch(bundle_ref)
            if result is None:
                return  # already logged
            self._cache.put(bundle_ref, result)

        compliance = self._resolve_compliance(event) if self._check_compliance else None
        self._apply(event, result, compliance)

    def _resolve_compliance(self, event: CanonicalEvent) -> dict | None:
        """Group-baseline compliance for this event's device, cached per device.

        A device's own `firmwareBundleUri` proves a direct update was *applied*
        (`lastFirmwareUpdate.status`), but says nothing about a *group*
        baseline assigned separately — this can be materially different from
        (and less current than) what the device actually has installed. Failure
        here (no COM access, device not in a group, lookup error) is a normal
        skip: the analyzer simply reasons without a compliance signal.
        """
        device_id = (event.raw or {}).get("id") or event.event_id
        if not device_id:
            return None

        cached = self._compliance_cache.get(device_id)
        if cached is not None:
            return cached

        try:
            with ComClient() as client:
                compliance = resolve_compliance(client, device_id)
                if compliance is None:
                    log.info(
                        "event %s: no group compliance record for %s; trying "
                        "server-level UI-doorway compliance fallback",
                        event.event_id, device_id,
                    )
                    compliance = get_ui_doorway_compliance(client, device_id)
        except Exception as e:
            boundary = "UI-doorway" if "UI-doorway" in str(e) else "COM group"
            log.warning(
                "event %s: %s compliance lookup failed (%s); continuing "
                "without a compliance signal", event.event_id, boundary, e,
            )
            return None

        if compliance is not None:
            self._compliance_cache.put(device_id, compliance)
        return compliance

    def _resolve_bundle_ref(self, event: CanonicalEvent) -> str | None:
        """Prefer the payload's own fields; fall back to a COM API lookup.

        Order: `firmwareBundleUri` on the raw server payload, then
        `lastFirmwareUpdate.attemptedBaselineUri`, then a narrow
        `GET /servers/{id}` lookup — only attempted when COM_BASE_URL/COM_PAT
        are configured, so a deployment without COM API access simply never
        takes this path (a normal skip, not an error).
        """
        raw = event.raw or {}
        ref = raw.get("firmwareBundleUri")
        if ref:
            return ref

        last_update = raw.get("lastFirmwareUpdate") or {}
        ref = last_update.get("attemptedBaselineUri")
        if ref:
            return ref

        server_id = raw.get("id") or event.event_id
        if not server_id:
            return None
        try:
            with ComClient() as client:
                server = client.get_server(
                    server_id,
                    select="firmwareBundleUri,lastFirmwareUpdate,firmwareInventory,"
                    "hardware,serverGeneration",
                )
        except Exception as e:
            log.info(
                "event %s: COM lookup for firmware bundle failed (%s); "
                "skipping advisory lookup", event.event_id, e,
            )
            return None
        ref = server.get("firmwareBundleUri") or (
            (server.get("lastFirmwareUpdate") or {}).get("attemptedBaselineUri")
        )
        if ref:
            return ref

        # Some devices expose no bundle reference on the public server shape.
        # The GreenLake UI's server-level report still identifies the derived
        # baseline; use that bundle id to continue to the normal COM bundle
        # endpoint and its advisories URL.
        try:
            with ComClient() as client:
                ui_report = client.get_ui_doorway_server(server_id)
            baseline_id = ((ui_report.get("baselineDerived_") or {}).get("id"))
            if baseline_id:
                log.info(
                    "event %s: using UI-doorway baseline %s as firmware bundle "
                    "fallback", event.event_id, baseline_id,
                )
                return f"/v1/firmware-bundles/{baseline_id}"
        except Exception as e:
            log.warning(
                "event %s: UI-doorway firmware baseline fallback failed (%s); "
                "skipping advisory lookup", event.event_id, e,
            )
        return None

    def _fetch(self, bundle_ref: str) -> dict | None:
        """Fetch the bundle + its advisories page. Returns None on any failure."""
        try:
            with ComClient() as client:
                bundle = client.get_firmware_bundle(bundle_ref)
        except Exception as e:
            log.warning("firmware bundle %s lookup failed: %s", bundle_ref, e)
            return None

        advisories_url = bundle.get("advisories")
        bundle_info = {
            "id": bundle.get("id"),
            "displayName": bundle.get("displayName"),
            "releaseVersion": bundle.get("releaseVersion"),
            "releaseDate": bundle.get("releaseDate"),
            "bundleGeneration": bundle.get("bundleGeneration"),
            "supportUrl": bundle.get("supportUrl"),
            "advisoriesUrl": advisories_url,
        }
        if not advisories_url:
            log.info("firmware bundle %s has no advisories link", bundle_ref)
            return {"bundle": bundle_info, "open": [], "resolved": []}

        try:
            parsed = fetch_and_parse_advisories(advisories_url, timeout=self._page_timeout)
        except Exception as e:
            log.warning("advisory page %s fetch failed: %s", advisories_url, e)
            return {"bundle": bundle_info, "open": [], "resolved": []}

        return {
            "bundle": bundle_info,
            "open": parsed["open"],
            "resolved": parsed["resolved"],
        }

    @staticmethod
    def _apply(event: CanonicalEvent, result: dict, compliance: dict | None) -> None:
        event.advisory_evidence = {
            "bundle": result["bundle"],
            "open": result["open"],
            "resolved": result["resolved"],
            # None when the device isn't in a group, has no compliance record,
            # or COM access isn't configured \u2014 the analyzer must treat that as
            # "unknown", never as "compliant".
            "compliance": compliance,
        }
        matched = _relevant(event, result["open"], "open") + _relevant(
            event, result["resolved"], "resolved"
        )
        event.advisory_references = matched
