"""iLO + AI enrichment — attach root-cause analysis to a COM event.

For a server event that represents a *problem*, this stage:

1. fetches a bounded Redfish evidence bundle from the server's own iLO (the
   address already travels on the event as ``mgmt_url``, so there is no CMDB
   lookup),
2. POSTs event + evidence to an analyzer service,
3. writes the structured result onto the event's ``analysis_*`` fields.

Every adapter then renders the enriched event, so the analysis lands *inside*
the ticket/message at creation rather than arriving separately.

Where this runs
---------------
On-prem only (shim or bridge) — the cloud relay has no route to a BMC. It opens
an **additional outbound** connection from the on-prem component to the
management network; it does not open any inbound port, so the project's
outbound-only property is preserved.

The four things that keep it safe
---------------------------------
* **Gated.** Only ``raise`` actions, only at or above ``AI_MIN_SEVERITY``, only
  when ``mgmt_url`` is set or can be resolved. Server snapshots emit a
  ``clear`` for every healthy condition on *every* delivery; analysing those
  would be pure cost. Alert-sourced events never carry ``mgmt_url`` directly
  (verified against the live COM alerts API, 2026-09), so a raise alert
  resolves it via a COM API lookup of ``device.resourceUri``/``device.id``
  first; this is a normal skip, not an error, when COM API access isn't
  configured or the lookup fails.
* **Cached** by ``correlation_key``. A `PartialDeliveryError` retries the whole
  payload, so without a cache the analysis is re-run — and re-paid for — on
  every retry of a multi-target fan-out. The resolved alert ``mgmt_url`` is
  also cached, separately, per device id, since a BMC address effectively
  never changes.
* **Budgeted.** Hourly/daily caps trip a circuit breaker that skips analysis
  rather than spending without limit. The breaker's state is logged loudly on
  trip and on reset, so "tickets stopped carrying analysis" has a visible cause.
* **Fail-open.** Every failure path here ends in "deliver the event unenriched"
  (enforced by the runner in `com_event_core.enrich`).
"""

from __future__ import annotations

import logging
import os
import threading
import time

import httpx

from .base import Enricher
from .redfish import collect_evidence
from ..com_client import ComClient
from ..normalize import ACTION_RAISE, CanonicalEvent
from ..secrets import get_secret

log = logging.getLogger("com-event-core.enrich.ilo_ai")

# Canonical severity scale, weakest first. Used to compare against
# AI_MIN_SEVERITY; anything unrecognised sorts at the bottom.
_SEVERITY_RANK = {
    "normal": 0,
    "warning": 1,
    "minor": 2,
    "major": 3,
    "critical": 4,
}


class _Budget:
    """Hourly/daily analysis caps with a circuit breaker.

    On breach the breaker trips open and analysis is skipped until the window
    rolls over — the same fail-open path as an analyzer outage, so it introduces
    no new failure mode. Trips and resets are logged at WARNING so an operator
    can tell "AI disabled: budget exhausted" from "the analyzer is broken".
    """

    def __init__(self, per_hour: int, per_day: int) -> None:
        self._per_hour = per_hour
        self._per_day = per_day
        self._lock = threading.Lock()
        self._hour_start = 0.0
        self._day_start = 0.0
        self._hour_count = 0
        self._day_count = 0
        self._tripped = False

    def take(self) -> bool:
        """Consume one unit. False when the budget is exhausted."""
        now = time.time()
        with self._lock:
            if now - self._hour_start >= 3600:
                self._hour_start, self._hour_count = now, 0
            if now - self._day_start >= 86400:
                self._day_start, self._day_count = now, 0

            over = self._hour_count >= self._per_hour or self._day_count >= self._per_day
            if over:
                if not self._tripped:
                    self._tripped = True
                    log.warning(
                        "AI analysis budget exhausted (%s/hour, %s/day); skipping "
                        "analysis until the window resets — events are still "
                        "delivered, just unenriched",
                        self._per_hour, self._per_day,
                    )
                return False

            if self._tripped:
                self._tripped = False
                log.warning("AI analysis budget window reset; analysis re-enabled")

            self._hour_count += 1
            self._day_count += 1
            return True


class _Cache:
    """Tiny TTL cache of analyses, keyed by correlation_key."""

    def __init__(self, ttl: int, max_entries: int = 512) -> None:
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


def _as_actions(value: object) -> list[str]:
    """Coerce the analyzer's recommended actions into a list of strings.

    Models return this as a list of strings *or* a list of step objects, and
    occasionally as one newline-separated string. Rendering adapters want plain
    lines, so normalise here rather than in four adapters.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        actions: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = str(item.get("action") or item.get("step") or item).strip()
            else:
                text = str(item).strip()
            if text:
                actions.append(text)
        return actions
    return [str(value)]


def _as_confidence(value: object) -> float | None:
    """Coerce confidence to 0.0-1.0, tolerating '0.8', '80%' and 'high'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().lower().rstrip("%")
        worded = {"high": 0.9, "medium": 0.6, "moderate": 0.6, "low": 0.3}
        if text in worded:
            return worded[text]
        try:
            number = float(text)
        except ValueError:
            return None
        if number > 1:  # given as a percentage
            number /= 100.0
    return max(0.0, min(1.0, number))


class IloAiEnricher(Enricher):
    """Enriches server problem events with iLO-evidence-based AI analysis."""

    name = "ilo_ai"
    # Must run after any enricher that attaches evidence this one consumes
    # (e.g. hpe_advisories' event.advisory_evidence) — see Enricher.priority.
    priority = 50

    def __init__(self) -> None:
        # Fail fast at startup on missing config rather than on the first event.
        url = os.environ.get("AI_ANALYZER_URL")
        if not url:
            raise ValueError(
                "ENRICHERS=ilo_ai requires AI_ANALYZER_URL — the base URL of the "
                "analyzer service (e.g. http://ai-gateway). The analyzer is "
                "a separate on-prem container so the model can be hosted or "
                "self-hosted without changing the shim or the bridge."
            )
        self._url = url.rstrip("/")
        self._agent = os.environ.get("AI_AGENT", "com-rca")
        self._tenant = os.environ.get("AI_TENANT") or None
        self._timeout = float(os.environ.get("AI_TIMEOUT", "90"))
        self._token = get_secret("AI_ANALYZER_TOKEN", required=False)

        threshold = os.environ.get("AI_MIN_SEVERITY", "warning").strip().lower()
        if threshold not in _SEVERITY_RANK:
            supported = ", ".join(_SEVERITY_RANK)
            raise ValueError(
                f"AI_MIN_SEVERITY must be one of: {supported}. Got '{threshold}'."
            )
        self._min_rank = _SEVERITY_RANK[threshold]

        self._budget = _Budget(
            per_hour=int(os.environ.get("AI_MAX_ANALYSES_PER_HOUR", "60")),
            per_day=int(os.environ.get("AI_MAX_ANALYSES_PER_DAY", "500")),
        )
        self._cache = _Cache(ttl=int(os.environ.get("AI_CACHE_TTL_SECONDS", "3600")))
        # Separate cache: an analysis is per-PROBLEM (correlation_key), a
        # resolved BMC address is per-DEVICE, and effectively never changes.
        self._mgmt_url_cache = _Cache(
            ttl=int(os.environ.get("ILO_ALERT_MGMT_URL_CACHE_TTL_SECONDS", "86400"))
        )
        # Set once the first alert hits an unconfigured COM API, so that
        # misconfiguration is logged loudly but only once, not once per alert.
        self._com_not_configured_warned = False

    # --- gating ----------------------------------------------------------
    def wants(self, event: CanonicalEvent) -> bool:
        """Only analyse real problems on servers we can actually reach."""
        if event.action != ACTION_RAISE:
            return False

        if _SEVERITY_RANK.get(event.severity, 0) < self._min_rank:
            return False

        if not event.mgmt_url and event.source_type == "alert":
            event.mgmt_url = self._resolve_alert_mgmt_url(event)

        if not event.mgmt_url:
            # Not an error, and not always a config mistake: an alert-sourced
            # event only reaches here when it has no mgmt_url of its own AND
            # the COM API resolution above wasn't configured or didn't find one.
            log.info(
                "no mgmt_url on event %s (source_type=%s); skipping AI analysis",
                event.event_id, event.source_type,
            )
            return False

        return True

    def _resolve_alert_mgmt_url(self, event: CanonicalEvent) -> str | None:
        """Resolve mgmt_url for an alert-sourced raise via the COM API.

        An alert payload never carries a BMC address directly, only
        ``device.resourceUri`` (preferred) or ``device.id``, a reference to
        the server resource whose ``hardware.bmc.ip`` we need. Cached per
        device id, separately from the analysis cache above, since a resolved
        address doesn't expire the way an analysis does. Fails open: a
        missing device id, unconfigured COM API access, or a lookup error all
        just mean "no mgmt_url", the same as an alert always producing today.
        """
        device = (event.raw or {}).get("device")
        device = device if isinstance(device, dict) else {}
        device_id = device.get("id")
        if not device_id:
            return None

        cached = self._mgmt_url_cache.get(device_id)
        if cached is not None:
            return cached.get("mgmt_url")

        try:
            client = ComClient()
        except RuntimeError as e:
            # ComClient() raises RuntimeError only for missing config
            # (COM_BASE_URL / credentials) — never for a live API failure,
            # which only happens once the client is actually used below.
            # Distinct from a transient lookup failure, this repeats for
            # every alert until fixed, so warn loudly, but only once.
            if not self._com_not_configured_warned:
                self._com_not_configured_warned = True
                log.warning(
                    "alert-sourced AI analysis needs COM_BASE_URL + COM "
                    "credentials to resolve mgmt_url (%s); alerts will be "
                    "delivered unenriched until this is configured (logged once)",
                    e,
                )
            return None

        try:
            with client:
                resource_uri = device.get("resourceUri")
                server = (
                    client.get(resource_uri, params={"select": "hardware"})
                    if resource_uri
                    else client.get_server(device_id, select="hardware")
                )
        except Exception as e:
            log.info(
                "event %s: COM lookup for alert device %s failed (%s); "
                "skipping AI analysis", event.event_id, device_id, e,
            )
            return None

        ip = ((server.get("hardware") or {}).get("bmc") or {}).get("ip")
        mgmt_url = f"https://{ip}" if ip else None
        self._mgmt_url_cache.put(device_id, {"mgmt_url": mgmt_url})
        return mgmt_url

    # --- enrichment ------------------------------------------------------
    def enrich(self, event: CanonicalEvent) -> None:
        cached = self._cache.get(event.correlation_key)
        if cached is not None:
            log.info("event %s reusing cached analysis for %s",
                     event.event_id, event.correlation_key)
            self._apply(event, cached)
            return

        if not self._budget.take():
            return  # breaker open; already logged

        assert event.mgmt_url  # guaranteed by wants()
        started = time.monotonic()
        evidence = collect_evidence(event.mgmt_url)
        result = self._analyze(event, evidence)

        self._cache.put(event.correlation_key, result)
        self._apply(event, result)
        log.info("event %s analysed in %.1fs (confidence=%s)",
                 event.event_id, time.monotonic() - started,
                 event.analysis_confidence)

    def _analyze(self, event: CanonicalEvent, evidence: dict) -> dict:
        """POST event + evidence to the analyzer and return its structured result."""
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        body = {
            "input": {
                "event": {
                    "event_id": event.event_id,
                    "title": event.title,
                    "severity": event.severity,
                    "description": event.description,
                    "category": event.category,
                    "resource_serial": event.resource_serial,
                    "resource_model": event.resource_model,
                    "resource_name": event.resource_name,
                    "time_created": event.time_created,
                },
                "redfish": evidence,
            },
            # Thread every analysis of one problem into the same conversation.
            "session_id": event.correlation_key or event.event_id,
        }
        # Only present when ENRICHERS also runs hpe_advisories (order matters:
        # it must come before ilo_ai so this field is already set). Absent
        # otherwise, so the analyzer payload is unchanged for every existing
        # deployment.
        if event.advisory_evidence:
            body["input"]["advisories"] = event.advisory_evidence
        if self._tenant:
            body["tenant"] = self._tenant

        r = httpx.post(
            f"{self._url}/agent/{self._agent}",
            json=body,
            headers=headers,
            timeout=self._timeout,
        )
        if r.is_error:
            # Keep the analyzer's explanation — "HTTP 502" alone doesn't say
            # whether the model, the credential or the payload was the problem.
            raise RuntimeError(
                f"analyzer {self._agent} failed: HTTP {r.status_code} - {r.text[:500]}"
            )

        result = r.json().get("result")
        if isinstance(result, str):
            # The model answered in prose rather than JSON — still useful.
            return {"summary": result}
        if not isinstance(result, dict):
            raise RuntimeError(f"analyzer returned an unusable result: {type(result)}")
        return result

    @staticmethod
    def _apply(event: CanonicalEvent, result: dict) -> None:
        """Copy the analyzer's fields onto the event."""
        event.analysis_summary = result.get("summary") or None
        event.analysis_root_cause = result.get("likely_root_cause") or None
        event.analysis_confidence = _as_confidence(result.get("confidence"))
        event.analysis_actions = _as_actions(result.get("recommended_actions"))
