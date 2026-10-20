"""A repeated power-off snapshot must be skipped per target within the TTL."""

from __future__ import annotations

from com_event_core.dedup import DedupStore
from com_event_core.deliver import deliver_events
from com_event_core.normalize import normalize


class _Recorder:
    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[str] = []

    def forward(self, event) -> None:
        self.sent.append(event.action)


def _power_off(updated_at: str) -> dict:
    return {
        "id": "P81966-B21+3M1D4812NW",
        "type": "compute-ops-mgmt/server",
        "name": "srv",
        "operation": "Updated",
        "updatedAt": updated_at,
        "hardware": {"powerState": "OFF", "serialNumber": "3M1D4812NW"},
    }


def test_repeated_power_off_is_skipped_for_every_target(monkeypatch):
    monkeypatch.setenv("SERVER_MONITORS", "power")
    dedup = DedupStore(":memory:", 3600)
    targets = [_Recorder("mattermost"), _Recorder("github")]

    deliver_events(normalize(_power_off("2026-10-19T10:00:00Z")), targets, dedup)
    deliver_events(normalize(_power_off("2026-10-19T10:05:00Z")), targets, dedup)

    assert [t.sent for t in targets] == [["raise"], ["raise"]]
