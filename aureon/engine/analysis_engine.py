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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import pandas as pd

from aureon.agents.base_agent import WINDOW_COLUMNS, BaseAgent
from aureon.config.sessions import SESSION_CONFIG_VERSION, session_for
from aureon.models.detection import CandleContext, Detection, SessionContext
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.market import Candle

# Extra bars beyond the agents' stated minimum. Zero by default: any margin must be
# identical in live and replay, so it is explicit rather than incidental.
DEFAULT_WINDOW_MARGIN = 0


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
    ) -> None:
        if not agents:
            raise ValueError("AnalysisEngine needs at least one agent")
        self.agents = list(agents)
        self.account_scope = account_scope
        self.market_tz = market_tz

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

        window = self._windows.setdefault(key, deque(maxlen=self.window_size))
        window.append(candle)
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
            # Exactly this agent's stated requirement, so a hungrier sibling cannot
            # change what this agent sees.
            needed = agent.min_window()
            view = frame if len(frame) <= needed else frame.iloc[-needed:]
            detections.extend(agent.on_closed_candle(view, ctx))
        return [
            self._with_context(self._number(detection, key), tracker)
            for detection in detections
        ]

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

    def context_tracker(self, symbol: str, timeframe: Timeframe):
        """The tracker for one symbol, for ``system_state`` and ``/status`` (9B)."""
        return self._context_trackers.get((symbol, timeframe))

    def _with_context(self, detection: Detection, tracker) -> Detection:
        """Attach the profile reference and volatility of this candle's close (9B).

        Done in the engine rather than in each agent for the same reason the cross
        sequence is: an agent is a pure function of ``(window, ctx)``, and reaching for
        a day's worth of candles inside one would break the replay/live parity the whole
        engine rests on. Attaching here also guarantees every agent's detections from one
        candle carry the SAME context, computed once.
        """
        return detection.model_copy(
            update={
                "volume_profile_ref": tracker.reference(detection.price),
                "volatility": tracker.volatility(),
            }
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
