"""Detection persistence (§19, §83).

Writes are **idempotent ``set()`` by document id**, never ``add()``. That single
choice is what lets the outbox retry an ambiguous delivery without risking a
duplicate: if the first attempt actually landed, the retry overwrites it with byte-
identical content, because ``detection_id`` is a pure function of the candle that
produced it.

Detections are immutable, so there is no update method here -- only upsert and read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aureon.models.base import to_utc
from aureon.models.detection import Detection
from aureon.storage import paths


class DetectionRepository:
    """Reads and upserts ``detections`` documents."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # ── Writing ───────────────────────────────────────────────────────────────

    def upsert(self, detection: Detection) -> str:
        """Write a detection at its deterministic id. Safe to repeat."""
        payload = detection.model_dump(mode="json")
        path = paths.detection_path(detection.detection_id)
        self._client.document(path).set(payload)
        return path

    def upsert_payload(self, payload: dict[str, Any]) -> str:
        """Upsert an already-serialised detection.

        The outbox stores payloads, not models, so it can deliver a detection queued
        by an earlier version of the code without that payload having to survive a
        model round-trip first.
        """
        detection_id = payload.get("detection_id")
        if not detection_id:
            raise ValueError("detection payload has no detection_id")
        path = paths.detection_path(str(detection_id))
        self._client.document(path).set(payload)
        return path

    # ── Reading ───────────────────────────────────────────────────────────────

    def get(self, detection_id: str) -> Detection | None:
        snapshot = self._client.document(paths.detection_path(detection_id)).get()
        if not getattr(snapshot, "exists", False):
            return None
        return Detection.model_validate(snapshot.to_dict())

    def exists(self, detection_id: str) -> bool:
        snapshot = self._client.document(paths.detection_path(detection_id)).get()
        return bool(getattr(snapshot, "exists", False))

    def recent_for_symbol(
        self, symbol: str, *, since: datetime | None = None, limit: int = 10
    ) -> list[Detection]:
        """Most recent detections for a symbol, newest first (§37).

        Backs the Discord detection selector, which offers the last few detections
        within a time window so a human can link a trade to what they saw.
        """
        query = self._client.collection(paths.DETECTIONS).where("symbol", "==", symbol)
        if since is not None:
            query = query.where("detected_at.utc", ">=", to_utc(since).isoformat())
        query = query.order_by("detected_at.utc", direction="DESCENDING").limit(limit)
        return [Detection.model_validate(doc.to_dict()) for doc in query.stream()]
