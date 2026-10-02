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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        parallel_streams: bool = False,
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
        # Multi-symbol deployments may analyse streams concurrently, but calls into the
        # broker/provider remain serialized. The MetaTrader5 Python module exposes one
        # terminal session and is not treated as a thread-safe parallel API.
        self.parallel_streams = parallel_streams
        self._provider_lock = threading.Lock()
        self._diagnostics_lock = threading.Lock()
        self._stream_diagnostics: dict[tuple[str, Timeframe], dict[str, object]] = {}

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
            streams = [
                (symbol, timeframe)
                for symbol in self.symbols
                for timeframe in self.timeframes
            ]
            if self.parallel_streams and len(streams) > 1:
                # One worker per stream (bounded by the configured stream count). A slow
                # Gold analysis cannot hold Silver behind it. Exceptions are isolated to
                # their stream and the next poll retries because its cursor only advances
                # after a candle is accepted by that stream.
                with ThreadPoolExecutor(
                    max_workers=len(streams),
                    thread_name_prefix="aureon-stream",
                ) as pool:
                    futures = {
                        pool.submit(self._poll_stream, symbol, timeframe): (symbol, timeframe)
                        for symbol, timeframe in streams
                    }
                    for future in as_completed(futures):
                        symbol, timeframe = futures[future]
                        try:
                            produced.extend(future.result())
                        except Exception:  # noqa: BLE001
                            log.exception(
                                "stream poll failed for %s/%s; sibling streams continue",
                                symbol,
                                timeframe.value,
                            )
            else:
                for symbol, timeframe in streams:
                    produced.extend(self._poll_stream(symbol, timeframe))
        if self.on_poll is not None:
            try:
                self.on_poll()
            except Exception:  # noqa: BLE001 - a side errand must not stop observation
                log.exception("on_poll hook failed")
        return produced

    def _poll_stream(self, symbol: str, timeframe: Timeframe) -> list[Detection]:
        key = (symbol, timeframe)
        started = time.perf_counter()
        with self._diagnostics_lock:
            self._stream_diagnostics[key] = {
                **self._stream_diagnostics.get(key, {}),
                "status": "RUNNING",
                "started_at": datetime.now().astimezone().isoformat(),
            }
        # Provider calls are intentionally serialized even when stream analysis is
        # parallel. This keeps one MT5 terminal/session and avoids relying on vendor API
        # thread-safety while still preventing Gold analysis from delaying Silver.
        with self._provider_lock:
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
                self._finish_diagnostic(key, started, cursor)
                return []  # nothing new has closed
            start = cursor + timedelta(minutes=timeframe.minutes)

        end = newest_closed_open + timedelta(minutes=timeframe.minutes)
        with self._provider_lock:
            candles = self.provider.get_closed_candles(symbol, timeframe, start, end)
        if not candles:
            self._finish_diagnostic(key, started, cursor)
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
        self._finish_diagnostic(key, started, self._cursor.get(key))
        return produced

    def _finish_diagnostic(
        self,
        key: tuple[str, Timeframe],
        started: float,
        last_open: datetime | None,
    ) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self._diagnostics_lock:
            self._stream_diagnostics[key] = {
                "status": "RUNNING",
                "processing_ms": round(elapsed_ms, 2),
                "last_completed_at": datetime.now().astimezone().isoformat(),
                "last_candle_open": last_open.isoformat() if last_open is not None else None,
            }

    def diagnostics(self) -> dict[str, object]:
        """Thread-safe observer diagnostics suitable for a heartbeat detail payload."""
        with self._diagnostics_lock:
            streams = {
                f"{symbol}/{timeframe.value}": dict(detail)
                for (symbol, timeframe), detail in self._stream_diagnostics.items()
            }
        return {
            "parallel_enabled": self.parallel_streams,
            "worker_count": len(self.symbols) * len(self.timeframes),
            "provider_access": "serialized",
            "streams": streams,
        }

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
