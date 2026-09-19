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

import pandas as pd

from aureon.agents.base_agent import WINDOW_COLUMNS, BaseAgent
from aureon.config.sessions import SESSION_CONFIG_VERSION, session_for
from aureon.models.detection import CandleContext, Detection, SessionContext
from aureon.models.enums import SessionName, Timeframe
from aureon.models.market import Candle

# Extra bars beyond the agents' stated minimum. Zero by default: any margin must be
# identical in live and replay, so it is explicit rather than incidental.
DEFAULT_WINDOW_MARGIN = 0


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

        self._windows: dict[tuple[str, Timeframe], deque[Candle]] = {}
        # Candle counters, reset on a new broker day / session.
        self._day: dict[tuple[str, Timeframe], str] = {}
        self._session: dict[tuple[str, Timeframe], SessionName] = {}
        self._count_today: dict[tuple[str, Timeframe], int] = {}
        self._count_session: dict[tuple[str, Timeframe], int] = {}
        self._last_open: dict[tuple[str, Timeframe], pd.Timestamp] = {}

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

        detections: list[Detection] = []
        for agent in self.agents:
            produced = agent.on_closed_candle(frame, ctx)
            for detection in produced:
                detections.append(detection)
        return detections

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
        if self._day.get(key) != market_date:
            self._day[key] = market_date
            self._count_today[key] = 0
            self._session[key] = session
            self._count_session[key] = 0
        if self._session.get(key) != session:
            self._session[key] = session
            self._count_session[key] = 0

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

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        names = ", ".join(a.agent_name for a in self.agents)
        return f"AnalysisEngine(window={self.window_size}, agents=[{names}])"
