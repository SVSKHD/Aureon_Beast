"""Session trend agent (§18).

Fires once, on the first candle of a new session, describing the session that just
**ended**. Emitting mid-session would be describing something still in progress: the
high, low and close are all still moving, so the summary would be wrong and would
later be indistinguishable from a final one.

## Context-only, deliberately

``direction`` is ``None`` even though a session that closed higher has an obvious
"up" direction. A session summary describes the *past*, not a forecast, and Phase 7's
inferred-link rule matches on "same symbol, same direction, trade opened shortly
after" (§50) -- so a directional session detection would attract spurious links to
unrelated trades and quietly corrupt the review. The trend is carried as a label in
``event_key`` plus the numeric change, which loses nothing and cannot be mistaken for
a signal.

## Window size

A session is long: 8 hours is ~96 M5 bars. The agent therefore declares a large
``min_window()``, derived from its configured timeframe. Because ``AnalysisEngine``
hands every agent its own slice, needing a big window here does not change what the
EMA-cross agent sees.
"""

from __future__ import annotations

import math

import pandas as pd

from aureon.agents.base_agent import BaseAgent, validate_window
from aureon.config.sessions import SESSION_CONFIG_VERSION, sessions_for_index
from aureon.models.detection import CandleContext, Detection, IndicatorSnapshot
from aureon.models.enums import SessionName, Timeframe
from aureon.models.session import TREND_DOWN, TREND_FLAT, TREND_UP, SessionSummary
from aureon.storage.paths import session_doc_id

# Longest session in the default configuration (§18, decision 7).
DEFAULT_MAX_SESSION_HOURS = 8.0

# Below this many points of net change, a session is called flat rather than trending.
DEFAULT_FLAT_POINTS = 50.0


class SessionTrendAgent(BaseAgent):
    """Summarises each completed session (§18)."""

    agent_name = "session_trend"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        timeframe: Timeframe = Timeframe.M5,
        point: float = 0.01,
        max_session_hours: float = DEFAULT_MAX_SESSION_HOURS,
        flat_points: float = DEFAULT_FLAT_POINTS,
        include_off_session: bool = False,
    ) -> None:
        if max_session_hours <= 0:
            raise ValueError("max_session_hours must be positive")
        if point <= 0:
            raise ValueError("point must be positive")
        self.timeframe = timeframe
        self.point = point
        self.max_session_hours = max_session_hours
        self.flat_points = flat_points
        # The stretch outside every configured session is not a session; summarising
        # it would invent one. Off by default, configurable for research.
        self.include_off_session = include_off_session

    def params_snapshot(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe.value,
            "point": self.point,
            "max_session_hours": self.max_session_hours,
            "flat_points": self.flat_points,
            "include_off_session": self.include_off_session,
            "session_config_version": SESSION_CONFIG_VERSION,
            "warmup_bars": self.min_window(),
        }

    def min_window(self) -> int:
        """Enough bars to hold the longest session, plus the boundary candle."""
        bars = math.ceil(self.max_session_hours * 60 / self.timeframe.minutes)
        return bars + 2

    def on_closed_candle(self, window: pd.DataFrame, ctx: CandleContext) -> list[Detection]:
        validate_window(window)
        if ctx.timeframe is not self.timeframe:
            # Bars-per-session was computed from the configured timeframe, so a
            # mismatch would silently mis-size the window rather than fail.
            raise ValueError(
                f"{self.agent_name} is configured for {self.timeframe.value} but received "
                f"{ctx.timeframe.value}; configure one agent per timeframe"
            )
        if len(window) < 2:
            return []

        sessions = self._sessions_for(window, ctx.market_tz)
        current, previous = sessions[-1], sessions[-2]
        if current == previous:
            return []  # still inside the same session

        if previous is SessionName.OFF and not self.include_off_session:
            return []

        # The completed session is the contiguous run of bars immediately before the
        # boundary that share `previous`.
        run = self._trailing_run(sessions, previous)
        if run is None:
            # The window starts mid-session, so its true open is unknown. Reporting a
            # partial session as complete would be worse than reporting nothing --
            # this self-corrects once the window covers a full session.
            return []

        start, end = run
        block = window.iloc[start : end + 1]
        summary_values = self._summarise(block)
        trend = self._trend(summary_values["change_points"])

        return [
            self.build_detection(
                ctx=ctx,
                event_key=f"{previous.value}|{trend}",
                price=summary_values["close"],
                # Context only -- see the module docstring.
                direction=None,
                indicators=IndicatorSnapshot(extras=summary_values),
                levels={
                    "session_open": summary_values["open"],
                    "session_high": summary_values["high"],
                    "session_low": summary_values["low"],
                    "session_close": summary_values["close"],
                },
            )
        ]

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _sessions_for(self, window: pd.DataFrame, market_tz: str) -> list[SessionName]:
        """Session of every bar in the window, by its MARKET-local open time.

        Market-local, not UTC: passing UTC would shift every boundary by the broker's
        offset (§18).
        """
        # Vectorised: building a MarketTime, or even a datetime, per bar was
        # measurably the dominant cost. The result is identical.
        return sessions_for_index(window.index.tz_convert(market_tz))

    @staticmethod
    def _trailing_run(
        sessions: list[SessionName], target: SessionName
    ) -> tuple[int, int] | None:
        """Index bounds of the run of ``target`` ending just before the last bar.

        Returns ``None`` when the run reaches the start of the window, which means the
        session's real open is outside it.
        """
        end = len(sessions) - 2
        start = end
        while start >= 0 and sessions[start] == target:
            start -= 1
        if start < 0:
            return None  # truncated: the session began before the window
        return start + 1, end

    def _summarise(self, block: pd.DataFrame) -> dict[str, float]:
        open_price = float(block["open"].iloc[0])
        close_price = float(block["close"].iloc[-1])
        high = float(block["high"].max())
        low = float(block["low"].min())
        change = close_price - open_price
        return {
            "open": open_price,
            "high": high,
            "low": low,
            "close": close_price,
            "change": change,
            "change_points": change / self.point,
            "range": high - low,
            "range_points": (high - low) / self.point,
            "candle_count": float(len(block)),
        }

    def _trend(self, change_points: float) -> str:
        if abs(change_points) < self.flat_points:
            return TREND_FLAT
        return TREND_UP if change_points > 0 else TREND_DOWN


def summary_from_detection(detection: Detection) -> SessionSummary:
    """Build the ``sessions/`` document from a session-trend detection.

    Lives here, beside the agent that produced the numbers, but is called by the
    **observer** -- an agent stays pure and never writes to Firestore (CLAUDE.md), so
    the conversion and the write are separate steps.
    """
    if detection.agent_name != SessionTrendAgent.agent_name:
        raise ValueError(
            f"expected a {SessionTrendAgent.agent_name} detection, got {detection.agent_name}"
        )
    session_label, trend = detection.event_key.split("|", 1)
    extras = detection.indicators.extras
    # The session ran from its first candle's open to this boundary candle's open,
    # which is the instant the previous session's last candle closed.
    started = detection.candle_open_time.model_copy(
        update={
            "utc": detection.candle_open_time.utc
            - pd.Timedelta(
                minutes=detection.timeframe.minutes * extras["candle_count"]
            ).to_pytimedelta()
        }
    )
    market_date = started.market_date
    return SessionSummary(
        session_id=session_doc_id(market_date, session_label),
        account_scope=detection.account_scope,
        symbol=detection.symbol,
        timeframe=detection.timeframe,
        session=SessionName(session_label),
        market_date=market_date,
        session_config_version=detection.session.session_config_version,
        started_at=started,
        ended_at=detection.candle_open_time,
        open=extras["open"],
        high=extras["high"],
        low=extras["low"],
        close=extras["close"],
        trend=trend,
        change=extras["change"],
        change_points=extras["change_points"],
        range=extras["range"],
        candle_count=int(extras["candle_count"]),
    )
