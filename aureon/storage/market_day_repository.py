"""Where one broker day's shape and bars live (11D).

Two documents, written by the observer at the moment a day is finished -- the same moment the
parquet archive is flushed, and for the same reason: that is when the day will never grow
again. A day in progress is written too, so a chart is not blank until midnight, but with
``complete=False`` so nothing mistakes it for the finished article.

## Plain writes, not transactions

One process writes these: the observer, which is the only one with a feed. A second observer on
the same symbol would be a misconfiguration rather than a race, and the worst outcome of the
last writer winning is a day's cached bars coming from whichever process saw them -- the same
bars either way. Nothing gates on them, which is what makes the cheap answer the right one; a
trade request, by contrast, is a Firestore transaction with a lease.

## Reading back is deliberately narrow

``recent_frames`` reads whole days at one timeframe, newest last, for
``aureon/engine/mtf.py``'s higher-timeframe biases. It takes an explicit list of market dates
rather than a range query: the caller knows which days it wants (the observer's sleep cycle
knows the trading week), a range over a string id would be lexicographic rather than temporal
across a year boundary, and a query that silently returned fewer days than asked for would
produce a quietly shorter EMA.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.enums import Timeframe
from aureon.models.market_day import MarketDay, MarketDayFrame
from aureon.storage import paths

log = logging.getLogger(__name__)


class MarketDayRepository:
    """Reads and writes ``{prefix}_market_days`` and ``{prefix}_market_day_frames``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # ── The day ──────────────────────────────────────────────────────────────

    def get_day(self, symbol: str, market_date: str) -> MarketDay | None:
        snapshot = self._client.document(
            paths.market_day_path(symbol, market_date)
        ).get()
        if not getattr(snapshot, "exists", False):
            return None
        return MarketDay.model_validate(snapshot.to_dict())

    def write_day(self, day: MarketDay, *, now: datetime | None = None) -> MarketDay:
        stamped = day.model_copy(update={"updated_at": to_utc(now or utc_now())})
        self._client.document(
            paths.market_day_path(stamped.symbol, stamped.market_date)
        ).set(stamped.model_dump(mode="json"))
        return stamped

    # ── The bars ─────────────────────────────────────────────────────────────

    def get_frame(
        self, symbol: str, market_date: str, timeframe: Timeframe
    ) -> MarketDayFrame | None:
        snapshot = self._client.document(
            paths.market_day_frame_path(symbol, market_date, timeframe)
        ).get()
        if not getattr(snapshot, "exists", False):
            return None
        return MarketDayFrame.model_validate(snapshot.to_dict())

    def write_frame(
        self, frame: MarketDayFrame, *, now: datetime | None = None
    ) -> MarketDayFrame:
        stamped = frame.model_copy(update={"updated_at": to_utc(now or utc_now())})
        self._client.document(
            paths.market_day_frame_path(
                stamped.symbol, stamped.market_date, stamped.timeframe
            )
        ).set(stamped.model_dump(mode="json"))
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

        ``complete_only`` because an unfinished day's last bar is not its last bar: feeding one
        into an aggregation would produce a daily bar whose close is not the close, which is
        the failure ``mtf._complete`` refuses one level down. A caller that genuinely wants the
        day in progress -- a chart -- asks for it by name with ``get_frame``.

        A missing day is skipped rather than raised on: the cache is an optimisation, and a
        bias built from eight of the ten days asked for is a weaker claim, not an error. The
        caller sees how many came back and ``mtf.frames_from`` refuses a timeframe with too
        little history anyway.
        """
        found: list[MarketDayFrame] = []
        for market_date in sorted(set(market_dates)):
            frame = self.get_frame(symbol, market_date, timeframe)
            if frame is None:
                continue
            if complete_only and not frame.complete:
                continue
            found.append(frame)
        return found
