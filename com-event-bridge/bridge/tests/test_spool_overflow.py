from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.spool as spool_module


def test_overflow_lane_is_bounded_separately(tmp_path, monkeypatch):
    monkeypatch.setattr(spool_module, "SPOOL_OVERFLOW_MAX_BYTES", 4)
    spool = spool_module.SpoolStore(str(tmp_path / "spool.db"), max_bytes=3)

    spool.put(b"abc", {})
    with pytest.raises(spool_module.SpoolFull):
        spool.put(b"d", {})

    row_id = spool.put_overflow(b"abcd", {})
    assert row_id == 2

    with pytest.raises(spool_module.SpoolFull):
        spool.put_overflow(b"e", {})

    spool.close()


def test_worker_delivers_overflow_without_enrichment(tmp_path, monkeypatch):
    spool = spool_module.SpoolStore(str(tmp_path / "spool.db"), max_bytes=100)
    spool.put_overflow(b'{"id":"overflow-event"}', {})
    calls: list[object] = []
    worker = None

    def fake_enrich(events, enrichers):
        calls.append(("enrich", enrichers))

    def fake_deliver(events, adapters, dedup):
        calls.append(("deliver", events))
        worker.stop()

    monkeypatch.setattr(spool_module, "normalize", lambda payload: [SimpleNamespace(event_id="e1")])
    monkeypatch.setattr(spool_module, "enrich_events", fake_enrich)
    monkeypatch.setattr(spool_module, "deliver_events", fake_deliver)

    worker = spool_module.SpoolWorker(
        spool,
        adapters=[SimpleNamespace(name="test")],
        dedup=object(),
        enrichers=[object()],
    )
    worker.run()

    assert calls[0] == ("enrich", ())
    assert calls[1][0] == "deliver"
    assert spool.pending() == 0
    spool.close()


def test_http_admission_returns_202_when_overflow_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("COM_SHARED_SECRET", "test-secret")
    monkeypatch.setenv("DELIVERY_MODE", "sync")
    monkeypatch.setenv("TARGETS", "webhook")
    monkeypatch.setenv("WEBHOOK_URL", "http://target.invalid")
    monkeypatch.setenv("DEDUP_DB_PATH", os.fspath(tmp_path / "dedup.db"))
    monkeypatch.delenv("ENRICHERS", raising=False)
    app_module = importlib.import_module("app")

    class OverflowSpool:
        def put(self, body, properties):
            raise spool_module.SpoolFull("normal lane full")

        def put_overflow(self, body, properties):
            return 42

    monkeypatch.setattr(app_module, "spool", OverflowSpool())
    response = app_module._accept_to_spool(b"{}", "server")

    assert response.status_code == 202
    assert response.headers["x-bridge-event-id"] == "42"