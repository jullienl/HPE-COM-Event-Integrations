"""Tests for `IloAiEnricher`'s alert mgmt_url resolution.

An alert-sourced event never carries mgmt_url directly (verified against a
real COM alerts API payload, 2026-09); `wants()` must resolve it via a COM API
lookup of device.resourceUri/device.id, cache it per device, and fail open
when COM API access isn't configured or the lookup errors.
"""

from __future__ import annotations

import pytest

from com_event_core.com_client import ComApiError
from com_event_core.enrich.ilo_ai import IloAiEnricher
from com_event_core.normalize import ACTION_CLEAR, ACTION_RAISE, CanonicalEvent

# A real GET /v1/alerts item (device id/type/resourceUri only; irrelevant
# fields trimmed).
_ALERT_RAW = {
    "id": "0058fbc0-0a6c-44b0-8b93-631cf603f769",
    "device": {
        "id": "P59868-B21+SGH308YRGP",
        "type": "compute-ops-mgmt/server",
        "resourceUri": "/compute-ops-mgmt/v1/servers/P59868-B21+SGH308YRGP",
    },
    "type": "compute-ops-mgmt/alert",
    "severity": "Critical",
    "cleared": False,
}


def _alert_event(**overrides) -> CanonicalEvent:
    base = dict(
        event_id="0058fbc0-0a6c-44b0-8b93-631cf603f769",
        operation="Created",
        title="SNMPv1AtRisk",
        severity="critical",
        resource_serial="SGH308YRGP",
        resource_model=None,
        mgmt_url=None,
        time_created="2026-06-23T07:34:54+00:00",
        source_type="alert",
        action=ACTION_RAISE,
        correlation_key="alert:0058fbc0-0a6c-44b0-8b93-631cf603f769",
        raw=_ALERT_RAW,
    )
    base.update(overrides)
    return CanonicalEvent(**base)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv("AI_ANALYZER_URL", "http://analyzer.test")
    for name in ("COM_BASE_URL", "COM_PAT", "COM_CLIENT_ID", "COM_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)


class TestAlertMgmtUrlResolution:
    def test_resolves_mgmt_url_via_device_resource_uri(self, monkeypatch):
        monkeypatch.setenv("COM_BASE_URL", "https://eu-central.api.greenlake.hpe.com")
        monkeypatch.setenv("COM_PAT", "token")

        calls = []

        def fake_get(self, path_or_url, params=None):
            calls.append((path_or_url, params))
            return {"hardware": {"bmc": {"ip": "10.0.0.5"}}}

        monkeypatch.setattr("com_event_core.com_client.ComClient.get", fake_get)

        enricher = IloAiEnricher()
        event = _alert_event()
        assert enricher.wants(event) is True
        assert event.mgmt_url == "https://10.0.0.5"
        assert calls[0][0] == "/compute-ops-mgmt/v1/servers/P59868-B21+SGH308YRGP"

    def test_second_alert_for_same_device_hits_cache(self, monkeypatch):
        monkeypatch.setenv("COM_BASE_URL", "https://eu-central.api.greenlake.hpe.com")
        monkeypatch.setenv("COM_PAT", "token")

        call_count = {"n": 0}

        def fake_get(self, path_or_url, params=None):
            call_count["n"] += 1
            return {"hardware": {"bmc": {"ip": "10.0.0.5"}}}

        monkeypatch.setattr("com_event_core.com_client.ComClient.get", fake_get)

        enricher = IloAiEnricher()
        assert enricher.wants(_alert_event()) is True
        assert enricher.wants(_alert_event()) is True
        assert call_count["n"] == 1

    def test_clear_is_never_resolved(self, monkeypatch):
        monkeypatch.setenv("COM_BASE_URL", "https://eu-central.api.greenlake.hpe.com")
        monkeypatch.setenv("COM_PAT", "token")

        def fake_get(self, path_or_url, params=None):
            raise AssertionError("must not call COM for a clear")

        monkeypatch.setattr("com_event_core.com_client.ComClient.get", fake_get)

        enricher = IloAiEnricher()
        event = _alert_event(action=ACTION_CLEAR)
        assert enricher.wants(event) is False
        assert event.mgmt_url is None

    def test_fails_open_when_com_api_not_configured(self):
        # No COM_BASE_URL/credentials set (see _clean_env).
        enricher = IloAiEnricher()
        event = _alert_event()
        assert enricher.wants(event) is False
        assert event.mgmt_url is None

    def test_warns_once_when_com_api_not_configured(self, caplog):
        # No COM_BASE_URL/credentials set (see _clean_env).
        enricher = IloAiEnricher()
        with caplog.at_level("WARNING", logger="com-event-core.enrich.ilo_ai"):
            enricher.wants(_alert_event())
            enricher.wants(_alert_event(event_id="another-alert"))
        warnings = [r for r in caplog.records if "COM_BASE_URL" in r.message]
        assert len(warnings) == 1

    def test_com_api_error_during_call_is_not_misclassified_as_unconfigured(
        self, monkeypatch, caplog
    ):
        monkeypatch.setenv("COM_BASE_URL", "https://eu-central.api.greenlake.hpe.com")
        monkeypatch.setenv("COM_PAT", "token")

        def fake_get(self, path_or_url, params=None):
            raise ComApiError("HTTP 401 - token expired")

        monkeypatch.setattr("com_event_core.com_client.ComClient.get", fake_get)

        enricher = IloAiEnricher()
        with caplog.at_level("WARNING", logger="com-event-core.enrich.ilo_ai"):
            assert enricher.wants(_alert_event()) is False
        # ComApiError is a RuntimeError subclass, but it happens during the
        # call, not construction, so it must be logged as a per-event info,
        # never as the "not configured" warning.
        assert not [r for r in caplog.records if "COM_BASE_URL" in r.message]

    def test_fails_open_when_com_lookup_errors(self, monkeypatch):
        monkeypatch.setenv("COM_BASE_URL", "https://eu-central.api.greenlake.hpe.com")
        monkeypatch.setenv("COM_PAT", "token")

        def fake_get(self, path_or_url, params=None):
            raise RuntimeError("boom")

        monkeypatch.setattr("com_event_core.com_client.ComClient.get", fake_get)

        enricher = IloAiEnricher()
        event = _alert_event()
        assert enricher.wants(event) is False
        assert event.mgmt_url is None

    def test_missing_device_id_skips_without_calling_com(self, monkeypatch):
        monkeypatch.setenv("COM_BASE_URL", "https://eu-central.api.greenlake.hpe.com")
        monkeypatch.setenv("COM_PAT", "token")

        def fake_get(self, path_or_url, params=None):
            raise AssertionError("must not call COM without a device id")

        monkeypatch.setattr("com_event_core.com_client.ComClient.get", fake_get)

        enricher = IloAiEnricher()
        event = _alert_event(raw={"id": "no-device-field"})
        assert enricher.wants(event) is False
        assert event.mgmt_url is None
