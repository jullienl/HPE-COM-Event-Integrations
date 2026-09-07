"""Local de-duplication store (SQLite, TTL-based).

Suppresses duplicate events (e.g. COM/relay retries) so a target doesn't receive
the same incident twice. State is local to the consumer process.

Thread-safe: `com-event-bridge` touches the store from both the HTTP handler and
the background spool worker, so access is guarded by a lock and the connection is
opened with `check_same_thread=False`. Single-threaded consumers (the shim) are
unaffected.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

DEDUP_DB_PATH = os.environ.get("DEDUP_DB_PATH", "./dedup.db")
DEDUP_TTL_SECONDS = int(os.environ.get("DEDUP_TTL_SECONDS", "3600"))


class DedupStore:
    def __init__(self, path: str = DEDUP_DB_PATH, ttl: int = DEDUP_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen (dedup_key TEXT PRIMARY KEY, seen_at INTEGER)"
        )
        self._conn.commit()

    def already_done(self, dedup_key: str) -> bool:
        """Return True if `dedup_key` was marked done within the TTL window.

        Read-only: it does NOT record the key. Pair it with mark_done(), which is
        called only *after* a successful delivery, so a failed-then-retried
        delivery is re-attempted rather than suppressed.
        """
        if self._ttl <= 0:
            return False  # dedup disabled

        now = int(time.time())
        cutoff = now - self._ttl

        with self._lock:
            # Purge expired keys so the store stays small.
            self._conn.execute("DELETE FROM seen WHERE seen_at < ?", (cutoff,))
            row = self._conn.execute(
                "SELECT seen_at FROM seen WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()
            self._conn.commit()
            return row is not None

    def mark_done(self, dedup_key: str) -> None:
        """Record `dedup_key` as done (idempotent). Call only after success."""
        if self._ttl <= 0:
            return  # dedup disabled

        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO seen (dedup_key, seen_at) VALUES (?, ?)",
                (dedup_key, now),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
