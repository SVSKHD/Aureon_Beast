"""Durable local outbox for detections (§83).

The observer must never lose a detection because Firestore was briefly
unreachable. So a detection is written to local SQLite **first**, and only then
delivered. Delivery marks the row; it never removes it until it has succeeded.

## The ordering that matters

``delivered_at`` is set **after** the remote write returns successfully, never
before and never in the same step. If it were set first, a crash between the mark
and the write would lose the detection silently -- the row would look delivered and
nothing would ever retry it. Marking afterwards means the worst case is a *duplicate
delivery attempt*, which is harmless because the remote write is an idempotent
``set()`` keyed by ``detection_id``.

## Why SQLite, in WAL mode

WAL gives durability across a process kill and lets the worker read while the
observer writes, without the writer blocking on a reader. ``synchronous=FULL`` is
kept (rather than the faster NORMAL) because the whole purpose of this table is to
survive an abrupt termination -- trading a real durability guarantee for write
throughput on a few detections an hour would be a bad bargain.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aureon.models.base import to_utc, utc_now
from aureon.models.detection import Detection

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    detection_id  TEXT PRIMARY KEY,
    payload_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    delivered_at  TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);
-- Partial index: the worker only ever scans undelivered rows, and once a backlog
-- is cleared this index stays tiny regardless of how much history the table holds.
CREATE INDEX IF NOT EXISTS outbox_undelivered
    ON outbox (created_at) WHERE delivered_at IS NULL;
"""


@dataclass(frozen=True)
class OutboxRow:
    """One queued detection."""

    detection_id: str
    payload: dict[str, object]
    created_at: datetime
    delivered_at: datetime | None
    attempts: int
    last_error: str | None

    @property
    def delivered(self) -> bool:
        return self.delivered_at is not None


class LocalOutbox:
    """SQLite-backed, crash-safe queue of detections awaiting delivery."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False plus an explicit lock: the observer thread enqueues
        # while the worker thread delivers, and SQLite objects are not safe to share
        # across threads without serialising access.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure()

    def _configure(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ── Writing ───────────────────────────────────────────────────────────────

    def enqueue(self, detection: Detection) -> bool:
        """Queue a detection. Idempotent on ``detection_id``.

        Returns True if a new row was inserted, False if it was already queued.
        Re-enqueueing is expected, not exceptional: the observer's startup replays
        from its last processed candle and may re-derive detections it already has.
        An already-delivered row is never resurrected.
        """
        payload = detection.model_dump(mode="json")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO outbox (detection_id, payload_json, created_at) "
                "VALUES (?, ?, ?)",
                (
                    detection.detection_id,
                    json.dumps(payload, sort_keys=True),
                    utc_now().isoformat(),
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def enqueue_many(self, detections: Iterable[Detection]) -> int:
        """Queue several detections in one transaction. Returns rows inserted."""
        rows = [
            (
                d.detection_id,
                json.dumps(d.model_dump(mode="json"), sort_keys=True),
                utc_now().isoformat(),
            )
            for d in detections
        ]
        if not rows:
            return 0
        with self._lock:
            before = self._total()
            self._conn.executemany(
                "INSERT OR IGNORE INTO outbox (detection_id, payload_json, created_at) "
                "VALUES (?, ?, ?)",
                rows,
            )
            self._conn.commit()
            return self._total() - before

    def mark_delivered(self, detection_id: str, *, at: datetime | None = None) -> None:
        """Mark a row delivered. Called ONLY after the remote write succeeded."""
        stamp = to_utc(at or utc_now()).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET delivered_at = ?, last_error = NULL WHERE detection_id = ?",
                (stamp, detection_id),
            )
            self._conn.commit()

    def record_failure(self, detection_id: str, error: str) -> None:
        """Count a failed attempt and keep the error, leaving the row undelivered."""
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ? "
                "WHERE detection_id = ?",
                (error[:1000], detection_id),
            )
            self._conn.commit()

    # ── Reading ───────────────────────────────────────────────────────────────

    def pending(self, limit: int = 100) -> list[OutboxRow]:
        """Undelivered rows, oldest first.

        Ordered by ``created_at`` so detections arrive in roughly the order they were
        observed, which keeps a Firestore listener's view sensible during a
        backlog drain.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE delivered_at IS NULL "
                "ORDER BY created_at, detection_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, detection_id: str) -> OutboxRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM outbox WHERE detection_id = ?", (detection_id,)
            ).fetchone()
        return self._row(row) if row else None

    def pending_count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL"
                ).fetchone()[0]
            )

    def delivered_count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE delivered_at IS NOT NULL"
                ).fetchone()[0]
            )

    def _total(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    def __len__(self) -> int:
        with self._lock:
            return self._total()

    @staticmethod
    def _row(row: sqlite3.Row) -> OutboxRow:
        return OutboxRow(
            detection_id=row["detection_id"],
            payload=json.loads(row["payload_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            delivered_at=(
                datetime.fromisoformat(row["delivered_at"]) if row["delivered_at"] else None
            ),
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
        )

    # ── Maintenance ───────────────────────────────────────────────────────────

    def purge_delivered(self, before: datetime) -> int:
        """Delete DELIVERED rows older than ``before``. Never touches pending rows.

        The WHERE clause names ``delivered_at IS NOT NULL`` explicitly rather than
        relying on the timestamp comparison alone, so a bug here can only ever
        delete something already safely in Firestore.
        """
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM outbox WHERE delivered_at IS NOT NULL AND delivered_at < ?",
                (to_utc(before).isoformat(),),
            )
            self._conn.commit()
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> LocalOutbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
