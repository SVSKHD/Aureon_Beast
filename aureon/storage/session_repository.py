"""Session summary persistence (§18).

Keyed by ``{market_date}__{session}`` so regenerating a session overwrites it rather
than accumulating duplicates -- the same idempotence the detections collection relies
on, for the same reason: a replay must be safe to re-run.
"""

from __future__ import annotations

from typing import Any

from aureon.models.session import SessionSummary
from aureon.storage import paths


class SessionRepository:
    """Reads and upserts ``sessions`` documents."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def upsert(self, summary: SessionSummary) -> str:
        path = paths.session_path(summary.market_date, summary.session.value)
        self._client.document(path).set(summary.model_dump(mode="json"))
        return path

    def get(self, market_date: str, session: str) -> SessionSummary | None:
        snapshot = self._client.document(paths.session_path(market_date, session)).get()
        if not getattr(snapshot, "exists", False):
            return None
        return SessionSummary.model_validate(snapshot.to_dict())
