"""Tests for the Mattermost adapter's message building and error reporting.

Mattermost does not render Slack Block Kit, so the payload must use message
attachments with the analysis and advisories as their own attachments.
"""

from __future__ import annotations

import httpx
import pytest

from com_event_core.adapters import get_adapters
from com_event_core.adapters.mattermost import MattermostAdapter, _check
from com_event_core.normalize import ACTION_CLEAR, ACTION_RAISE, CanonicalEvent


def _event(**overrides) -> CanonicalEvent:
    base = dict(
        event_id="evt-1",
        operation="Updated",
        title="Server team-01-server health CRITICAL",
        severity="critical",
        resource_serial="CZ2311004G",
        resource_model="ProLiant DL360 Gen10 Plus",
        mgmt_url="https://ilo-memory",
        time_created=None,
        action=ACTION_RAISE,
        resource_name="team-01-server",
        description="Server: team-01-server\nHealth summary: CRITICAL",
    )
    base.update(overrides)
    return CanonicalEvent(**base)


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setenv("MATTERMOST_WEBHOOK_URL", "http://mm:8065/hooks/abc")
    monkeypatch.delenv("MATTERMOST_USERNAME", raising=False)
    return MattermostAdapter()


def test_registered_under_targets(monkeypatch):
    monkeypatch.setenv("TARGETS", "mattermost")
    monkeypatch.setenv("MATTERMOST_WEBHOOK_URL", "http://mm:8065/hooks/abc")
    assert [a.name for a in get_adapters()] == ["mattermost"]


def test_raise_without_analysis_has_one_attachment_and_no_blocks(adapter):
    msg = adapter._to_message(_event())
    assert "blocks" not in str(msg)
    assert len(msg["attachments"]) == 1
    assert "team-01-server" in msg["text"]
    fields = {f["title"]: f["value"] for f in msg["attachments"][0]["fields"]}
    assert fields["Resource"] == "team-01-server"
    assert "[Open iLO](https://ilo-memory)" in msg["attachments"][0]["text"]


def test_analysis_is_its_own_attachment(adapter):
    msg = adapter._to_message(_event(
        analysis_summary="A DIMM failed.",
        analysis_root_cause="Uncorrectable ECC error.",
        analysis_confidence=0.94,
        analysis_actions=["Reseat the DIMM", "Run diagnostics"],
    ))
    analysis = msg["attachments"][1]
    assert analysis["text"] == "A DIMM failed."
    fields = {f["title"]: f["value"] for f in analysis["fields"]}
    assert fields["Confidence"] == "94%"
    assert fields["Likely root cause"] == "Uncorrectable ECC error."
    assert fields["Recommended actions"] == "1. Reseat the DIMM\n2. Run diagnostics"


def test_advisories_are_their_own_attachment(adapter):
    msg = adapter._to_message(_event(advisory_references=[
        {"status": "open", "id": "a00146775en_us", "title": "MRSA StorCLI BSOD",
         "url": "https://support.hpe.com/a00146775en_us"},
    ]))
    text = msg["attachments"][-1]["text"]
    assert "**[OPEN]** `a00146775en_us` MRSA StorCLI BSOD" in text
    assert "(https://support.hpe.com/a00146775en_us)" in text


def test_clear_is_rendered_as_resolved_without_analysis(adapter):
    msg = adapter._to_message(_event(action=ACTION_CLEAR, severity="normal"))
    assert "Resolved" in msg["text"]
    assert len(msg["attachments"]) == 1


def test_username_override_only_when_configured(monkeypatch, adapter):
    assert "username" not in adapter._to_message(_event())
    monkeypatch.setenv("MATTERMOST_USERNAME", "HPE COM")
    assert MattermostAdapter()._to_message(_event())["username"] == "HPE COM"


def test_error_includes_the_server_message():
    resp = httpx.Response(400, json={"message": "Failed to handle the payload"})
    with pytest.raises(RuntimeError, match="HTTP 400 - Failed to handle the payload"):
        _check(resp)


def test_success_does_not_raise():
    _check(httpx.Response(200, text="ok"))
