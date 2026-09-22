"""Tests for HPE Customer Advisory enrichment: extraction, parsing, matching,
rendering, and analyzer-payload wiring.

Live COM/HPE-support calls are never made here — `ComClient` and the HTTP
fetch are monkeypatched/mocked, consistent with the fail-open contract these
tests exist to pin down.
"""

from __future__ import annotations

import pytest

from com_event_core.enrich import advisories as advisories_mod
from com_event_core.enrich import compliance as compliance_mod
from com_event_core.enrich.hpe_advisories import HpeAdvisoriesEnricher, _relevant
from com_event_core.enrich.render import (
    ADVISORY_HEADING,
    advisory_lines,
    advisory_text,
    has_advisories,
)
from com_event_core.normalize import ACTION_CLEAR, ACTION_RAISE, CanonicalEvent


def _event(**overrides) -> CanonicalEvent:
    base = dict(
        event_id="evt-1",
        operation="Updated",
        title="Server prod-db-01 health CRITICAL",
        severity="critical",
        resource_serial="SN123",
        resource_model="ProLiant DL360 Gen10 Plus",
        mgmt_url="https://10.0.0.5",
        time_created="2026-09-01T00:00:00Z",
        source_type="server",
        action=ACTION_RAISE,
        correlation_key="server:SN123:health",
        description="Memory subsystem reported degraded DIMM health.",
        category="hardware-health",
        raw={"id": "srv-1"},
    )
    base.update(overrides)
    return CanonicalEvent(**base)


# --- 1. Extraction from server payloads -----------------------------------

class TestBundleRefResolution:
    def test_prefers_firmware_bundle_uri(self):
        e = _event(raw={"id": "srv-1", "firmwareBundleUri": "/compute-ops-mgmt/v1/firmware-bundles/abc"})
        enricher = HpeAdvisoriesEnricher.__new__(HpeAdvisoriesEnricher)
        assert enricher._resolve_bundle_ref(e) == "/compute-ops-mgmt/v1/firmware-bundles/abc"

    def test_falls_back_to_last_firmware_update(self):
        e = _event(raw={"id": "srv-1", "lastFirmwareUpdate": {"attemptedBaselineUri": "/fw/xyz"}})
        enricher = HpeAdvisoriesEnricher.__new__(HpeAdvisoriesEnricher)
        assert enricher._resolve_bundle_ref(e) == "/fw/xyz"

    def test_falls_back_to_ui_doorway_baseline_when_public_server_has_no_bundle(self, monkeypatch):
        e = _event(raw={"id": "srv-1"})
        enricher = HpeAdvisoriesEnricher.__new__(HpeAdvisoriesEnricher)

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_server(self, server_id, select=None):
                return {}

            def get_ui_doorway_server(self, server_id):
                return {"baselineDerived_": {"id": "baseline-1"}}

        monkeypatch.setattr(
            "com_event_core.enrich.hpe_advisories.ComClient", lambda: Client()
        )
        assert enricher._resolve_bundle_ref(e) == "/v1/firmware-bundles/baseline-1"

    def test_none_when_neither_present_and_no_com_config(self, monkeypatch):
        monkeypatch.delenv("COM_BASE_URL", raising=False)
        monkeypatch.delenv("COM_PAT", raising=False)
        monkeypatch.delenv("COM_PAT_FILE", raising=False)
        e = _event(raw={"id": "srv-1"})
        enricher = HpeAdvisoriesEnricher.__new__(HpeAdvisoriesEnricher)
        assert enricher._resolve_bundle_ref(e) is None


# --- 2. Firmware-bundle client (mocked) -----------------------------------

class TestFetchBundle:
    def test_fetch_extracts_bundle_fields_and_advisories(self, monkeypatch):
        enricher = HpeAdvisoriesEnricher()
        bundle_json = {
            "id": "bundle-1",
            "displayName": "SPP Gen12 2026.07",
            "releaseVersion": "2026.07.00.00",
            "releaseDate": "2026-07-01",
            "bundleGeneration": "gen12",
            "supportUrl": "https://support.hpe.com/x",
            "advisories": "https://support.hpe.com/docs/advisories",
        }

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_firmware_bundle(self, ref):
                return bundle_json

        monkeypatch.setattr(
            "com_event_core.enrich.hpe_advisories.ComClient", lambda: FakeClient()
        )
        monkeypatch.setattr(
            "com_event_core.enrich.hpe_advisories.fetch_and_parse_advisories",
            lambda url, timeout=20.0: {
                "open": [{"id": "CA-1", "title": "Fan fault", "component": None, "url": "u"}],
                "resolved": [], "parsed_ok": True,
            },
        )
        result = enricher._fetch("bundle-1")
        assert result["bundle"]["id"] == "bundle-1"
        assert result["bundle"]["advisoriesUrl"] == bundle_json["advisories"]
        assert result["open"][0]["id"] == "CA-1"
        assert result["resolved"] == []

    def test_fetch_returns_none_on_bundle_lookup_failure(self, monkeypatch):
        enricher = HpeAdvisoriesEnricher()

        class FailingClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_firmware_bundle(self, ref):
                raise RuntimeError("COM GET failed: HTTP 404")

        monkeypatch.setattr(
            "com_event_core.enrich.hpe_advisories.ComClient", lambda: FailingClient()
        )
        assert enricher._fetch("bundle-1") is None

    def test_fetch_handles_missing_advisories_link(self, monkeypatch):
        enricher = HpeAdvisoriesEnricher()

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_firmware_bundle(self, ref):
                return {"id": "bundle-2"}  # no "advisories" key

        monkeypatch.setattr(
            "com_event_core.enrich.hpe_advisories.ComClient", lambda: FakeClient()
        )
        result = enricher._fetch("bundle-2")
        assert result == {"bundle": {
            "id": "bundle-2", "displayName": None, "releaseVersion": None,
            "releaseDate": None, "bundleGeneration": None, "supportUrl": None,
            "advisoriesUrl": None,
        }, "open": [], "resolved": []}

    def test_fetch_handles_page_fetch_failure(self, monkeypatch):
        enricher = HpeAdvisoriesEnricher()

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_firmware_bundle(self, ref):
                return {"id": "bundle-3", "advisories": "https://support.hpe.com/x"}

        monkeypatch.setattr(
            "com_event_core.enrich.hpe_advisories.ComClient", lambda: FakeClient()
        )

        def raise_fetch(url, timeout=20.0):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(
            "com_event_core.enrich.hpe_advisories.fetch_and_parse_advisories", raise_fetch
        )
        result = enricher._fetch("bundle-3")
        assert result["open"] == [] and result["resolved"] == []


# --- 3. Advisory HTML parsing ----------------------------------------------

_FIXTURE_HTML = """
<html><body>
<h2>Open Customer Advisories</h2>
<ul>
  <li><a href="https://support.hpe.com/ca/1">CA00012345: Fan module may report incorrect speed</a></li>
  <li>[Memory] DIMM initialization failure on certain workloads</li>
</ul>
<h2>Resolved Customer Advisories</h2>
<table>
  <tr><th>Advisory</th></tr>
  <tr><td><a href="https://support.hpe.com/ca/2">CA00099999: Power supply firmware may fail to update</a></td></tr>
</table>
</body></html>
"""


class TestParseAdvisoriesHtml:
    def test_separates_open_and_resolved(self):
        result = advisories_mod.parse_advisories_html(_FIXTURE_HTML)
        assert result["parsed_ok"] is True
        assert len(result["open"]) == 2
        assert len(result["resolved"]) == 1

    def test_extracts_stable_fields(self):
        result = advisories_mod.parse_advisories_html(_FIXTURE_HTML)
        first = result["open"][0]
        assert first["id"] == "CA00012345"
        assert "Fan module" in first["title"]
        assert first["url"] == "https://support.hpe.com/ca/1"

        second = result["open"][1]
        assert second["component"] == "Memory"
        assert "DIMM initialization" in second["title"]

        resolved = result["resolved"][0]
        assert resolved["id"] == "CA00099999"
        assert resolved["url"] == "https://support.hpe.com/ca/2"

    def test_no_matching_headings_marks_unparsed(self):
        result = advisories_mod.parse_advisories_html("<html><body><p>nothing here</p></body></html>")
        assert result == {"open": [], "resolved": [], "parsed_ok": False}

    def test_malformed_markup_does_not_raise(self):
        # Deeply unbalanced tags must degrade to an empty result, never throw.
        result = advisories_mod.parse_advisories_html("<html><body><div><div><p>")
        assert result["open"] == [] and result["resolved"] == []


# --- 3b. JSON-asset extraction (primary path) ------------------------------

# Trimmed from a real asset fetched live (2026-09) via
# .../spp/assets/gen10.2025.11.00.00.json.
_FIXTURE_JSON_BODY = {
    "BundleVersion": "2025.11.00.00",
    "Advisories": {
        "OpendCAs": [
            {
                "Description": "Advisory: HPE MegaRAID Storage Administrator (MRSA) Software - "
                               "BSOD if S100i Gen10 is Enabled",
                "FixedSPPVersion": "Future Release",
                "CA": "a00146775en_us",
                "CALink": "https://support.hpe.com/hpesc/public/docDisplay?docId=a00146775en_us",
                "Date": "03/27/2025",
            },
        ],
        "ResolvedCAs": [
            {
                "Description": "Advisory: HPE Network Adapters - Firmware Update For HPE Intel "
                               "Based Adapters May Not Complete",
                "FixedSPPVersion": "2025.11.00.00",
                "CA": "a00094374en_us",
                "CALink": "https://support.hpe.com/hpesc/public/docDisplay?docId=a00094374en_us",
                "Date": "10/31/2025",
            },
        ],
        "KnownLimitations": [],
    },
}

_ADVISORIES_PAGE_URL = (
    "https://support.hpe.com/docs/display/public/a00sppdocen_US/spp/index.aspx"
    "?version=gen10.2025.11.00.00"
)


class TestJsonAssetExtraction:
    def test_derive_json_asset_url(self):
        assert advisories_mod._derive_json_asset_url(_ADVISORIES_PAGE_URL) == (
            "https://support.hpe.com/docs/display/public/a00sppdocen_US/spp/"
            "assets/gen10.2025.11.00.00.json"
        )

    def test_derive_json_asset_url_no_version_param(self):
        assert advisories_mod._derive_json_asset_url(
            "https://support.hpe.com/docs/display/public/a00sppdocen_US/spp/index.aspx"
        ) is None

    def test_derive_json_asset_url_no_scheme(self):
        assert advisories_mod._derive_json_asset_url("not-a-url") is None

    def test_fetch_advisories_json_success(self, monkeypatch):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return _FIXTURE_JSON_BODY

        monkeypatch.setattr(
            advisories_mod.httpx, "get", lambda url, **kw: FakeResponse()
        )
        result = advisories_mod.fetch_advisories_json(_ADVISORIES_PAGE_URL)
        assert result["parsed_ok"] is True
        assert result["open"] == [{
            "id": "a00146775en_us", "component": None,
            "title": _FIXTURE_JSON_BODY["Advisories"]["OpendCAs"][0]["Description"],
            "url": "https://support.hpe.com/hpesc/public/docDisplay?docId=a00146775en_us",
        }]
        assert result["resolved"][0]["id"] == "a00094374en_us"

    def test_fetch_advisories_json_missing_advisories_key_returns_none(self, monkeypatch):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"BundleVersion": "x"}  # no "Advisories" key

        monkeypatch.setattr(
            advisories_mod.httpx, "get", lambda url, **kw: FakeResponse()
        )
        assert advisories_mod.fetch_advisories_json(_ADVISORIES_PAGE_URL) is None

    def test_fetch_advisories_json_transport_error_returns_none(self, monkeypatch):
        def raise_error(url, **kw):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(advisories_mod.httpx, "get", raise_error)
        assert advisories_mod.fetch_advisories_json(_ADVISORIES_PAGE_URL) is None

    def test_fetch_advisories_json_no_derivable_asset_returns_none(self):
        assert advisories_mod.fetch_advisories_json("https://example.com/no-version") is None

    def test_fetch_and_parse_prefers_json_over_html(self, monkeypatch):
        monkeypatch.setattr(
            advisories_mod, "fetch_advisories_json",
            lambda url, timeout=20.0: {"open": [{"id": "CA-1"}], "resolved": [], "parsed_ok": True},
        )

        def fail_if_called(url, timeout=20.0):
            raise AssertionError("HTML fallback must not run when JSON succeeds")

        monkeypatch.setattr(advisories_mod, "fetch_advisories_page", fail_if_called)
        result = advisories_mod.fetch_and_parse_advisories(_ADVISORIES_PAGE_URL)
        assert result["open"] == [{"id": "CA-1"}]

    def test_fetch_and_parse_falls_back_to_html_when_json_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            advisories_mod, "fetch_advisories_json", lambda url, timeout=20.0: None
        )
        monkeypatch.setattr(
            advisories_mod, "fetch_advisories_page", lambda url, timeout=20.0: _FIXTURE_HTML
        )
        result = advisories_mod.fetch_and_parse_advisories(_ADVISORIES_PAGE_URL)
        assert result["parsed_ok"] is True
        assert len(result["open"]) == 2


# --- 4. Enricher fail-open behaviour ---------------------------------------

class TestFailOpen:
    def test_wants_false_for_clear_action(self):
        enricher = HpeAdvisoriesEnricher()
        assert enricher.wants(_event(action=ACTION_CLEAR)) is False

    def test_wants_false_below_severity_threshold(self, monkeypatch):
        monkeypatch.setenv("HPE_ADVISORIES_MIN_SEVERITY", "critical")
        enricher = HpeAdvisoriesEnricher()
        assert enricher.wants(_event(severity="warning")) is False

    def test_wants_false_for_non_server_source(self):
        enricher = HpeAdvisoriesEnricher()
        assert enricher.wants(_event(source_type="alert")) is False

    def test_enrich_skips_quietly_with_no_bundle_ref(self, monkeypatch):
        monkeypatch.delenv("COM_BASE_URL", raising=False)
        monkeypatch.delenv("COM_PAT", raising=False)
        monkeypatch.delenv("COM_PAT_FILE", raising=False)
        enricher = HpeAdvisoriesEnricher()
        e = _event(raw={"id": "srv-1"})
        enricher.enrich(e)  # must not raise
        assert e.advisory_references == []
        assert e.advisory_evidence is None

    def test_enrich_survives_com_401(self, monkeypatch):
        e = _event(raw={"id": "srv-1", "firmwareBundleUri": "/fw/1"})
        enricher = HpeAdvisoriesEnricher()

        class Client401:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_firmware_bundle(self, ref):
                raise RuntimeError("COM GET failed: HTTP 401 (check COM_PAT ...)")

        monkeypatch.setattr(
            "com_event_core.enrich.hpe_advisories.ComClient", lambda: Client401()
        )
        enricher.enrich(e)  # must not raise
        assert e.advisory_references == []


# --- 5. Matching heuristics --------------------------------------------------

class TestRelevantMatching:
    def test_matches_on_shared_keyword(self):
        e = _event(title="Server X memory DIMM fault", description="degraded DIMM detected")
        cas = [{"id": "CA-1", "title": "DIMM initialization failure", "component": "Memory", "url": "u"}]
        matched = _relevant(e, cas, "open")
        assert len(matched) == 1
        assert matched[0]["status"] == "open"

    def test_no_match_with_no_keyword_overlap(self):
        e = _event(title="Server X power supply fault", description="PSU failed")
        cas = [{"id": "CA-2", "title": "Network adapter link flapping", "component": "Networking", "url": "u"}]
        assert _relevant(e, cas, "resolved") == []

    def test_severity_word_alone_is_not_a_match(self):
        # Verified live (2026-09): a "memory DIMM degraded" event with
        # "health WARNING" in its title matched an unrelated VMware vLCM
        # advisory purely because both texts contain "warning" — every event
        # title embeds its own severity word, so it must never count as signal.
        e = _event(
            title="Server LIVE health WARNING",
            description="memory dimm degraded",
            category="hardware-health",
        )
        cas = [{
            "id": "emr_na-a00147657en_us",
            "title": "Advisory: VMware - vLCM Warning Message To Remove Obsolete Component",
            "component": None, "url": "u",
        }]
        assert _relevant(e, cas, "open") == []

    def test_advisory_prefix_alone_is_not_a_match(self):
        e = _event(title="Server X power supply fault", description="PSU failed", category="power")
        cas = [{"id": "CA-3", "title": "Advisory: Notice (Revision) unrelated topic",
                "component": None, "url": "u"}]
        assert _relevant(e, cas, "open") == []


# --- 6. Analyzer payload formation ------------------------------------------

class TestAnalyzerPayloadWiring:
    def test_advisories_included_only_when_present(self, monkeypatch):
        from com_event_core.enrich.ilo_ai import IloAiEnricher

        monkeypatch.setenv("AI_ANALYZER_URL", "http://analyzer")
        enricher = IloAiEnricher()

        captured = {}

        class FakeResponse:
            is_error = False

            def json(self):
                return {"result": {"summary": "ok"}}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["body"] = json
            return FakeResponse()

        monkeypatch.setattr("com_event_core.enrich.ilo_ai.httpx.post", fake_post)

        e = _event()
        enricher._analyze(e, {"resources": {}})
        assert "advisories" not in captured["body"]["input"]

        e.advisory_evidence = {"bundle": {"id": "b1"}, "open": [], "resolved": []}
        enricher._analyze(e, {"resources": {}})
        assert captured["body"]["input"]["advisories"] == e.advisory_evidence


# --- 7. Adapter rendering ----------------------------------------------------

class TestRendering:
    def test_no_advisories_renders_nothing(self):
        e = _event()
        assert has_advisories(e) is False
        assert advisory_lines(e) == []
        assert advisory_text(e) == ""

    def test_open_only(self):
        e = _event(advisory_references=[
            {"status": "open", "id": "CA-1", "title": "Fan fault", "component": None,
             "summary": None, "resolution": None, "url": "https://x/1"},
        ])
        text = advisory_text(e)
        assert text.startswith(ADVISORY_HEADING)
        assert "[OPEN] CA-1: Fan fault" in text
        assert "https://x/1" in text

    def test_resolved_only(self):
        e = _event(advisory_references=[
            {"status": "resolved", "id": "CA-2", "title": "PSU fw issue", "component": None,
             "summary": None, "resolution": None, "url": None},
        ])
        lines = advisory_lines(e)
        assert lines == ["[RESOLVED] CA-2: PSU fw issue"]

    def test_both_sections(self):
        e = _event(advisory_references=[
            {"status": "open", "id": "CA-1", "title": "A", "component": None,
             "summary": None, "resolution": None, "url": None},
            {"status": "resolved", "id": "CA-2", "title": "B", "component": None,
             "summary": None, "resolution": None, "url": None},
        ])
        lines = advisory_lines(e)
        assert lines == ["[OPEN] CA-1: A", "[RESOLVED] CA-2: B"]

    def test_slack_adapter_renders_advisory_block(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/x")
        from com_event_core.adapters.slack import SlackAdapter

        e = _event(advisory_references=[
            {"status": "open", "id": "CA-1", "title": "Fan fault", "component": None,
             "summary": None, "resolution": None, "url": "https://x/1"},
        ])
        message = SlackAdapter()._to_message(e)
        blocks = message["attachments"][0]["blocks"]
        assert any(
            "Fan fault" in str(b) for b in blocks
        ), "advisory block missing from Slack message"

    def test_jira_adapter_renders_advisory_heading(self, monkeypatch):
        monkeypatch.setenv("JIRA_URL", "https://acme.atlassian.net")
        monkeypatch.setenv("JIRA_EMAIL", "a@b.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "tok")
        monkeypatch.setenv("JIRA_PROJECT_KEY", "OPS")
        from com_event_core.adapters.jira import JiraAdapter

        e = _event(advisory_references=[
            {"status": "open", "id": "CA-1", "title": "Fan fault", "component": None,
             "summary": None, "resolution": None, "url": None},
        ])
        doc = JiraAdapter()._adf(e)
        rendered = str(doc)
        assert ADVISORY_HEADING in rendered
        assert "Fan fault" in rendered

    def test_elastic_adapter_includes_advisory_references(self, monkeypatch):
        monkeypatch.setenv("ELASTIC_URL", "https://es.test:9200")
        from com_event_core.adapters.elastic import ElasticAdapter

        e = _event(advisory_references=[
            {"status": "open", "id": "CA-1", "title": "Fan fault", "component": None,
             "summary": None, "resolution": None, "url": None},
        ])
        doc = ElasticAdapter()._to_doc(e)
        assert doc["advisory_references"] == e.advisory_references


# --- 8. Group firmware-compliance resolution --------------------------------

_GROUP_WITH_DEVICE = {
    "id": "grp-1",
    "name": "HVM_group",
    "devices": [{"deviceId": "srv-1", "id": "srv-1"}],
    "groupCompliance": {"firmware": {"status": "Not Compliant"}},
}

_COMPLIANCE_PAGE = {
    "items": [
        {
            "deviceId": "srv-1",
            "bundleId": "bundle-assigned",
            "complianceState": "Not Compliant",
            "score": 56,
            "deviations": [
                {"category": "BIOS", "componentName": "System ROM",
                 "expectedVersion": "v2.60", "installedVersion": "v2.50"},
            ],
        },
    ],
}


class FakeComplianceClient:
    def __init__(self, groups_pages, compliance_page, ui_doorway=None):
        self._groups_pages = groups_pages
        self._compliance_page = compliance_page
        self._ui_doorway = ui_doorway

    def list_groups(self, *, limit=100, offset=0):
        idx = offset // limit if limit else 0
        return self._groups_pages[idx] if idx < len(self._groups_pages) else {"items": []}

    def get_group_compliance(self, group_id, *, limit=100, offset=0):
        return self._compliance_page

    def get_ui_doorway_server(self, device_id):
        if isinstance(self._ui_doorway, Exception):
            raise self._ui_doorway
        return self._ui_doorway or {}


class TestGroupCompliance:
    def test_find_group_for_device_matches(self):
        client = FakeComplianceClient([{"items": [_GROUP_WITH_DEVICE], "total": 1}], _COMPLIANCE_PAGE)
        group = compliance_mod.find_group_for_device(client, "srv-1")
        assert group == _GROUP_WITH_DEVICE

    def test_find_group_for_device_no_match(self):
        other = {"id": "grp-2", "name": "other", "devices": [{"deviceId": "srv-999"}]}
        client = FakeComplianceClient([{"items": [other], "total": 1}], _COMPLIANCE_PAGE)
        assert compliance_mod.find_group_for_device(client, "srv-1") is None

    def test_get_device_compliance_trims_remediation(self):
        client = FakeComplianceClient([], _COMPLIANCE_PAGE)
        result = compliance_mod.get_device_compliance(client, _GROUP_WITH_DEVICE, "srv-1")
        assert result["compliance_state"] == "Not Compliant"
        assert result["score"] == 56
        assert result["assigned_bundle_id"] == "bundle-assigned"
        assert result["group_firmware_status"] == "Not Compliant"
        assert result["deviations"] == [
            {"category": "BIOS", "component": "System ROM",
             "expected_version": "v2.60", "installed_version": "v2.50"},
        ]
        assert "remediation" not in result

    def test_get_device_compliance_no_record_returns_none(self):
        client = FakeComplianceClient([], {"items": []})
        assert compliance_mod.get_device_compliance(client, _GROUP_WITH_DEVICE, "srv-1") is None

    def test_resolve_compliance_none_when_no_group(self):
        client = FakeComplianceClient([{"items": [], "total": 0}], _COMPLIANCE_PAGE)
        assert compliance_mod.resolve_compliance(client, "srv-1") is None

    def test_ui_doorway_compliance_normalizes_server_report(self):
        client = FakeComplianceClient([], _COMPLIANCE_PAGE, {
            "baselineDerived_": {
                "releaseVersion": "2025.01.00.00",
                "displayName": "SPP 2025.01.00.00 (24 Jan 2025)",
                "id": "bundle-baseline",
            },
            "serverFirmwareCompliance_": {
                "score": 75,
                "bundleId": "bundle-baseline",
                "complianceState": "Not Compliant",
                "deviations": [{
                    "componentName": "System ROM",
                    "recommendedVersion": "v3.34",
                    "installedVersion": "v3.66",
                }],
            },
        })
        result = compliance_mod.get_ui_doorway_compliance(client, "srv-1")
        assert result["source"] == "server_ui_doorway"
        assert result["compliance_state"] == "Not Compliant"
        assert result["score"] == 75
        assert result["baseline_release_version"] == "2025.01.00.00"
        assert result["deviations"] == [{
            "category": None,
            "component": "System ROM",
            "expected_version": "v3.34",
            "installed_version": "v3.66",
        }]

    def test_ui_doorway_compliance_empty_report_returns_none(self):
        client = FakeComplianceClient([], _COMPLIANCE_PAGE, {})
        assert compliance_mod.get_ui_doorway_compliance(client, "srv-1") is None

    def test_enrich_attaches_compliance_evidence(self, monkeypatch):
        e = _event(raw={"id": "srv-1", "firmwareBundleUri": "/fw/1"})
        enricher = HpeAdvisoriesEnricher()

        class CombinedClient(FakeComplianceClient):
            """One client used for both the bundle fetch AND the group/compliance
            calls, since `_resolve_compliance` constructs `ComClient()` from the
            same symbol `hpe_advisories` uses for the bundle fetch."""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_firmware_bundle(self, ref):
                return {"id": "bundle-1", "advisories": None}

        monkeypatch.setattr(
            "com_event_core.enrich.hpe_advisories.ComClient",
            lambda: CombinedClient([{"items": [_GROUP_WITH_DEVICE], "total": 1}], _COMPLIANCE_PAGE),
        )
        enricher.enrich(e)
        assert e.advisory_evidence["compliance"]["compliance_state"] == "Not Compliant"
        assert e.advisory_evidence["compliance"]["assigned_bundle_id"] == "bundle-assigned"

    def test_enrich_fails_open_when_compliance_lookup_errors(self, monkeypatch):
        e = _event(raw={"id": "srv-1", "firmwareBundleUri": "/fw/1"})
        enricher = HpeAdvisoriesEnricher()

        class FlakyComplianceClient:
            """Serves the bundle fine but explodes on any group/compliance call."""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_firmware_bundle(self, ref):
                return {"id": "bundle-1", "advisories": None}

            def list_groups(self, *, limit=100, offset=0):
                raise RuntimeError("COM groups lookup failed: HTTP 500")

        monkeypatch.setattr(
            "com_event_core.enrich.hpe_advisories.ComClient", lambda: FlakyComplianceClient()
        )

        enricher.enrich(e)  # must not raise
        assert e.advisory_evidence["compliance"] is None
        assert e.advisory_evidence["bundle"]["id"] == "bundle-1"

    def test_enrich_uses_ui_doorway_when_device_has_no_group(self, monkeypatch):
        e = _event(raw={"id": "srv-1", "firmwareBundleUri": "/fw/1"})
        enricher = HpeAdvisoriesEnricher()

        class UiFallbackClient(FakeComplianceClient):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_firmware_bundle(self, ref):
                return {"id": "bundle-1", "advisories": None}

        ui_report = {
            "baselineDerived_": {"id": "bundle-baseline", "releaseVersion": "2025.01.00.00"},
            "serverFirmwareCompliance_": {
                "score": 75,
                "bundleId": "bundle-baseline",
                "complianceState": "Not Compliant",
                "deviations": [],
            },
        }
        monkeypatch.setattr(
            "com_event_core.enrich.hpe_advisories.ComClient",
            lambda: UiFallbackClient([{"items": [], "total": 0}], _COMPLIANCE_PAGE, ui_report),
        )
        enricher.enrich(e)
        assert e.advisory_evidence["compliance"]["source"] == "server_ui_doorway"
        assert e.advisory_evidence["compliance"]["score"] == 75

    def test_ui_doorway_failure_is_identifiable_and_fail_open(self, monkeypatch, caplog):
        e = _event(raw={"id": "srv-1", "firmwareBundleUri": "/fw/1"})
        enricher = HpeAdvisoriesEnricher()

        class BrokenUiClient(FakeComplianceClient):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_firmware_bundle(self, ref):
                return {"id": "bundle-1", "advisories": None}

            def get_ui_doorway_server(self, device_id):
                raise RuntimeError("COM UI-doorway compliance fallback failed: HTTP 404")

        monkeypatch.setattr(
            "com_event_core.enrich.hpe_advisories.ComClient",
            lambda: BrokenUiClient([{"items": [], "total": 0}], _COMPLIANCE_PAGE),
        )
        enricher.enrich(e)
        assert e.advisory_evidence["compliance"] is None
        assert "UI-doorway compliance lookup failed" in caplog.text
