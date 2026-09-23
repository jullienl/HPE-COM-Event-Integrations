"""Durable on-disk spool (SQLite) — the bridge's optional delivery buffer.

Without a cloud queue, the bridge needs somewhere to hold events if the target
is briefly unreachable. In `DELIVERY_MODE=spool` the HTTP handler persists the
raw event here and returns `202` to COM immediately; a background worker
(`SpoolWorker`) drains the spool, forwarding each event via the target adapter
and deleting it on success (retrying with capped exponential backoff on
failure). The spool is crash-safe: on restart, unsent events are still present.

This trades a small local disk footprint for "no lost events" without any cloud
dependency, and it is the DEFAULT delivery mode. `DELIVERY_MODE=sync` is an
opt-in best-effort mode that does not use the spool at all — it forwards inline
(COM webhooks are fire-and-forget, with no retries, so a delivery that fails
inline is lost). In spool mode SPOOL_PATH MUST live on durable storage (a mounted
volume); app.py refuses to start otherwise so an ephemeral path can't silently
drop the backlog on restart.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

from com_event_core import DedupStore, deliver_events, enrich_events, normalize

log = logging.getLogger("com-event-bridge.spool")

SPOOL_PATH = os.environ.get("SPOOL_PATH", "./spool.db")
# Reject normal records once the pending backlog exceeds this many bytes.
SPOOL_MAX_BYTES = int(os.environ.get("SPOOL_MAX_BYTES", str(50 * 1024 * 1024)))  # 50 MB
# Reserve a separate bounded lane for accepting events without enrichment when
# the normal backlog is full. Both budgets consume the same durable volume.
SPOOL_OVERFLOW_MAX_BYTES = int(
    os.environ.get("SPOOL_OVERFLOW_MAX_BYTES", str(10 * 1024 * 1024))
)  # 10 MB
# Base retry delay; grows exponentially per attempt up to SPOOL_RETRY_CAP.
SPOOL_RETRY_SECONDS = int(os.environ.get("SPOOL_RETRY_SECONDS", "30"))
SPOOL_RETRY_CAP = int(os.environ.get("SPOOL_RETRY_CAP", "3600"))
# How often the worker wakes to look for due events.
SPOOL_POLL_SECONDS = float(os.environ.get("SPOOL_POLL_SECONDS", "2"))


class SpoolFull(Exception):
    """Raised by `put()` when the pending backlog exceeds SPOOL_MAX_BYTES."""


class SpoolStore:
    """Crash-safe FIFO of pending events, keyed by insertion order."""

    def __init__(self, path: str = SPOOL_PATH, max_bytes: int = SPOOL_MAX_BYTES) -> None:
        self._max_bytes = max_bytes
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS spool (
                   id            INTEGER PRIMARY KEY AUTOINCREMENT,
                   body          BLOB    NOT NULL,
                   properties    TEXT    NOT NULL,
                   enqueued_at   INTEGER NOT NULL,
                   attempts      INTEGER NOT NULL DEFAULT 0,
                   next_attempt  INTEGER NOT NULL DEFAULT 0,
                   enrich        INTEGER NOT NULL DEFAULT 1
               )"""
        )
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(spool)").fetchall()
        }
        if "enrich" not in columns:
            self._conn.execute(
                "ALTER TABLE spool ADD COLUMN enrich INTEGER NOT NULL DEFAULT 1"
            )
        self._conn.commit()

    def put(self, body: bytes, properties: dict[str, str]) -> int:
        """Persist a normal event. Raises SpoolFull if its backlog is over budget."""
        return self._put(body, properties, enrich=True)

    def put_overflow(self, body: bytes, properties: dict[str, str]) -> int:
        """Persist an unenriched event in the bounded overflow lane."""
        return self._put(body, properties, enrich=False)

    def _put(self, body: bytes, properties: dict[str, str], *, enrich: bool) -> int:
        """Persist a raw event in either the normal or overflow lane."""
        now = int(time.time())
        max_bytes = self._max_bytes if enrich else SPOOL_OVERFLOW_MAX_BYTES
        lane = 1 if enrich else 0
        with self._lock:
            pending = self._conn.execute(
                "SELECT COALESCE(SUM(LENGTH(body)), 0) FROM spool WHERE enrich = ?",
                (lane,),
            ).fetchone()[0]
            if pending + len(body) > max_bytes:
                lane_name = "spool" if enrich else "overflow spool"
                raise SpoolFull(f"{lane_name} backlog {pending} + {len(body)} > {max_bytes}")

            cur = self._conn.execute(
                "INSERT INTO spool (body, properties, enqueued_at, next_attempt, enrich) "
                "VALUES (?, ?, ?, ?, ?)",
                (body, json.dumps(properties), now, now, lane),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def claim_due(self, now: int) -> tuple[int, bytes, int, bool] | None:
        """Return (id, body, attempts, enrich) for the oldest event whose retry time has
        arrived, or None if nothing is due."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, body, attempts, enrich FROM spool WHERE next_attempt <= ? "
                "ORDER BY id ASC LIMIT 1",
                (now,),
            ).fetchone()
            return (int(row[0]), row[1], int(row[2]), bool(row[3])) if row else None

    def delete(self, row_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM spool WHERE id = ?", (row_id,))
            self._conn.commit()

    def reschedule(self, row_id: int, attempts: int, next_attempt: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE spool SET attempts = ?, next_attempt = ? WHERE id = ?",
                (attempts, next_attempt, row_id),
            )
            self._conn.commit()

    def pending(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM spool").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SpoolWorker(threading.Thread):
    """Background thread that drains the spool into the target adapter(s)."""

    def __init__(self, spool: SpoolStore, adapters, dedup: DedupStore,
                 enrichers=()) -> None:
        super().__init__(name="spool-worker", daemon=True)
        self._spool = spool
        self._adapters = adapters
        self._dedup = dedup
        self._enrichers = list(enrichers)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        targets = ", ".join(a.name for a in self._adapters)
        log.info("spool worker started; draining to target(s)=%s", targets)
        while not self._stop.is_set():
            claimed = self._spool.claim_due(int(time.time()))
            if claimed is None:
                self._stop.wait(SPOOL_POLL_SECONDS)
                continue

            row_id, body, attempts, should_enrich = claimed
            try:
                events = normalize(json.loads(body.decode("utf-8")))

                # Enrich before delivery so every adapter renders the analysis.
                # Never raises — a failed enrichment delivers the event as-is,
                # so a slow or broken analyzer cannot stall the spool drain.
                enrich_events(events, self._enrichers if should_enrich else ())

                # Fan-out every derived event to every adapter. deliver_events()
                # skips targets already done for an event and raises if any
                # target fails, so the row is rescheduled and only the failed
                # target(s) are re-attempted.
                deliver_events(events, self._adapters, self._dedup)
                self._spool.delete(row_id)
                log.info("event %s delivered from spool", events[0].event_id)

            except (json.JSONDecodeError, UnicodeDecodeError):
                # Poison message: it will never parse — drop it rather than loop.
                log.error("spool row %s is not valid JSON; discarding", row_id)
                self._spool.delete(row_id)

            except Exception as e:
                attempts += 1
                delay = min(SPOOL_RETRY_SECONDS * (2 ** (attempts - 1)), SPOOL_RETRY_CAP)
                next_attempt = int(time.time()) + delay
                log.warning(
                    "spool row %s delivery failed (attempt %s): %s; retrying in %ss",
                    row_id, attempts, e, delay,
                )
                self._spool.reschedule(row_id, attempts, next_attempt)
