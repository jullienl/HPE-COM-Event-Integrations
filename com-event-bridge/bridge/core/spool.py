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

from com_event_core import DedupStore, normalize

log = logging.getLogger("com-event-bridge.spool")

SPOOL_PATH = os.environ.get("SPOOL_PATH", "./spool.db")
# Reject new events once the pending backlog exceeds this many bytes, so a
# prolonged target outage can't fill the disk (handler returns 503 as backpressure;
# COM does not retry, so an event rejected here is dropped).
SPOOL_MAX_BYTES = int(os.environ.get("SPOOL_MAX_BYTES", str(50 * 1024 * 1024)))  # 50 MB
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
                   next_attempt  INTEGER NOT NULL DEFAULT 0
               )"""
        )
        self._conn.commit()

    def put(self, body: bytes, properties: dict[str, str]) -> int:
        """Persist a raw event. Raises SpoolFull if the backlog is over budget."""
        now = int(time.time())
        with self._lock:
            pending = self._conn.execute(
                "SELECT COALESCE(SUM(LENGTH(body)), 0) FROM spool"
            ).fetchone()[0]
            if pending + len(body) > self._max_bytes:
                raise SpoolFull(f"spool backlog {pending} + {len(body)} > {self._max_bytes}")

            cur = self._conn.execute(
                "INSERT INTO spool (body, properties, enqueued_at, next_attempt) "
                "VALUES (?, ?, ?, ?)",
                (body, json.dumps(properties), now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def claim_due(self, now: int) -> tuple[int, bytes, int] | None:
        """Return (id, body, attempts) for the oldest event whose retry time has
        arrived, or None if nothing is due."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, body, attempts FROM spool WHERE next_attempt <= ? "
                "ORDER BY id ASC LIMIT 1",
                (now,),
            ).fetchone()
            return (int(row[0]), row[1], int(row[2])) if row else None

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
    """Background thread that drains the spool into the target adapter."""

    def __init__(self, spool: SpoolStore, adapter, dedup: DedupStore) -> None:
        super().__init__(name="spool-worker", daemon=True)
        self._spool = spool
        self._adapter = adapter
        self._dedup = dedup
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("spool worker started; draining to target=%s", self._adapter.name)
        while not self._stop.is_set():
            claimed = self._spool.claim_due(int(time.time()))
            if claimed is None:
                self._stop.wait(SPOOL_POLL_SECONDS)
                continue

            row_id, body, attempts = claimed
            try:
                event = normalize(json.loads(body.decode("utf-8")))

                if self._dedup.is_duplicate(event.dedup_key):
                    log.info("event %s duplicate; dropping from spool", event.event_id)
                    self._spool.delete(row_id)
                    continue

                self._adapter.forward(event)
                self._spool.delete(row_id)
                log.info("event %s delivered from spool", event.event_id)

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
