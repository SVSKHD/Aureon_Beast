"""What the trader said about a trade (9D).

A separate collection, and that is the whole design. A CLOSED trade refuses every field
write but the reconciliation stamps (§45), because a closed trade is a record of what
happened and editing one silently rewrites history. A note is not a correction to that
record -- it is a human's sentence beside it -- so it lives in its own document and the
weekly review prints the two together.

## Nothing automated reads these

Not the cohort, not the assessment, not any gate. A note is for the person who wrote it and
for whoever reads the review. The moment a note influenced a number, the number would stop
being a measurement of the market and start being a measurement of the trader's mood at the
time they typed it -- and nothing downstream could tell the two apart. A boundary test keeps
``aureon/services/assessment_service.py`` from importing this module at all.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aureon.models.assessment import TradeNote
from aureon.models.base import to_utc, utc_now
from aureon.models.identity import new_note_id
from aureon.storage import paths


def _where(query: Any, field: str, op: str, value: Any) -> Any:
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter

        return query.where(filter=FieldFilter(field, op, value))
    except (TypeError, ImportError):
        return query.where(field, op, value)


class TradeNoteRepository:
    """Reads and writes ``{prefix}_trade_notes``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def add(
        self,
        trade_id: str,
        *,
        author: str,
        text: str,
        now: datetime | None = None,
    ) -> TradeNote:
        """Record one note. Never touches the trade document."""
        note = TradeNote(
            note_id=new_note_id(),
            trade_id=trade_id,
            author=str(author),
            text=text.strip(),
            at=to_utc(now or utc_now()),
        )
        self._client.document(paths.trade_note_path(note.note_id)).set(
            note.model_dump(mode="json")
        )
        return note

    def get(self, note_id: str) -> TradeNote | None:
        snapshot = self._client.document(paths.trade_note_path(note_id)).get()
        if not getattr(snapshot, "exists", False):
            return None
        return TradeNote.model_validate(snapshot.to_dict())

    def for_trade(self, trade_id: str) -> list[TradeNote]:
        """One trade's notes, oldest first, so a thread reads in the order it was written."""
        query = _where(
            self._client.collection(paths.TRADE_NOTES), "trade_id", "==", trade_id
        )
        return sorted(
            (TradeNote.model_validate(doc.to_dict()) for doc in query.stream()),
            key=_written,
        )

    def for_trades(self, trade_ids: list[str]) -> dict[str, list[TradeNote]]:
        """Notes for a period's trades, grouped by trade.

        One read per trade rather than a whole-collection scan: a review covers a handful of
        trades and the collection grows for as long as anybody writes notes.
        """
        return {trade_id: self.for_trade(trade_id) for trade_id in trade_ids}


def _written(note: TradeNote) -> datetime:
    from datetime import UTC

    if note.at is None:
        return datetime.min.replace(tzinfo=UTC)
    return to_utc(note.at)
