"""Enricher registry + fail-open runner.

`ENRICHERS` selects the optional analysis stage(s) that run between
`normalize()` and `deliver_events()`. It defaults to **none**, so every existing
deployment is unchanged until an operator opts in.

Why the runner swallows every exception
---------------------------------------
Enrichment is a *nice-to-have* on the delivery path. If a Redfish endpoint is
unreachable or the analyzer times out, the correct outcome is a ticket without
an analysis — not a failed delivery. A raising enricher would surface as a
`PartialDeliveryError`, the caller would retry the whole payload, and a flaky
analyzer would become a permanently growing backlog (and, in the shim, eventual
dead-lettering). So: enrich if you can, always deliver.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from .base import Enricher
from ..normalize import CanonicalEvent

log = logging.getLogger("com-event-core.enrich")

# Known enrichers: ENRICHERS value -> (module, class). Imported lazily so a
# deployment that doesn't use one needs neither its config nor its dependencies.
_ENRICHERS = {
    "ilo_ai": ("com_event_core.enrich.ilo_ai", "IloAiEnricher"),
}


def _instantiate(name: str) -> Enricher:
    """Import + construct the enricher registered under `name`."""
    if name not in _ENRICHERS:
        supported = ", ".join(sorted(_ENRICHERS))
        raise ValueError(f"Unsupported enricher '{name}'. Supported: {supported}.")

    module_name, class_name = _ENRICHERS[name]
    module = __import__(module_name, fromlist=[class_name])
    return getattr(module, class_name)()


def get_enrichers() -> list[Enricher]:
    """Instantiate the enrichment stage(s) for this deployment.

    `ENRICHERS` is the selector: unset or empty means **no enrichment** (the
    default), one name enables one stage, and a comma-separated list runs several
    in order. Names are lower-cased and de-duplicated with order preserved; an
    unknown name raises ValueError at startup rather than silently doing nothing.

    Config errors surface here, at startup, not on the first event.
    """
    raw = os.environ.get("ENRICHERS", "")
    ordered: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if name and name not in ordered:
            ordered.append(name)
    return [_instantiate(name) for name in ordered]


def enrich_events(
    events: Iterable[CanonicalEvent],
    enrichers: Iterable[Enricher],
) -> None:
    """Run every enricher over every event, in place. Never raises.

    An enricher that declines an event (`wants()` is False) is skipped quietly.
    An enricher that fails is logged and the event is left unenriched, so
    delivery proceeds regardless.
    """
    enrichers = list(enrichers)
    if not enrichers:
        return

    for event in events:
        for enricher in enrichers:
            try:
                if not enricher.wants(event):
                    continue
                enricher.enrich(event)
            except Exception as e:
                # Fail open: an enrichment failure must never become a delivery
                # failure (see module docstring).
                log.warning(
                    "event %s enrichment by %s failed: %s; delivering unenriched",
                    event.event_id, enricher.name, e,
                )


__all__ = ["Enricher", "get_enrichers", "enrich_events"]
