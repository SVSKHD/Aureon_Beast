"""The live observation loop (§79).

Polls a provider for newly-closed candles, hands them to the same
``AnalysisEngine`` replay uses, and passes the resulting detections to a sink
(normally the durable outbox).

## Why it polls on a boundary plus a grace period

A candle is closed once its end time passes, but a terminal may still be finalising
the bar at that exact instant. Reading too early can return a bar whose high or low
then changes -- which would produce a detection encoding a price that never finally
existed, and which replay could never reproduce. So the loop waits
``grace_seconds`` past the boundary. This is also why it never asks for the forming
bar at all.

## What it does NOT do

It does not trade. There is no broker here, `aureon.engine` may not import
`aureon.execution` (a boundary test enforces it), and a detection carries no field
an executor reads. Detections go to the outbox; a human decides what, if anything,
happens next.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from aureon.data.base_provider import BaseMarketDataProvider, floor_to_timeframe
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.models.detection import Detection
from aureon.models.enums import Timeframe
from aureon.models.market import Candle

log = logging.getLogger(__name__)

# Seconds past a candle boundary before the bar is trusted to be final.
DEFAULT_GRACE_SECONDS = 2.0

# How far back to look for candles missed while the process was down or stalled.
DEFAULT_LOOKBACK_BARS = 500


class MarketEngine:
    """Live candle polling and dispatch."""

    def __init__(
        self,
        provider: BaseMarketDataProvider,
        engine: AnalysisEngine,
        *,
        symbols: Sequence[str],
        timeframes: Sequence[Timeframe],
        on_detections: Callable[[list[Detection]], None] | None = None,
        on_candle_close: Callable[[Candle], None] | None = None,
        on_poll: Callable[[], None] | None = None,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    ) -> None:
        self.provider = provider
        self.engine = engine
        self.symbols = list(symbols)
        self.timeframes = list(timeframes)
        self.on_detections = on_detections
        self.on_candle_close = on_candle_close
        #: Called once per poll, after any candles were processed (9C). The observer uses
        #: it to answer price alerts from the quotes it is already reading -- on the poll
        #: clock rather than the candle clock, because "the first quote across the level"
        #: is the thing a human asked about and a five-minute granularity would answer a
        #: different question.
        self.on_poll = on_poll
        self.grace_seconds = grace_seconds
        self.lookback_bars = lookback_bars

        # Highest candle open already processed, per stream. Seeded from durable
        # state at startup so a restart neither reprocesses nor skips.
        self._cursor: dict[tuple[str, Timeframe], datetime] = {}
        self._stop = threading.Event()

    # ── Cursor ────────────────────────────────────────────────────────────────

    def seed_cursor(self, symbol: str, timeframe: Timeframe, last_open: datetime) -> None:
        """Set the last processed candle open, from persisted observer state (§75)."""
        self._cursor[(symbol, timeframe)] = last_open

    def cursor(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        return self._cursor.get((symbol, timeframe))

    # ── Polling ───────────────────────────────────────────────────────────────

    def poll_once(self) -> list[Detection]:
        """Fetch and process any newly-closed candles across all streams.

        Safe to call as often as desired: candles already processed are filtered by
        the cursor, and ``AnalysisEngine`` independently ignores a duplicate.
        """
        produced: list[Detection] = []
        for symbol in self.symbols:
            for timeframe in self.timeframes:
                produced.extend(self._poll_stream(symbol, timeframe))
        if self.on_poll is not None:
            try:
                self.on_poll()
            except Exception:  # noqa: BLE001 - a side errand must not stop observation
                log.exception("on_poll hook failed")
        return produced

    def _poll_stream(self, symbol: str, timeframe: Timeframe) -> list[Detection]:
        key = (symbol, timeframe)
        now = self.provider.now_utc()
        # The newest bar that is certainly final: floor to the boundary using a
        # clock pulled back by the grace period, then step back one full bar.
        boundary = floor_to_timeframe(
            now - timedelta(seconds=self.grace_seconds), timeframe
        )
        newest_closed_open = boundary - timedelta(minutes=timeframe.minutes)

        cursor = self._cursor.get(key)
        if cursor is None:
            start = newest_closed_open - timedelta(
                minutes=timeframe.minutes * self.lookback_bars
            )
        else:
            if cursor >= newest_closed_open:
                return []  # nothing new has closed
            start = cursor + timedelta(minutes=timeframe.minutes)

        end = newest_closed_open + timedelta(minutes=timeframe.minutes)
        candles = self.provider.get_closed_candles(symbol, timeframe, start, end)
        if not candles:
            return []

        produced: list[Detection] = []
        for candle in candles:
            detections = self.engine.on_closed_candle(candle)
            self._cursor[key] = candle.open_time.utc
            if self.on_candle_close is not None:
                self.on_candle_close(candle)
            if detections:
                produced.extend(detections)
                if self.on_detections is not None:
                    # Handed over per candle, not per poll: a crash mid-poll must
                    # not lose detections from candles already processed.
                    self.on_detections(detections)
        return produced

    # ── Loop ──────────────────────────────────────────────────────────────────

    def run(self, *, poll_seconds: float = 1.0) -> None:
        """Poll until ``stop()``. Exceptions are logged and the loop continues.

        A transient provider error must not take the observer down: the cursor is
        only advanced on success, so the next poll retries the same candles.
        """
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - a poll failure must not kill the loop
                log.exception("market engine poll failed; retrying")
            self._stop.wait(poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()
