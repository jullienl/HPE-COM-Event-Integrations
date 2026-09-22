"""Tests for the enricher registry: ENRICHERS parsing and priority ordering.

The key property under test: run order is decided by each Enricher's
`priority`, never by the order names happen to be listed in ENRICHERS.
"""

from __future__ import annotations

import pytest

from com_event_core.enrich import get_enrichers
from com_event_core.enrich.hpe_advisories import HpeAdvisoriesEnricher
from com_event_core.enrich.ilo_ai import IloAiEnricher


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ENRICHERS", raising=False)
    monkeypatch.setenv("AI_ANALYZER_URL", "http://analyzer.test")


def test_priority_order_independent_of_enrichers_string_order(monkeypatch):
    monkeypatch.setenv("ENRICHERS", "ilo_ai,hpe_advisories")
    enrichers = get_enrichers()
    assert [type(e) for e in enrichers] == [HpeAdvisoriesEnricher, IloAiEnricher]


def test_priority_order_matches_when_already_correct(monkeypatch):
    monkeypatch.setenv("ENRICHERS", "hpe_advisories,ilo_ai")
    enrichers = get_enrichers()
    assert [type(e) for e in enrichers] == [HpeAdvisoriesEnricher, IloAiEnricher]


def test_single_enricher_still_works(monkeypatch):
    monkeypatch.setenv("ENRICHERS", "ilo_ai")
    enrichers = get_enrichers()
    assert [type(e) for e in enrichers] == [IloAiEnricher]


def test_duplicate_names_deduplicated(monkeypatch):
    monkeypatch.setenv("ENRICHERS", "ilo_ai,hpe_advisories,ilo_ai")
    enrichers = get_enrichers()
    assert [type(e) for e in enrichers] == [HpeAdvisoriesEnricher, IloAiEnricher]


def test_empty_enrichers_returns_nothing(monkeypatch):
    monkeypatch.setenv("ENRICHERS", "")
    assert get_enrichers() == []


def test_unknown_enricher_raises(monkeypatch):
    monkeypatch.setenv("ENRICHERS", "not-a-real-enricher")
    with pytest.raises(ValueError, match="Unsupported enricher"):
        get_enrichers()
