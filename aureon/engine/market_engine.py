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
        on_analysis: Callable[[Candle, list[Detection]], None] | None = None,
        on_poll: Callable[[], None] | None = None,
        before_poll: Callable[[], None] | None = None,
        parked: Callable[[], bool] | None = None,
        pace: Callable[[float], float] | None = None,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    ) -> None:
        self.provider = provider
        self.engine = engine
        self.symbols = list(symbols)
        self.timeframes = list(timeframes)
        self.on_detections = on_detections
        self.on_candle_close = on_candle_close
        #: Called for EVERY closed candle with its detections, after the two callbacks above
        #: (12, T-7). Neither of those serves the setup engine: ``on_candle_close`` has no
        #: detections, and ``on_detections`` does not fire on a candle that produced none --
        #: which is most of them, and exactly the candles on which a setup expires, invalidates
        #: or comes into proximity of a level.
        self.on_analysis = on_analysis
        #: Called once per poll, after any candles were processed (9C). The observer uses
        #: it to answer price alerts from the quotes it is already reading -- on the poll
        #: clock rather than the candle clock, because "the first quote across the level"
        #: is the thing a human asked about and a five-minute granularity would answer a
        #: different question.
        self.on_poll = on_poll
        #: Called FIRST on every poll, before any stream is read (11B). The observer puts
        #: its sleep decision here: a hook that ran after the streams could only park the
        #: loop from the NEXT iteration, and a wake would lose a poll at the open.
        self.before_poll = before_poll
        #: Asked, after ``before_poll``, whether to skip the streams entirely (11B). Not a
        #: flag: the answer belongs to whoever owns the sleep cycle, and an engine holding
        #: its own copy would be a second place for it to be wrong.
        self.parked = parked
        #: Given the awake cadence, returns the wait before the next iteration (11B). The
        #: loop keeps running while asleep -- slowly -- because a loop that exited would
        #: turn the weekend into a restart.
        self.pace = pace
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
        if self.before_poll is not None:
            try:
                self.before_poll()
            except Exception:  # noqa: BLE001 - a side errand must not stop observation
                log.exception("before_poll hook failed")
        produced: list[Detection] = []
        if not self.is_parked:
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
            if self.on_analysis is not None:
                # Last, so whatever it does sees the state the two callbacks above already
                # updated -- the session extremes, the day cache and the outbox.
                self.on_analysis(candle, detections)
        return produced

    @property
    def is_parked(self) -> bool:
        """Whether the streams are skipped this poll (11B).

        Fails toward polling: a ``parked`` callable that raises leaves the engine awake,
        because observing a closed market wastes a request and missing an open one loses
        candles that no later poll will bring back.
        """
        if self.parked is None:
            return False
        try:
            return bool(self.parked())
        except Exception:  # noqa: BLE001
            log.exception("parked hook failed; staying awake")
            return False

    # ── Loop ──────────────────────────────────────────────────────────────────

    def run(self, *, poll_seconds: float = 1.0) -> None:
        """Poll until ``stop()``. Exceptions are logged and the loop continues.

        A transient provider error must not take the observer down: the cursor is
        only advanced on success, so the next poll retries the same candles.

        The wait is asked for on every iteration rather than fixed at the top, so a
        market that closes mid-loop slows this loop down without restarting it (11B).
        """
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - a poll failure must not kill the loop
                log.exception("market engine poll failed; retrying")
            self._stop.wait(self._wait(poll_seconds))

    def _wait(self, poll_seconds: float) -> float:
        if self.pace is None:
            return poll_seconds
        try:
            return float(self.pace(poll_seconds))
        except Exception:  # noqa: BLE001
            log.exception("pace hook failed; using the awake cadence")
            return poll_seconds

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()
