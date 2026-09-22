"""Session summaries, on PostgreSQL (§18).

Keyed by ``{market_date}__{session}`` so regenerating a session overwrites it rather than
accumulating variants -- the same idempotence detections rely on, for the same reason: a
replay of a day must be safe to re-run.

**``in_period`` is now a range query instead of a full scan.** The Firestore version
streamed the whole collection and filtered in Python, because a range on a nested
``started_at.utc`` would have needed an index it did not have. Here ``started_at`` is a
column, so the database does the filtering -- and a review of one week stops reading every
session ever recorded.

``timezone`` is stored beside the two instants because ``started_at`` and ``ended_at`` are
``MarketTime``, and a ``MarketTime`` cannot be rebuilt from an instant alone. Deriving the
zone from the running config on read would make a session stored under one broker's clock
render itself in another's the day that config changed (decision 357).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from aureon.models.base import to_utc
from aureon.models.session import SessionSummary
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


def session_row_id(market_date: str, session: Any) -> str:
    """``{market_date}__{session}`` -- the Firestore document id, unchanged (C-12)."""
    name = getattr(session, "value", session)
    if not market_date:
        raise ValueError("market_date must not be empty")
    if not name:
        raise ValueError("session must not be empty")
    return f"{market_date}__{name}"


class SessionRepository(PostgresRepository):
    """Reads and upserts ``sessions``."""

    table = tables.SessionSummary.__table__

    def upsert(self, summary: SessionSummary) -> str:
        row_id = session_row_id(summary.market_date, summary.session)
        self._upsert(self._to_row(summary))
        return row_id

    def get(self, market_date: str, session: Any) -> SessionSummary | None:
        row = self._row(session_row_id(market_date, session))
        return None if row is None else SessionSummary.model_validate(self._to_model_dict(row))

    def in_period(self, start: datetime, end: datetime) -> list[SessionSummary]:
        """Sessions whose ``started_at`` falls in ``[start, end)``.

        Half-open for the reason every period read in this system is: a session that starts
        exactly at a boundary belongs to one period, and a closed interval would put it in
        both -- which double-counts it in whichever review runs second.
        """
        statement = (
            select(self.table)
            .where(self.table.c.started_at >= to_utc(start))
            .where(self.table.c.started_at < to_utc(end))
            .order_by(self.table.c.started_at)
        )
        return self._parse_all(self._rows(statement), SessionSummary, what="session")

    def for_market_date(
        self, market_date: str, *, symbol: str | None = None
    ) -> list[SessionSummary]:
        """One broker day's sessions, for a daily review."""
        statement = select(self.table).where(self.table.c.market_date == market_date)
        if symbol is not None:
            statement = statement.where(self.table.c.symbol == symbol.upper())
        statement = statement.order_by(self.table.c.started_at)
        return self._parse_all(self._rows(statement), SessionSummary, what="session")

    @staticmethod
    def _to_row(summary: SessionSummary) -> dict[str, Any]:
        return {
            "session_doc_id": session_row_id(summary.market_date, summary.session),
            "schema_version": summary.schema_version,
            "session_id": summary.session_id,
            "account_scope": summary.account_scope,
            "symbol": summary.symbol,
            "timeframe": summary.timeframe.value,
            "session": summary.session.value,
            "market_date": summary.market_date,
            "timezone": summary.started_at.market_tz,
            "session_config_version": summary.session_config_version,
            "started_at": summary.started_at.utc,
            "ended_at": summary.ended_at.utc,
            "open": summary.open,
            "high": summary.high,
            "low": summary.low,
            "close": summary.close,
            "trend": summary.trend,
            "change": summary.change,
            "change_points": summary.change_points,
            "range": summary.range,
            "candle_count": summary.candle_count,
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        timezone = data.pop("timezone")
        data.pop("session_doc_id", None)
        data["started_at"] = {"utc": data["started_at"], "market_tz": timezone}
        data["ended_at"] = {"utc": data["ended_at"], "market_tz": timezone}
        return data
