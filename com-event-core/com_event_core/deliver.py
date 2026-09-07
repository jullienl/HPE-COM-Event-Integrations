"""Fan-out delivery to one or more target adapters.

A single COM event can be delivered to several targets at once (e.g. open a
ticket *and* post to Slack). Both consumers — the relay's shim and the bridge —
call `deliver()` so they share identical fan-out and retry semantics.

Correctness rules that make fan-out safe under retries:

* De-dup is **per adapter** (keyed on the event's dedup_key + the adapter name),
  so each target is tracked independently.
* An adapter is marked done **only after** its `forward()` succeeds. If some
  adapters fail, `deliver()` raises `PartialDeliveryError` so the caller retries
  the whole event; on that retry the adapters that already succeeded are skipped
  and only the failed ones are re-attempted — no duplicate tickets, no lost
  events.

Reliable multi-target delivery therefore depends on the caller retrying on
`PartialDeliveryError` (spool worker / queue redelivery). In best-effort inline
paths (bridge `sync` mode) there is no retry, so a failed target's copy is lost —
consistent with that mode being best-effort.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from .adapters import TargetAdapter
from .dedup import DedupStore
from .normalize import CanonicalEvent

log = logging.getLogger("com-event-core.deliver")


class PartialDeliveryError(Exception):
    """One or more adapters failed to receive the event.

    Adapters that already succeeded (this attempt or a prior one) are recorded in
    the DedupStore, so a retry re-attempts only the targets listed in `failures`.
    """

    def __init__(self, failures: list[tuple[str, Exception]]) -> None:
        self.failures = failures
        names = ", ".join(name for name, _ in failures)
        super().__init__(f"delivery failed for: {names}")


def deliver(
    event: CanonicalEvent,
    adapters: Iterable[TargetAdapter],
    dedup: DedupStore,
) -> None:
    """Deliver `event` to every adapter, skipping ones already done for this event.

    Marks each adapter done only after a successful `forward()`. Returns normally
    when every adapter has received the event (now or on a prior attempt); raises
    PartialDeliveryError if any adapter failed so the caller can retry.
    """
    failures: list[tuple[str, Exception]] = []

    for adapter in adapters:
        key = f"{event.dedup_key}:{adapter.name}"

        if dedup.already_done(key):
            log.info("event %s already delivered to %s; skipping",
                     event.event_id, adapter.name)
            continue

        try:
            adapter.forward(event)
        except Exception as e:  # adapters raise arbitrary target/transport errors
            log.error("event %s forward to %s FAILED: %s",
                      event.event_id, adapter.name, e)
            failures.append((adapter.name, e))
            continue

        dedup.mark_done(key)
        log.info("event %s delivered to %s", event.event_id, adapter.name)

    if failures:
        raise PartialDeliveryError(failures)


def deliver_events(
    events: Iterable[CanonicalEvent],
    adapters: Iterable[TargetAdapter],
    dedup: DedupStore,
) -> None:
    """Deliver a batch of events (one COM payload may normalise to several).

    Each event is delivered to every adapter via `deliver()`. Delivery of the
    remaining events continues past a failing one; if any adapter of any event
    failed, a single `PartialDeliveryError` aggregating all failures is raised so
    the caller retries the whole payload. On that retry, events/adapters already
    delivered are skipped by the per-adapter dedup, so only the failed target(s)
    of the failed event(s) are re-attempted — no duplicate tickets.
    """
    adapters = list(adapters)  # reusable across events (Iterable may be one-shot)
    failures: list[tuple[str, Exception]] = []
    for event in events:
        try:
            deliver(event, adapters, dedup)
        except PartialDeliveryError as e:
            failures.extend(e.failures)
    if failures:
        raise PartialDeliveryError(failures)
