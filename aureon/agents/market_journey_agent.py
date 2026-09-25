"""Agent 9: deterministic market-journey / historical-context observations.

The agent never fetches data. It reconstructs the current broker-day journey from the
closed-candle window it is handed, then joins it to the previous broker day and the
last few completed days. That keeps the same answer in live observation and replay.

It is context-only: it never emits BUY/SELL. A detection is emitted when the market's
location state changes (session, Asia-range location, previous-day-range location, or
side of previous close). The full numeric snapshot is stored on that transition.
"""

from __future__ import annotations

import pandas as pd

from aureon.agents.base_agent import BaseAgent, validate_window
from aureon.config.sessions import sessions_for_index
from aureon.models.detection import AgentEvidence, CandleContext, Detection, IndicatorSnapshot
from aureon.models.enums import SessionName


class MarketJourneyAgent(BaseAgent):
    """Where price is now relative to Asia and recent completed broker days."""

    agent_name = "market_journey"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        point: float,
        lookback_bars: int = 1200,
        recent_days: int = 3,
        near_level_points: float = 300.0,
    ) -> None:
        if point <= 0:
            raise ValueError("point must be positive")
        if lookback_bars < 100:
            raise ValueError("lookback_bars must be at least 100")
        if recent_days < 1:
            raise ValueError("recent_days must be positive")
        if near_level_points <= 0:
            raise ValueError("near_level_points must be positive")
        self.point = point
        self.lookback_bars = lookback_bars
        self.recent_days = recent_days
        self.near_level_points = near_level_points

    def params_snapshot(self) -> dict[str, object]:
        return {
            "point": self.point,
            "lookback_bars": self.lookback_bars,
            "recent_days": self.recent_days,
            "near_level_points": self.near_level_points,
            "warmup_bars": self.lookback_bars,
        }

    def min_window(self) -> int:
        return self.lookback_bars

    def on_closed_candle(self, window: pd.DataFrame, ctx: CandleContext) -> list[Detection]:
        validate_window(window)
        if len(window) < self.min_window():
            return []

        current = market_journey_snapshot(\n            window, ctx, point=self.point, recent_days=self.recent_days,\n            near_level_points=self.near_level_points,\n        )
        if current is None:
            return []
        previous = market_journey_snapshot(
            window.iloc[:-1], ctx, point=self.point, recent_days=self.recent_days
        )

        # Store transitions rather than one document every five minutes. The helper is public,
        # so the future Director can ask for the same snapshot on every candle without changing
        # the research population.
        if previous is not None and _state_key(current) == _state_key(previous):
            return []

        levels = {
            key: float(current[key])
            for key in (
                "asia_open",
                "asia_high",
                "asia_low",
                "previous_day_high",
                "previous_day_low",
                "previous_day_close",
                "recent_high",
                "recent_low",
            )
            if current.get(key) is not None
        }
        numeric = {
            key: float(value)
            for key, value in current.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        evidence = AgentEvidence(
            numeric=numeric,
            categorical={
                "session": str(current["session"]),
                "asia_location": str(current["asia_location"]),
                "previous_day_location": str(current["previous_day_location"]),
                "previous_close_relation": str(current["previous_close_relation"]),
                "journey_state": str(current["journey_state"]),
            },
            flags={
                "asia_available": current["asia_open"] is not None,
                "previous_day_available": current["previous_day_high"] is not None,
                "above_asia_open": bool(current["above_asia_open"]),
                "above_previous_close": bool(current["above_previous_close"]),
                "near_previous_day_high": bool(current["near_previous_day_high"]),
                "near_previous_day_low": bool(current["near_previous_day_low"]),
            },
        )
        event_key = (
            f"journey|{current['session']}|{current['asia_location']}|"
            f"{current['previous_day_location']}|{current['previous_close_relation']}"
        )
        return [
            self.build_detection(
                ctx=ctx,
                event_key=event_key,
                price=float(current["current_price"]),
                direction=None,
                indicators=IndicatorSnapshot(extras=numeric),
                levels=levels,
                evidence=evidence,
            )
        ]


def market_journey_snapshot(
    window: pd.DataFrame,
    ctx: CandleContext,
    *,
    point: float,
    recent_days: int = 3,
) -> dict[str, object] | None:
    """Pure journey snapshot from already-closed candles.

    The last completed market date in the supplied window is the previous day. The
    window is intentionally much larger than one day so this survives restarts and
    weekend gaps without an agent-side fetch.
    """
    if len(window) < 2:
        return None

    local_index = window.index.tz_convert(ctx.market_tz)
    dates = [stamp.date().isoformat() for stamp in local_index.to_pydatetime()]
    distinct_dates = list(dict.fromkeys(dates))
    current_date = dates[-1]
    completed_dates = [value for value in distinct_dates if value < current_date]
    previous_date = completed_dates[-1] if completed_dates else None

    current_pos = [i for i, value in enumerate(dates) if value == current_date]
    if not current_pos:
        return None
    current_day = window.iloc[current_pos]
    current_price = float(window["close"].iloc[-1])

    sessions = sessions_for_index(local_index)
    current_session = sessions[-1]
    asia_pos = [
        i
        for i, (value, session) in enumerate(zip(dates, sessions, strict=True))
        if value == current_date and session is SessionName.ASIA
    ]
    asia = window.iloc[asia_pos] if asia_pos else None

    previous = None
    if previous_date is not None:
        previous_pos = [i for i, value in enumerate(dates) if value == previous_date]
        previous = window.iloc[previous_pos] if previous_pos else None

    recent_completed = completed_dates[-recent_days:]
    recent_pos = [i for i, value in enumerate(dates) if value in recent_completed]
    recent = window.iloc[recent_pos] if recent_pos else None

    asia_open = _first(asia, "open")
    asia_high = _max(asia, "high")
    asia_low = _min(asia, "low")
    previous_open = _first(previous, "open")
    previous_high = _max(previous, "high")
    previous_low = _min(previous, "low")
    previous_close = _last(previous, "close")
    recent_high = _max(recent, "high")
    recent_low = _min(recent, "low")

    asia_location = _range_location(current_price, asia_low, asia_high, "asia")
    previous_location = _range_location(
        current_price, previous_low, previous_high, "previous_day"
    )
    previous_close_relation = _relation(current_price, previous_close)
    above_asia_open = asia_open is not None and current_price > asia_open
    above_previous_close = previous_close is not None and current_price > previous_close

    return {
        "current_price": current_price,
        "current_day_open": float(current_day["open"].iloc[0]),
        "current_day_high": float(current_day["high"].max()),
        "current_day_low": float(current_day["low"].min()),
        "current_day_range": float(current_day["high"].max() - current_day["low"].min()),
        "asia_open": asia_open,
        "asia_high": asia_high,
        "asia_low": asia_low,
        "asia_range": None if asia_high is None or asia_low is None else asia_high - asia_low,
        "previous_day_open": previous_open,
        "previous_day_high": previous_high,
        "previous_day_low": previous_low,
        "previous_day_close": previous_close,
        "previous_day_range": (
            None if previous_high is None or previous_low is None else previous_high - previous_low
        ),
        "recent_high": recent_high,
        "recent_low": recent_low,
        "distance_from_asia_open_points": _distance(current_price, asia_open, point),
        "distance_from_asia_high_points": _distance(current_price, asia_high, point),
        "distance_from_asia_low_points": _distance(current_price, asia_low, point),
        "distance_from_previous_high_points": _distance(current_price, previous_high, point),
        "distance_from_previous_low_points": _distance(current_price, previous_low, point),
        "distance_from_previous_close_points": _distance(current_price, previous_close, point),
        "session": current_session.value,
        "asia_location": asia_location,
        "previous_day_location": previous_location,
        "previous_close_relation": previous_close_relation,
        "above_asia_open": bool(above_asia_open),
        "above_previous_close": bool(above_previous_close),
        "near_previous_day_high": _near(current_price, previous_high, point, 300.0),
        "near_previous_day_low": _near(current_price, previous_low, point, 300.0),
        "journey_state": (
            f"{current_session.value}|{asia_location}|{previous_location}|"
            f"{previous_close_relation}"
        ),
    }


def _state_key(snapshot: dict[str, object]) -> tuple[object, ...]:
    return (
        snapshot["session"],
        snapshot["asia_location"],
        snapshot["previous_day_location"],
        snapshot["previous_close_relation"],
    )


def _range_location(
    price: float, low: float | None, high: float | None, label: str
) -> str:
    if low is None or high is None:
        return f"{label}_unknown"
    if price > high:
        return f"above_{label}_high"
    if price < low:
        return f"below_{label}_low"
    return f"inside_{label}_range"


def _relation(price: float, level: float | None) -> str:
    if level is None:
        return "unknown"
    if price > level:
        return "above"
    if price < level:
        return "below"
    return "at"


def _distance(price: float, level: float | None, point: float) -> float | None:
    return None if level is None else (price - level) / point


def _near(
    price: float, level: float | None, point: float, threshold_points: float
) -> bool:
    return level is not None and abs(price - level) / point <= threshold_points


def _first(frame: pd.DataFrame | None, field: str) -> float | None:
    return None if frame is None or frame.empty else float(frame[field].iloc[0])


def _last(frame: pd.DataFrame | None, field: str) -> float | None:
    return None if frame is None or frame.empty else float(frame[field].iloc[-1])


def _max(frame: pd.DataFrame | None, field: str) -> float | None:
    return None if frame is None or frame.empty else float(frame[field].max())


def _min(frame: pd.DataFrame | None, field: str) -> float | None:
    return None if frame is None or frame.empty else float(frame[field].min())
