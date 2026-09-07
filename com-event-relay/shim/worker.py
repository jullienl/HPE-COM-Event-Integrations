"""
COM event shim — outbound-only queue consumer with pluggable target adapters.

Drains the cloud queue (Azure Service Bus or AWS SQS) that the relay fills, and
for each message:

  1. parses + normalises the COM event into a CanonicalEvent,
  2. de-duplicates using a local SQLite TTL store,
  3. forwards it to the selected TARGETS via their adapters,
  4. acknowledges the message (complete / abandon / dead-letter).

Opens only an OUTBOUND connection to the cloud queue — no inbound listener, so
it runs safely inside a protected network. Select one or more targets with
TARGETS (comma-separated, e.g. "halo,slack") and the queue
with QUEUE_BACKEND; the same image serves every target and both clouds.

AI-generated reference implementation. Review and harden before production use.

Run:
    pip install -r requirements.txt
    python worker.py
"""

import json
import logging

from com_event_core import DedupStore, deliver_events, get_adapters, normalize
from core.queue import get_consumer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("com-event-shim")


def main() -> None:
    adapters = get_adapters()        # selected by TARGETS; validates config
    consumer = get_consumer()        # selected by QUEUE_BACKEND; validates its config
    dedup = DedupStore()

    targets = ", ".join(a.name for a in adapters)
    log.info("shim starting; targets=%s draining queue outbound-only", targets)

    try:
        for msg in consumer.receive():
            corr_id = msg.properties.get("relay_event_id", "unknown")
            try:
                payload = json.loads(msg.body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                log.error("event %s invalid JSON; dead-lettering", corr_id)
                consumer.dead_letter(msg, reason="invalid-json")
                continue

            try:
                events = normalize(payload)
                # Fan-out every derived event to every adapter. deliver_events()
                # skips targets already done for an event and raises if any
                # target fails, so abandon → redelivery re-attempts only the
                # failed target(s).
                deliver_events(events, adapters, dedup)
                consumer.complete(msg)

            except Exception as e:
                # Transient failure (a target down / 5xx): abandon for redelivery.
                log.error("event %s forward FAILED: %s; abandoning for retry",
                          corr_id, e)
                consumer.abandon(msg)
    finally:
        consumer.close()
        dedup.close()


if __name__ == "__main__":
    main()
