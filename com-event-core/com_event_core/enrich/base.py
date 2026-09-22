"""Enrichment stage interface.

An **enricher** is not an adapter. An adapter is *terminal*: `forward()` delivers
the event to one target and the pipeline ends there. An enricher is a *pass* that
runs after `normalize()` and before `deliver_events()`, mutating the
`CanonicalEvent` in place so that **every** configured adapter renders the
enriched version::

    normalize()  ->  [CanonicalEvent]
                          |
                     enrich_events()      <- this stage
                          |
                     deliver_events()  ->  github / jira / slack / ...

Selecting an enricher via `TARGETS` would be wrong: it would run *beside* the
ticket adapter rather than *before* it, so the ticket would contain no analysis.

Contract
--------
* `wants(event)` decides whether this enricher applies. Returning False is a
  normal, quiet outcome (wrong event type, healthy snapshot, below threshold) —
  it is **not** a failure.
* `enrich(event)` mutates the event. It MAY raise: the runner
  (`enrich_events()`) catches everything and delivers the event unenriched. That
  fail-open behaviour lives in the runner on purpose, so an enricher author
  cannot forget it and turn a flaky dependency into an endlessly retried
  delivery backlog.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from com_event_core.normalize import CanonicalEvent


class Enricher(ABC):
    """Contract every enrichment stage implements."""

    #: Short name used to select the enricher via the ENRICHERS env var.
    name: str = "base"

    #: Run order, lower runs first. `get_enrichers()` sorts by this — NOT by
    #: the order names are listed in ENRICHERS — so a dependency between two
    #: enrichers (e.g. one attaches evidence the other must see) is enforced by
    #: the framework regardless of how an operator writes the env var.
    #: `ENRICHERS=ilo_ai,hpe_advisories` and `ENRICHERS=hpe_advisories,ilo_ai`
    #: run in the identical, correct order. Ties keep the ENRICHERS order given
    #: (stable sort), so unrelated enrichers with no ordering requirement are
    #: unaffected.
    priority: int = 100

    def wants(self, event: CanonicalEvent) -> bool:
        """True if this enricher applies to `event`.

        Returning False is a quiet skip, not an error. Override to gate on
        action/severity/source type so the expensive path is only taken for the
        events that warrant it.
        """
        return True

    @abstractmethod
    def enrich(self, event: CanonicalEvent) -> None:
        """Mutate `event` in place with whatever this stage adds.

        May raise — the runner catches and delivers the event unenriched.
        """
