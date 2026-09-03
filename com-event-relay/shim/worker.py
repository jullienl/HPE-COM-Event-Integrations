"""
COM event shim — outbound-only queue consumer with pluggable target adapters.

Drains the cloud queue (Azure Service Bus or AWS SQS) that the relay fills, and
for each message:

  1. parses + normalises the COM event into a CanonicalEvent,
  2. de-duplicates using a local SQLite TTL store,
  3. forwards it to the selected TARGET via its adapter,
  4. acknowledges the message (complete / abandon / dead-letter).

Opens only an OUTBOUND connection to the cloud queue — no inbound listener, so
it runs safely inside a protected network. Select the target with TARGET and the
queue with QUEUE_BACKEND; the same image serves every target and both clouds.

AI-generated reference implementation. Review and harden before production use.

Run:
    pip install -r requirements.txt
    python worker.py
"""

import json
import logging

from com_event_core import DedupStore, get_adapter, normalize
from core.queue import get_consumer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("com-event-shim")


def main() -> None:
    adapter = get_adapter()          # selected by TARGET; validates its config
    consumer = get_consumer()        # selected by QUEUE_BACKEND; validates its config
    dedup = DedupStore()

    log.info("shim starting; target=%s draining queue outbound-only", adapter.name)

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
                event = normalize(payload)

                if dedup.is_duplicate(event.dedup_key):
                    log.info("event %s duplicate (key=%s), skipping",
                             corr_id, event.dedup_key)
                    consumer.complete(msg)
                    continue

                adapter.forward(event)
                consumer.complete(msg)

            except Exception as e:
                # Transient failure (target down / 5xx): abandon for redelivery.
                log.error("event %s forward FAILED: %s; abandoning for retry",
                          corr_id, e)
                consumer.abandon(msg)
    finally:
        consumer.close()
        dedup.close()


if __name__ == "__main__":
    main()
