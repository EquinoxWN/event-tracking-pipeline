"""SQLite tables for accepted events and dead letters; every write is safe to repeat."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_DEAD_PAYLOAD = 16_384

DDL = """
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    name         TEXT NOT NULL,
    anonymous_id TEXT NOT NULL,
    user_id      TEXT,
    client_ts    TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dead_letters (
    fingerprint  TEXT PRIMARY KEY,
    reason       TEXT NOT NULL,
    detail       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    seen         INTEGER NOT NULL DEFAULT 1
);
"""


@dataclass(frozen=True)
class DeadLetter:
    """A rejected event or request body and why it was rejected."""

    reason: str
    detail: str
    payload: str

    @property
    def fingerprint(self) -> str:
        """Stable key: the same bad input for the same reason is stored once."""
        digest = hashlib.sha256()
        digest.update(self.reason.encode())
        digest.update(b"\x00")
        digest.update(self.payload.encode("utf-8", "surrogatepass"))
        return digest.hexdigest()


@dataclass(frozen=True)
class WriteResult:
    """Counts from one batch write."""

    inserted: int
    duplicates: int
    dead_letters: int


def storable(text: str) -> str:
    """Text SQLite can hold: lone surrogates become backslash escapes."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def canonical(event: dict[str, Any]) -> str:
    """Compact JSON with sorted keys, used for stored payloads."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class EventStore:
    """One SQLite database; a lock serialises writes from the collector's threads."""

    def __init__(self, path: str | Path) -> None:
        """Open (or create) the database and its tables."""
        self._path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self._path, timeout=5.0, check_same_thread=False, isolation_level=None
        )
        with self._lock:
            if self._path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(DDL)

    def write_batch(
        self, events: list[dict[str, Any]], dead: list[DeadLetter], received_at: str
    ) -> WriteResult:
        """Insert valid events and dead letters in one transaction; repeats change nothing."""
        inserted = 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                for e in events:
                    cur.execute(
                        "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            e["event_id"],
                            e["type"],
                            e["name"],
                            e["anonymous_id"],
                            e.get("user_id"),
                            e["timestamp"],
                            received_at,
                            canonical(e),
                        ),
                    )
                    inserted += cur.rowcount
                for d in dead:
                    cur.execute(
                        "INSERT INTO dead_letters VALUES (?, ?, ?, ?, ?, ?, 1) "
                        "ON CONFLICT(fingerprint) DO UPDATE SET "
                        "seen = seen + 1, last_seen = excluded.last_seen",
                        (
                            d.fingerprint,
                            d.reason,
                            storable(d.detail),
                            storable(d.payload[:MAX_DEAD_PAYLOAD]),
                            received_at,
                            received_at,
                        ),
                    )
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise
        return WriteResult(inserted, len(events) - inserted, len(dead))

    def count_events(self) -> int:
        """Number of distinct stored events."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0])

    def events_by_name(self) -> list[tuple[str, int]]:
        """Stored events per name, most frequent first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, COUNT(*) FROM events GROUP BY name ORDER BY 2 DESC, 1"
            ).fetchall()
        return [(str(n), int(c)) for n, c in rows]

    def dead_letter_summary(self) -> list[tuple[str, int, int]]:
        """(reason, distinct rows, times seen) per dead-letter reason."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT reason, COUNT(*), SUM(seen) FROM dead_letters GROUP BY reason ORDER BY 1"
            ).fetchall()
        return [(str(r), int(n), int(s)) for r, n, s in rows]

    def dead_letters(self, limit: int = 20) -> list[tuple[str, str, int]]:
        """Most recent dead letters as (reason, detail, seen)."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            rows = self._conn.execute(
                "SELECT reason, detail, seen FROM dead_letters ORDER BY last_seen DESC, reason "
                "LIMIT ?",
                (limit,),
            ).fetchall()
        return [(str(r), str(d), int(s)) for r, d, s in rows]

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()
