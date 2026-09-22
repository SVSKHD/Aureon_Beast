"""The broker-day cache, on PostgreSQL (11D).

Two tables, written by the observer at the moment a day is finished -- the same moment the
parquet archive is flushed, and for the same reason: that is when the day will never grow
again. A day in progress is written too, so a chart is not blank until midnight, but with
``complete=False`` so nothing mistakes it for the finished article.

**Plain upserts, not transactions.** One process writes these: the observer, which is the
only one with a feed. A second observer on the same symbol would be a misconfiguration
rather than a race, and the worst outcome of the last writer winning is a day's cached bars
coming from whichever process saw them -- the same bars either way. Nothing gates on them,
which is what makes the cheap answer the right one; a trade request, by contrast, is a
transaction with a lease.

**``recent_frames`` still takes an explicit list of dates, and that is deliberate.** A
range query is now available -- ``market_date`` is a column with an index -- but the caller
knows which days it wants (the observer's sleep cycle knows the trading week), and a range
would quietly include days the market was shut. A frame that silently arrived with fewer
days than asked for produces a quietly shorter EMA, which is the failure mode this shape
exists to avoid. What changes is that the lookup is now ONE query rather than one per day.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select

from aureon.models.base import to_utc, utc_now
from aureon.models.enums import Timeframe
from aureon.models.market_day import MarketDay, MarketDayFrame
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


def market_day_row_id(symbol: str, market_date: str) -> str:
    """``{SYMBOL}_{market_date}`` -- the BROKER date, unchanged from Firestore (11D)."""
    if not symbol:
        raise ValueError("symbol must not be empty")
    if not market_date:
        raise ValueError("market_date must not be empty")
    return f"{symbol.upper()}_{market_date}"


def market_day_frame_row_id(symbol: str, market_date: str, timeframe: Any) -> str:
    """``{SYMBOL}_{market_date}_{TIMEFRAME}``."""
    name = getattr(timeframe, "value", timeframe)
    if not name:
        raise ValueError("timeframe must not be empty")
    return f"{market_day_row_id(symbol, market_date)}_{name}"


class MarketDayRepository(PostgresRepository):
    """Reads and writes ``market_days`` and ``market_day_frames``."""

    table = tables.MarketDay.__table__

    frames = tables.MarketDayFrame.__table__

    # ── The day ──────────────────────────────────────────────────────────────

    def get_day(self, symbol: str, market_date: str) -> MarketDay | None:
        row = self._row(market_day_row_id(symbol, market_date))
        return None if row is None else MarketDay.model_validate(self._to_model_dict(row))

    def write_day(self, day: MarketDay, *, now: datetime | None = None) -> MarketDay:
        stamped = day.model_copy(update={"updated_at": to_utc(now or utc_now())})
        self._upsert(self._day_row(stamped))
        return stamped

    def complete_days(self, symbol: str, *, limit: int = 30) -> list[MarketDay]:
        """The most recent FINISHED days for one symbol, oldest first.

        ``complete`` is a column precisely so this read can exclude the day in progress: a
        tuning report that averaged a half-finished day alongside finished ones would report
        a range that is simply wrong, and nothing downstream could tell.
        """
        statement = (
            select(self.table)
            .where(self.table.c.symbol == symbol.upper())
            .where(self.table.c.complete.is_(True))
            .order_by(self.table.c.market_date.desc())
            .limit(limit)
        )
        newest_first = self._parse_all(self._rows(statement), MarketDay, what="market_day")
        return list(reversed(newest_first))

    # ── The bars ─────────────────────────────────────────────────────────────

    def get_frame(
        self, symbol: str, market_date: str, timeframe: Timeframe
    ) -> MarketDayFrame | None:
        row = self._row(
            market_day_frame_row_id(symbol, market_date, timeframe), table=self.frames
        )
        return None if row is None else MarketDayFrame.model_validate(self._frame_dict(row))

    def write_frame(
        self, frame: MarketDayFrame, *, now: datetime | None = None
    ) -> MarketDayFrame:
        stamped = frame.model_copy(update={"updated_at": to_utc(now or utc_now())})
        self._upsert(self._frame_row(stamped), table=self.frames)
        return stamped

    def recent_frames(
        self,
        symbol: str,
        timeframe: Timeframe,
        market_dates: Sequence[str],
        *,
        complete_only: bool = True,
    ) -> list[MarketDayFrame]:
        """The stored frames for ``market_dates``, oldest first, missing days skipped.

        ``complete_only`` because an unfinished day's last bar is not its last bar: feeding
        one into an aggregation produces a daily bar whose close is not the close, which is
        the failure ``mtf._complete`` refuses one level down. A caller that genuinely wants
        the day in progress -- a chart -- asks for it by name with ``get_frame``.

        A missing day is skipped rather than raised on: the cache is an optimisation, and a
        bias built from eight of the ten days asked for is a weaker claim, not an error.
        """
        wanted = sorted(set(market_dates))
        if not wanted:
            return []
        statement = (
            select(self.frames)
            .where(self.frames.c.symbol == symbol.upper())
            .where(self.frames.c.timeframe == timeframe.value)
            .where(self.frames.c.market_date.in_(wanted))
        )
        if complete_only:
            statement = statement.where(self.frames.c.complete.is_(True))
        statement = statement.order_by(self.frames.c.market_date)
        rows = self._rows(statement)
        found: list[MarketDayFrame] = []
        for row in rows:
            try:
                found.append(MarketDayFrame.model_validate(self._frame_dict(row)))
            except Exception:  # noqa: BLE001 - one bad day must not lose the rest
                continue
        return found

    # ── Mapping ───────────────────────────────────────────────────────────────

    @staticmethod
    def _day_row(day: MarketDay) -> dict[str, Any]:
        payload = day.model_dump(mode="json")
        return {
            "id": market_day_row_id(day.symbol, day.market_date),
            "schema_version": day.schema_version,
            "symbol": day.symbol,
            "market_date": day.market_date,
            "timezone": day.market_tz,
            "complete": day.complete,
            "open": day.open,
            "high": day.high,
            "low": day.low,
            "close": day.close,
            "bars": day.bars,
            "tick_volume": day.tick_volume,
            "first_bar_at": day.first_bar_at,
            "last_bar_at": day.last_bar_at,
            "updated_at": day.updated_at,
            # A JSONB list needs an object around it, because the column's type is a
            # mapping and a bare array would be a second shape the reader has to expect.
            "frames": {"items": payload["frames"]},
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        """A ``market_days`` row. ``_parse_all`` on this repository only ever reads days;
        the frames are parsed by ``_frame_dict`` at their two call sites."""
        data = dict(row)
        data.pop("id", None)
        data["market_tz"] = data.pop("timezone")
        data["frames"] = data["frames"]["items"]
        return data

    @staticmethod
    def _frame_row(frame: MarketDayFrame) -> dict[str, Any]:
        payload = frame.model_dump(mode="json")
        return {
            "id": market_day_frame_row_id(frame.symbol, frame.market_date, frame.timeframe),
            "schema_version": frame.schema_version,
            "symbol": frame.symbol,
            "market_date": frame.market_date,
            "timeframe": frame.timeframe.value,
            "timezone": frame.market_tz,
            "truncated": frame.truncated,
            "complete": frame.complete,
            "updated_at": frame.updated_at,
            "bars": {"items": payload["bars"]},
        }

    @staticmethod
    def _frame_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        data.pop("id", None)
        data["market_tz"] = data.pop("timezone")
        data["bars"] = data["bars"]["items"]
        return data
