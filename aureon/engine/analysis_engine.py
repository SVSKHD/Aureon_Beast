"""The analysis engine: one code path for live and replay (§79, §82).

This is the same class in both directions, and that is the point. Parity is not
achieved by carefully keeping two implementations in step -- it is achieved by
there only being one. ``MarketEngine`` feeds it candles from a live provider,
``ReplayEngine`` feeds it candles from a file, and neither adds any analysis of its
own.

## Why the window size is fixed

Recursive indicators (EMA, Wilder's RSI) depend on how much history they are given,
not only on recent bars -- a 30-bar window and a 200-bar window disagree on EMA(21)
by enough to move a crossover onto a different candle. So the window length is
derived once, from the agents' own stated requirements, and used identically in both
directions. It is a correctness parameter, not a tuning knob.

## Why each agent gets its OWN slice

The engine holds one buffer sized to the hungriest agent, but hands each agent exactly
``agent.min_window()`` bars -- not the whole buffer. Without this, window dependence
would leak between agents: adding a session agent that needs ~96 bars would widen the
shared window and thereby change the EMA-cross agent's detections, so introducing a
new agent would silently rewrite an existing agent's history. Slicing per agent makes
each one's output depend only on its own configuration, which is what lets agents be
added in Part B without invalidating Part A's recorded baseline.

## Counters

``sequence_today`` and ``sequence_session`` count **candles** within the broker
trading day and within the session (decision 29). The engine has to build a context
before knowing whether any agent will emit, so a per-agent *detection* count cannot
be known at that moment -- and it is recoverable by querying ``detections`` anyway,
whereas a candle index is deterministic and reproducible under replay.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from statistics import median

import pandas as pd

from aureon.agents.base_agent import WINDOW_COLUMNS, BaseAgent
from aureon.config.sessions import SESSION_CONFIG_VERSION, session_for
from aureon.models.detection import CandleContext, Detection, SessionContext
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.market import Candle
from aureon.services.agent_highway import AgentHighway

# Extra bars beyond the agents' stated minimum. Zero by default: any margin must be
# identical in live and replay, so it is explicit rather than incidental.
DEFAULT_WINDOW_MARGIN = 0


@dataclass(frozen=True)
class SkippedCandle:
    """A bar the engine refused to analyse, and why (11B).

    Kept as a record rather than dropped silently: "no detections on Friday evening" and
    "the engine threw a bar away" look identical in the detections collection, and only one
    of them is expected.
    """

    symbol: str
    timeframe: Timeframe
    open_time: datetime
    close_time: datetime
    reason: str


#: How many skips are remembered. A bounded list, because the observer runs for weeks and
#: this is diagnostic: two a week is the expected rate, and a burst is what matters.
MAX_REMEMBERED_SKIPS = 20


#: A "cross" is an ema_cross detection carrying a direction. Named explicitly rather
#: than inferred from the event key alone so a future agent that happens to emit
#: "bullish" cannot silently start incrementing the EMA cross counters.
CROSS_AGENT_NAME = "ema_cross"
CROSS_EVENT_KEYS = frozenset({"bullish", "bearish"})


def is_cross(detection: Detection) -> bool:
    """Whether a detection is an EMA cross, for counting and numbering (§13)."""
    return (
        detection.agent_name == CROSS_AGENT_NAME
        and detection.event_key in CROSS_EVENT_KEYS
        and detection.direction is not None
    )


@dataclass
class CrossCounts:
    """EMA crosses seen so far today and this session (§13, §66).

    Six numbers rather than two, because "4 crosses today" and "3 up, 1 down" answer
    different questions and a reader cannot derive the second from the first. Reset on
    the **market** clock: a broker day rolls at market midnight, not UTC midnight, and
    a counter that rolled at the wrong hour would put the evening's crosses under the
    wrong date with nothing in the number to show it.
    """

    total_today: int = 0
    total_session: int = 0
    bullish_today: int = 0
    bearish_today: int = 0
    bullish_session: int = 0
    bearish_session: int = 0

    def record(self, direction: Direction | None) -> None:
        self.total_today += 1
        self.total_session += 1
        if direction is Direction.BUY:
            self.bullish_today += 1
            self.bullish_session += 1
        elif direction is Direction.SELL:
            self.bearish_today += 1
            self.bearish_session += 1

    def reset_day(self) -> None:
        """A new broker day resets both scopes: a day contains its sessions."""
        self.total_today = 0
        self.bullish_today = 0
        self.bearish_today = 0
        self.reset_session()

    def reset_session(self) -> None:
        self.total_session = 0
        self.bullish_session = 0
        self.bearish_session = 0

    def as_state(self) -> dict[str, int]:
        """The six fields as ``system_state`` names them (§66)."""
        return {
            "ema_crosses_today": self.total_today,
            "ema_crosses_session": self.total_session,
            "bullish_crosses_today": self.bullish_today,
            "bearish_crosses_today": self.bearish_today,
            "bullish_crosses_session": self.bullish_session,
            "bearish_crosses_session": self.bearish_session,
        }


#: How many bars "the recent median tick volume" is taken over. One session of M5, so
#: "expansion" means expansion against today rather than against the whole window -- a median
#: over 600 bars would be yesterday's number and would call an ordinary bar expansive on a quiet
#: morning.
MEDIAN_VOLUME_BARS = 96

#: The RSI period the rsi agent uses. Named here so the setup engine's read and the agent's
#: cannot drift apart; if it ever becomes configurable, both come from the same place.
RSI_PERIOD = 14


@dataclass(frozen=True)
class IndicatorRead:
    """What the indicators said at one closed candle, including the bar before it.

    Every field optional, because a window shorter than the slow period genuinely has no EMA and
    a read that invented one would hand the setup engine a number with no history behind it.
    """

    ema_fast: float | None = None
    ema_slow: float | None = None
    previous_ema_fast: float | None = None
    previous_ema_slow: float | None = None
    rsi: float | None = None
    previous_rsi: float | None = None
    atr: float | None = None
    median_tick_volume: float | None = None


def _last(series, *, back: int = 0) -> float | None:
    """The value ``back`` bars from the end, or ``None`` if it is not there or not a number."""
    import math

    if len(series) <= back:
        return None
    value = series.iloc[-1 - back]
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return float(value)


class AnalysisEngine:
    """Holds rolling windows and runs agents over closed candles."""

    def __init__(
        self,
        agents: Sequence[BaseAgent],
        *,
        account_scope: str,
        market_tz: str,
        window_size: int | None = None,
        window_margin: int = DEFAULT_WINDOW_MARGIN,
        gap_guard: Callable[[datetime, datetime], datetime | None] | None = None,
        mtf_bars: int = 0,
        mtf_periods: tuple[int, int] | None = None,
        agent_highway: AgentHighway | None = None,
    ) -> None:
        if not agents:
            raise ValueError("AnalysisEngine needs at least one agent")
        self.agents = list(agents)
        self.account_scope = account_scope
        self.market_tz = market_tz
        self.agent_highway = agent_highway or AgentHighway()
        #: Given a bar's start and end, returns the weekly close it straddles, or None
        #: (11B). Injected rather than imported: the engine stays free of the services
        #: package, and `WeeklySchedule.close_spanned_by` is what the observer passes.
        #: Optional, because an engine built without one analyses every bar it is given --
        #: which is what the replay fixtures and the agent unit tests want, since they feed
        #: contiguous synthetic candles and a schedule would be one more thing to keep in
        #: step.
        self.gap_guard = gap_guard
        #: Bars refused by the gap guard, newest last. See ``SkippedCandle``.
        self.skipped: list[SkippedCandle] = []
        #: 11D. A LONGER M5 tail than ``window_size``, for the higher timeframes.
        #:
        #: Separate from the analysis window on purpose. The window is a correctness
        #: parameter -- it is fixed at the hungriest agent's stated requirement so that adding
        #: an agent cannot change another's detections -- and widening it to reach H4 would
        #: silently rewrite every agent's history. This buffer feeds nothing but the MTF
        #: context, so its length is a coverage choice rather than a correctness one.
        #:
        #: Zero disables it, and that is the default: an engine that was not asked for MTF
        #: context keeps exactly the memory it had before 11D.
        self.mtf_bars = mtf_bars
        self.mtf_periods = mtf_periods
        self._mtf_tail: dict[tuple[str, Timeframe], deque[Candle]] = {}
        #: This candle's frames, so ``_with_context`` can compute one alignment per detection
        #: without re-aggregating. Set by ``_mtf_context`` on every candle, including to an
        #: empty list, so a stale set from an earlier candle can never be read.
        self._frames_cache: list = []

        required = max(agent.min_window() for agent in self.agents)
        self.window_size = window_size if window_size is not None else required + window_margin
        if self.window_size < required:
            # Silently accepting a short window would hand agents too little
            # history and produce subtly wrong indicators rather than an error.
            raise ValueError(
                f"window_size {self.window_size} is below the {required} bars required by "
                f"{[a.agent_name for a in self.agents]}"
            )

        #: Volume-profile and volatility context, one tracker per (symbol, timeframe)
        #: (9B). Built on first sight of a symbol, because the engine is handed candles
        #: rather than a symbol list and a tracker needs that symbol's tuning.
        self._context_trackers: dict[tuple[str, Timeframe], object] = {}
        self._windows: dict[tuple[str, Timeframe], deque[Candle]] = {}
        # Candle counters, reset on a new broker day / session.
        self._day: dict[tuple[str, Timeframe], str] = {}
        self._session: dict[tuple[str, Timeframe], SessionName] = {}
        self._count_today: dict[tuple[str, Timeframe], int] = {}
        self._count_session: dict[tuple[str, Timeframe], int] = {}
        self._last_open: dict[tuple[str, Timeframe], pd.Timestamp] = {}
        # Cross counters, reset on the same market-clock boundaries.
        self._crosses: dict[tuple[str, Timeframe], CrossCounts] = {}

    # ── Feeding ───────────────────────────────────────────────────────────────

    def on_closed_candle(self, candle: Candle) -> list[Detection]:
        """Append a closed candle and run every agent over the resulting window.

        Re-feeding a candle already seen is ignored rather than appended: the
        observer's startup replays from its last processed candle and may overlap by
        one, and appending a duplicate would shift the whole window and change every
        indicator.
        """
        key = (candle.symbol, candle.timeframe)
        opened = pd.Timestamp(candle.open_time.utc)

        last = self._last_open.get(key)
        if last is not None and opened <= last:
            return []

        spanning = self._spans_a_close(candle)
        if spanning is not None:
            # Recorded, and NOT appended. Appending would poison the window: the bar's
            # range is the weekend gap, so every EMA for the next window_size bars would
            # be computed from a two-day move that involved no trading. Excluding it
            # leaves the window contiguous in MARKET time, which is what the indicators
            # are defined over and what the replay fixtures already do.
            self._remember_skip(spanning)
            self._last_open[key] = opened
            return []

        window = self._windows.setdefault(key, deque(maxlen=self.window_size))
        window.append(candle)
        if self.mtf_bars:
            self._mtf_tail.setdefault(key, deque(maxlen=self.mtf_bars)).append(candle)
        self._last_open[key] = opened

        ctx = self._context(candle, key)
        frame = self._frame(key)

        # Before the agents run, so the context a detection records includes the candle
        # that produced it -- which has closed, and is therefore part of its own moment
        # rather than hindsight (9B).
        tracker = self._tracker(key)
        tracker.observe(candle)

        detections: list[Detection] = []
        for agent in self.agents:
            # Every agent travels through its own bridge. An exception is converted into
            # health state and the siblings continue on this candle.
            needed = agent.min_window()
            view = frame if len(frame) <= needed else frame.iloc[-needed:]
            bridge = self.agent_highway.bridge(agent.agent_name)
            result, emitted = bridge.call(agent.on_closed_candle, view, ctx)
            if not result.ok:
                continue
            batch = list(emitted or ())
            detections.extend(batch)
            self.agent_highway.publish(
                topic=f"agent.output.{agent.agent_name}",
                source_agent=agent.agent_name,
                symbol=candle.symbol,
                timeframe=candle.timeframe.value,
                observed_at=candle.close_time,
                payload={
                    "detection_count": len(batch),
                    "detection_ids": [one.detection_id for one in batch],
                },
            )
        mtf = self._mtf_context(key)
        return [
            self._with_context(self._number(detection, key), tracker, mtf)
            for detection in detections
        ]

    def _spans_a_close(self, candle: Candle) -> SkippedCandle | None:
        """Whether this bar straddles a weekly close, and the record if it does."""
        if self.gap_guard is None:
            return None
        start, end = candle.open_time.utc, candle.close_time
        boundary = self.gap_guard(start, end)
        if boundary is None:
            return None
        return SkippedCandle(
            symbol=candle.symbol,
            timeframe=candle.timeframe,
            open_time=start,
            close_time=end,
            reason=f"bar spans the weekly close at {boundary.isoformat()}",
        )

    def _remember_skip(self, skipped: SkippedCandle) -> None:
        self.skipped.append(skipped)
        del self.skipped[:-MAX_REMEMBERED_SKIPS]

    def _tracker(self, key: tuple[str, Timeframe]):
        """This symbol's context tracker, built on first sight (9B)."""
        existing = self._context_trackers.get(key)
        if existing is not None:
            return existing
        from aureon.config.symbol_tuning import tuning_for
        from aureon.engine.market_context import MarketContextTracker

        symbol, timeframe = key
        tracker = MarketContextTracker(
            symbol=symbol, timeframe=timeframe, tuning=tuning_for(symbol)
        )
        self._context_trackers[key] = tracker
        return tracker

    def mtf_context(self, symbol: str, timeframe: Timeframe):
        """The higher-timeframe reads for one stream, for ``system_state`` (11D).

        The public read of ``_mtf_context``. No alignment on it: alignment needs a direction
        and ``system_state`` describes a market rather than a signal, so publishing one would
        be an answer to a question nobody asked.
        """
        return self._mtf_context((symbol, timeframe))

    def context_tracker(self, symbol: str, timeframe: Timeframe):
        """The tracker for one symbol, for ``system_state`` and ``/status`` (9B)."""
        return self._context_trackers.get((symbol, timeframe))

    def _with_context(self, detection: Detection, tracker, mtf=None) -> Detection:
        """Attach the profile reference, volatility and higher-timeframe reads (9B, 11D).

        Done in the engine rather than in each agent for the same reason the cross
        sequence is: an agent is a pure function of ``(window, ctx)``, and reaching for
        a day's worth of candles inside one would break the replay/live parity the whole
        engine rests on. Attaching here also guarantees every agent's detections from one
        candle carry the SAME context, computed once.

        The MTF context is computed once per candle and shared, not once per detection: it
        depends only on the bars, so deriving it per agent would be the same answer at N times
        the cost -- and if it ever were not the same answer, two agents on one candle would
        disagree about what H1 was doing.
        """
        alignment = None
        if mtf is not None:
            from aureon.engine.mtf import alignment as align

            alignment = mtf.model_copy(
                update={"alignment": align(self._frames_cache, detection.direction)}
            )
        return detection.model_copy(
            update={
                "volume_profile_ref": tracker.reference(detection.price),
                "volatility": tracker.volatility(),
                "mtf": alignment,
            }
        )

    def indicator_read(self, symbol: str, timeframe: Timeframe) -> IndicatorRead:
        """The indicator values at the last closed candle in this stream (12, T-7).

        Exists because the setup engine has to run on EVERY closed candle, including the ones
        that produce no detection -- an expiry, an invalidation and a proximity all happen with
        nothing detected. Reading the values off a detection therefore does not work: on a quiet
        candle there is nothing to read them off, and using the last detection's values would
        hand the setup engine numbers from ten minutes ago.

        Computed from this engine's own window with this engine's own periods, so the setup
        engine and the agents cannot disagree about what EMA(20) is. The previous bar's values
        come along because two of the four families are about a CHANGE (the fast slope turning,
        RSI crossing 50), and a change needs two readings.
        """
        from aureon.engine.indicators import ema
        from aureon.engine.indicators import rsi as rsi_series
        from aureon.engine.volatility import atr

        key = (symbol, timeframe)
        window = list(self._windows.get(key, ()))
        if not window:
            return IndicatorRead()

        frame = self._frame(key)
        fast_period, slow_period = self.mtf_periods or (20, 50)
        read = IndicatorRead(atr=atr(window))
        closes = frame["close"]
        if len(closes) > slow_period:
            fast = ema(closes, fast_period)
            slow = ema(closes, slow_period)
            read = replace(
                read,
                ema_fast=_last(fast),
                ema_slow=_last(slow),
                previous_ema_fast=_last(fast, back=1),
                previous_ema_slow=_last(slow, back=1),
            )
        if len(closes) > RSI_PERIOD:
            values = rsi_series(closes, RSI_PERIOD)
            read = replace(
                read, rsi=_last(values), previous_rsi=_last(values, back=1)
            )
        volumes = [candle.tick_volume for candle in window[-MEDIAN_VOLUME_BARS:]]
        if volumes:
            read = replace(read, median_tick_volume=float(median(volumes)))
        return read

    def _mtf_context(self, key: tuple[str, Timeframe]):
        """The higher-timeframe reads at this candle's close, or ``None`` (11D).

        ``None`` when no MTF tail was asked for, when the source stream is not M5, or when no
        timeframe has enough aggregated history to seed an EMA. The last of those is not
        "flat": a detection with no context and one whose H4 was sideways are different facts,
        and ``mtf`` being ``None`` says the first.

        The alignment is filled in per detection by ``_with_context``, because it depends on
        the detection's DIRECTION and the reads do not. Computing the reads once and the
        alignment N times is the cheap half of the split.
        """
        from aureon.engine.mtf import SOURCE, frames_from
        from aureon.models.mtf import MtfContext, TimeframeRead

        self._frames_cache = []
        if not self.mtf_bars or self.mtf_periods is None:
            return None
        symbol, timeframe = key
        if timeframe is not SOURCE:
            # Only the M5 stream aggregates upward. An H1 stream asked for its own higher
            # timeframes would need H1 bars it does not keep, and silently reporting M5's
            # answer for it would be worse than reporting none.
            return None
        tail = self._mtf_tail.get(key)
        if not tail:
            return None
        fast, slow = self.mtf_periods
        frames = frames_from(list(tail), fast=fast, slow=slow, market_tz=self.market_tz)
        if not frames:
            return None
        self._frames_cache = frames
        return MtfContext(
            reads=tuple(
                TimeframeRead(
                    timeframe=frame.timeframe,
                    at=frame.at,
                    ema_fast=frame.ema_fast,
                    ema_slow=frame.ema_slow,
                    close=frame.close,
                    bias=frame.bias,
                )
                for frame in frames
            ),
            ema_fast_period=fast,
            ema_slow_period=slow,
        )

    def _number(self, detection: Detection, key: tuple[str, Timeframe]) -> Detection:
        """Give a cross detection its ordinal rather than the candle index (§13).

        On a cross, ``sequence_today`` / ``sequence_session`` answer "which cross is
        this?" -- 1st, 2nd, 3rd of the day and of the session. That is the number a
        human reads on /status and in a review; the candle index is an implementation
        detail nobody asked for. Every other agent keeps the candle counters, where
        the index is the meaningful ordinal.

        Done HERE rather than in the agent because an agent is a pure function of
        ``(window, ctx)`` -- counting across candles is state, and state in an agent
        would break the replay/live parity contract the whole engine rests on.

        The id is unaffected: §12 does not include the sequence, so re-numbering
        cannot re-key a detection.
        """
        if not is_cross(detection):
            return detection
        counts = self._crosses.setdefault(key, CrossCounts())
        counts.record(detection.direction)
        return detection.model_copy(
            update={
                "sequence_today": counts.total_today,
                "sequence_session": counts.total_session,
            }
        )

    def cross_counts(self, symbol: str, timeframe: Timeframe) -> CrossCounts:
        """Current cross tallies for a symbol, for ``system_state`` (§66)."""
        return self._crosses.get((symbol, timeframe), CrossCounts())

    def feed(self, candles: Iterable[Candle]) -> list[Detection]:
        """Feed many candles in order, collecting every detection."""
        out: list[Detection] = []
        for candle in candles:
            out.extend(self.on_closed_candle(candle))
        return out

    # ── Context and window ────────────────────────────────────────────────────

    def _context(self, candle: Candle, key: tuple[str, Timeframe]) -> CandleContext:
        market_date = candle.open_time.market_date
        session = session_for(candle.open_time.market)

        # Broker day rollover resets both counters; a session change resets only the
        # session counter.
        counts = self._crosses.setdefault(key, CrossCounts())
        if self._day.get(key) != market_date:
            self._day[key] = market_date
            self._count_today[key] = 0
            self._session[key] = session
            self._count_session[key] = 0
            counts.reset_day()
        if self._session.get(key) != session:
            self._session[key] = session
            self._count_session[key] = 0
            counts.reset_session()

        self._count_today[key] += 1
        self._count_session[key] += 1

        return CandleContext(
            account_scope=self.account_scope,
            symbol=candle.symbol,
            timeframe=candle.timeframe,
            market_tz=self.market_tz,
            closed_at=candle.open_time.model_copy(update={"utc": candle.close_time}),
            candle_open_time=candle.open_time,
            session=SessionContext(
                session=session, session_config_version=SESSION_CONFIG_VERSION
            ),
            sequence_today=self._count_today[key],
            sequence_session=self._count_session[key],
        )

    def _frame(self, key: tuple[str, Timeframe]) -> pd.DataFrame:
        """The rolling window as the DataFrame agents expect."""
        candles = list(self._windows[key])
        index = pd.DatetimeIndex(
            [pd.Timestamp(c.open_time.utc) for c in candles], name="open_time"
        )
        data = {
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "tick_volume": [c.tick_volume for c in candles],
            "real_volume": [c.real_volume for c in candles],
        }
        frame = pd.DataFrame(data, index=index, columns=list(WINDOW_COLUMNS))
        return frame

    # ── Introspection ─────────────────────────────────────────────────────────

    def window(self, symbol: str, timeframe: Timeframe):
        """The rolling window as a DataFrame, for a caller that needs the same bars the agents saw.

        Public because the setup engine's levels must come from THIS window: ``LevelTracker`` is a
        pure function of its input, so a caller with its own tracker and this frame derives
        exactly the levels the agents derived. A caller that fetched its own bars would derive
        pivots from a different window, and a setup could then anchor to a level no detection ever
        saw.
        """
        key = (symbol, timeframe)
        if key not in self._windows:
            import pandas as pd

            return pd.DataFrame(columns=list(WINDOW_COLUMNS))
        return self._frame(key)

    def window_length(self, symbol: str, timeframe: Timeframe) -> int:
        return len(self._windows.get((symbol, timeframe), ()))

    def last_processed(self, symbol: str, timeframe: Timeframe) -> pd.Timestamp | None:
        return self._last_open.get((symbol, timeframe))

    def reset(self) -> None:
        """Drop all state. Used between independent replays in one process."""
        self._windows.clear()
        self._day.clear()
        self._session.clear()
        self._count_today.clear()
        self._count_session.clear()
        self._last_open.clear()
        self._crosses.clear()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        names = ", ".join(a.agent_name for a in self.agents)
        return f"AnalysisEngine(window={self.window_size}, agents=[{names}])"
