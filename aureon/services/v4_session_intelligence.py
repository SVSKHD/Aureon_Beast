"""Aureon V4 independent session intelligence (TODO 043-050).

V4 does not force a candle into one exclusive session.  Asia, London and New York
are evaluated independently from the configured windows, so the London/New York
overlap is represented explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Iterable

from aureon.config.sessions import SESSION_WINDOWS
from aureon.models.detection import Detection
from aureon.models.ema_journey_v4 import (
    V4SessionCloseOutcome,
    V4SessionContext,
    V4SessionLearningRow,
    V4SessionOpenSnapshot,
    V4SessionPhase,
)
from aureon.models.enums import SessionName
from aureon.models.market import Candle

_SESSIONS = (
    SessionName.ASIA,
    SessionName.LONDON,
    SessionName.NEW_YORK,
)


def active_sessions(market_dt: datetime) -> tuple[SessionName, ...]:
    """All real sessions containing this market-local timestamp."""
    moment = market_dt.time()
    return tuple(
        name
        for name in _SESSIONS
        if SESSION_WINDOWS[name].contains(moment)
    )


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def session_phase(name: SessionName, market_dt: datetime) -> V4SessionPhase:
    """EARLY/MID/LATE based on progress through that session's own window."""
    window = SESSION_WINDOWS[name]
    start = _minutes(window.start)
    end = _minutes(window.end)
    now = market_dt.hour * 60 + market_dt.minute
    if end <= start:
        end += 24 * 60
        if now < start:
            now += 24 * 60
    span = max(1, end - start)
    progress = max(0.0, min(0.999999, (now - start) / span))
    if progress < 1 / 3:
        return V4SessionPhase.EARLY
    if progress < 2 / 3:
        return V4SessionPhase.MID
    return V4SessionPhase.LATE


def independent_session_context(market_dt: datetime) -> V4SessionContext:
    sessions = active_sessions(market_dt)
    overlap = (
        SessionName.LONDON in sessions
        and SessionName.NEW_YORK in sessions
    )
    return V4SessionContext(
        active_sessions=tuple(name.value for name in sessions),
        phases={name.value: session_phase(name, market_dt) for name in sessions},
        overlap=overlap,
        overlap_name="london_new_york" if overlap else None,
    )


@dataclass
class _LiveSession:
    opened_at: datetime
    open_price: float
    high: float
    low: float
    close: float
    candle_count: int = 0
    ema20_50_crosses: int = 0
    ema200_crosses: int = 0
    journey_count: int = 0


class V4SessionTracker:
    """Independent open/close state for Asia, London and New York."""

    def __init__(self) -> None:
        self._active: dict[tuple[str, str, SessionName], _LiveSession] = {}
        self.opens: list[V4SessionOpenSnapshot] = []
        self.closes: list[V4SessionCloseOutcome] = []
        self._last_close: dict[tuple[str, str, SessionName], V4SessionCloseOutcome] = {}

    def on_closed_candle(
        self,
        candle: Candle,
        *,
        detections: Iterable[Detection] = (),
        ema20: float | None = None,
        ema50: float | None = None,
        ema200: float | None = None,
        trend: str = "UNKNOWN",
        journey_started: bool = False,
    ) -> tuple[list[V4SessionOpenSnapshot], list[V4SessionCloseOutcome]]:
        market_dt = candle.open_time.market
        now_active = set(active_sessions(market_dt))
        opened: list[V4SessionOpenSnapshot] = []
        closed: list[V4SessionCloseOutcome] = []

        stream = (candle.symbol, candle.timeframe.value)
        # Close any independently tracked session whose window no longer contains
        # this candle. The prior candle's close is the session close.
        for name in _SESSIONS:
            key = (*stream, name)
            state = self._active.get(key)
            if state is not None and name not in now_active:
                outcome = V4SessionCloseOutcome(
                    symbol=candle.symbol,
                    timeframe=candle.timeframe,
                    session=name.value,
                    opened_at=state.opened_at,
                    closed_at=candle.open_time.utc,
                    open_price=state.open_price,
                    high=state.high,
                    low=state.low,
                    close_price=state.close,
                    change=state.close - state.open_price,
                    range=state.high - state.low,
                    candle_count=state.candle_count,
                    ema20_50_crosses=state.ema20_50_crosses,
                    ema200_crosses=state.ema200_crosses,
                    journey_count=state.journey_count,
                )
                self.closes.append(outcome)
                self._last_close[key] = outcome
                closed.append(outcome)
                del self._active[key]

        for name in now_active:
            key = (*stream, name)
            state = self._active.get(key)
            if state is None:
                # Only claim a true session open when this candle is the configured
                # boundary. Starting Aureon mid-session must not fabricate an open.
                window = SESSION_WINDOWS[name]
                if market_dt.time().replace(second=0, microsecond=0) != window.start:
                    continue
                previous = self._latest_previous(stream)
                snapshot = V4SessionOpenSnapshot(
                    symbol=candle.symbol,
                    timeframe=candle.timeframe,
                    session=name.value,
                    opened_at=candle.open_time.utc,
                    open_price=candle.open,
                    ema20=ema20,
                    ema50=ema50,
                    ema200=ema200,
                    trend=trend,
                    previous_session=(previous.session if previous else None),
                    previous_session_change=(previous.change if previous else None),
                    previous_session_range=(previous.range if previous else None),
                )
                self.opens.append(snapshot)
                opened.append(snapshot)
                state = _LiveSession(
                    opened_at=candle.open_time.utc,
                    open_price=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                )
                self._active[key] = state

            state.high = max(state.high, candle.high)
            state.low = min(state.low, candle.low)
            state.close = candle.close
            state.candle_count += 1
            if journey_started:
                state.journey_count += 1
            for detection in detections:
                if detection.agent_name == "ema_cross":
                    state.ema20_50_crosses += 1
                elif detection.agent_name == "ema200_cross":
                    state.ema200_crosses += 1

        return opened, closed

    def active_context(self, candle: Candle) -> V4SessionContext:
        return independent_session_context(candle.open_time.market)

    def _latest_previous(
        self,
        stream: tuple[str, str],
    ) -> V4SessionCloseOutcome | None:
        candidates = [
            row
            for (symbol, timeframe, _name), row in self._last_close.items()
            if (symbol, timeframe) == stream
        ]
        return max(candidates, key=lambda row: row.closed_at) if candidates else None


def fit_session_specialists(
    rows: list[V4SessionLearningRow],
    *,
    min_samples: int = 20,
) -> dict[str, dict[str, float | int | bool]]:
    """Learn resolved continuation/pullback behaviour per session cell.

    These are evidence summaries used by later model routing. Cells below the
    minimum remain explicitly insufficient instead of publishing unstable rates.
    """
    grouped: dict[str, list[V4SessionLearningRow]] = {}
    for row in rows:
        key = (
            "london_new_york_overlap"
            if row.overlap
            else f"{row.session}:{row.phase.value}"
        )
        grouped.setdefault(key, []).append(row)

    report: dict[str, dict[str, float | int | bool]] = {}
    for key, group in grouped.items():
        n = len(group)
        if n < min_samples:
            report[key] = {
                "samples": n,
                "minimum_required": min_samples,
                "sufficient": False,
            }
            continue
        pullbacks = [
            row.pullback_depth
            for row in group
            if row.pullback_depth is not None
        ]
        report[key] = {
            "samples": n,
            "minimum_required": min_samples,
            "sufficient": True,
            "reach_3_rate": sum(row.reached_3 for row in group) / n,
            "reach_5_rate": sum(row.reached_5 for row in group) / n,
            "reach_10_rate": sum(row.reached_10 for row in group) / n,
            "mean_mfe": sum(row.mfe for row in group) / n,
            "mean_mae": sum(row.mae for row in group) / n,
            "mean_pullback_depth": (
                sum(float(value) for value in pullbacks) / len(pullbacks)
                if pullbacks
                else 0.0
            ),
        }
    return report


def session_phase_report(
    rows: list[V4SessionLearningRow],
    *,
    min_samples: int = 1,
) -> dict[str, dict[str, float | int | bool]]:
    """TODO 050: compare movement by independent session phase."""
    return fit_session_specialists(rows, min_samples=min_samples)
