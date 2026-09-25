"""Agent 11: volume / participation context.

Uses the broker's real volume only when it is actually populated; otherwise it states
explicitly that it is using tick volume. The agent measures relative participation,
session VWAP, candle expansion and close location. Existing AnalysisEngine context then
attaches the Asia POC/VAH/VAL reference to the resulting detection, so there is one
volume-profile implementation rather than two.
"""

from __future__ import annotations

import math

import pandas as pd

from aureon.agents.base_agent import BaseAgent, validate_window
from aureon.config.sessions import sessions_for_index
from aureon.models.detection import AgentEvidence, CandleContext, Detection, IndicatorSnapshot
from aureon.models.enums import SessionName, Timeframe


class VolumeParticipationAgent(BaseAgent):
    agent_name = "volume_participation"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        timeframe: Timeframe,
        point: float,
        baseline_bars: int = 20,
        expansion_ratio: float = 1.50,
        abnormal_ratio: float = 2.50,
        contraction_ratio: float = 0.65,
        impulse_range_ratio: float = 1.25,
        close_extreme: float = 0.75,
        vwap_neutral_points: float = 10.0,
        real_volume_coverage: float = 0.80,
    ) -> None:
        if point <= 0:
            raise ValueError("point must be positive")
        if baseline_bars < 5:
            raise ValueError("baseline_bars must be at least 5")
        if not 0 < contraction_ratio < 1 < expansion_ratio < abnormal_ratio:
            raise ValueError("volume ratios are not ordered")
        if impulse_range_ratio <= 0:
            raise ValueError("impulse_range_ratio must be positive")
        if not 0.5 <= close_extreme < 1:
            raise ValueError("close_extreme must be in [0.5, 1)")
        if vwap_neutral_points < 0:
            raise ValueError("vwap_neutral_points must be non-negative")
        if not 0 <= real_volume_coverage <= 1:
            raise ValueError("real_volume_coverage must be in [0, 1]")
        self.timeframe = timeframe
        self.point = point
        self.baseline_bars = baseline_bars
        self.expansion_ratio = expansion_ratio
        self.abnormal_ratio = abnormal_ratio
        self.contraction_ratio = contraction_ratio
        self.impulse_range_ratio = impulse_range_ratio
        self.close_extreme = close_extreme
        self.vwap_neutral_points = vwap_neutral_points
        self.real_volume_coverage = real_volume_coverage
        # Eight hours is the longest configured named session.
        self.session_window_bars = max(8 * 60 // timeframe.minutes + 4, baseline_bars + 2)

    def params_snapshot(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe.value,
            "point": self.point,
            "baseline_bars": self.baseline_bars,
            "expansion_ratio": self.expansion_ratio,
            "abnormal_ratio": self.abnormal_ratio,
            "contraction_ratio": self.contraction_ratio,
            "impulse_range_ratio": self.impulse_range_ratio,
            "close_extreme": self.close_extreme,
            "vwap_neutral_points": self.vwap_neutral_points,
            "real_volume_coverage": self.real_volume_coverage,
            "session_window_bars": self.session_window_bars,
            "warmup_bars": self.min_window(),
        }

    def min_window(self) -> int:
        return self.session_window_bars + 2

    def on_closed_candle(self, window: pd.DataFrame, ctx: CandleContext) -> list[Detection]:
        validate_window(window)
        if ctx.timeframe is not self.timeframe:
            raise ValueError(
                f"VolumeParticipationAgent configured for {self.timeframe.value} but received "
                f"{ctx.timeframe.value}"
            )
        if len(window) < self.min_window():
            return []

        current = participation_snapshot(
            window,
            ctx,
            point=self.point,
            baseline_bars=self.baseline_bars,
            expansion_ratio=self.expansion_ratio,
            abnormal_ratio=self.abnormal_ratio,
            contraction_ratio=self.contraction_ratio,
            impulse_range_ratio=self.impulse_range_ratio,
            close_extreme=self.close_extreme,
            vwap_neutral_points=self.vwap_neutral_points,
            real_volume_coverage=self.real_volume_coverage,
        )
        previous = participation_snapshot(
            window.iloc[:-1],
            ctx,
            point=self.point,
            baseline_bars=self.baseline_bars,
            expansion_ratio=self.expansion_ratio,
            abnormal_ratio=self.abnormal_ratio,
            contraction_ratio=self.contraction_ratio,
            impulse_range_ratio=self.impulse_range_ratio,
            close_extreme=self.close_extreme,
            vwap_neutral_points=self.vwap_neutral_points,
            real_volume_coverage=self.real_volume_coverage,
        )
        if current is None:
            return []

        changed = previous is None or (
            current["participation_state"] != previous["participation_state"]
            or current["vwap_relation"] != previous["vwap_relation"]
        )
        impulse = str(current["price_impulse"])
        if not changed and impulse == "none":
            return []

        numeric = {
            key: float(value)
            for key, value in current.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        categorical = {
            "participation_state": str(current["participation_state"]),
            "volume_source": str(current["volume_source"]),
            "price_impulse": impulse,
            "vwap_relation": str(current["vwap_relation"]),
            "session": str(current["session"]),
        }
        evidence = AgentEvidence(
            numeric=numeric,
            categorical=categorical,
            flags={
                "volume_expanding": bool(current["volume_expanding"]),
                "volume_contracting": bool(current["volume_contracting"]),
                "abnormal_volume": bool(current["abnormal_volume"]),
                "above_vwap": current["vwap_relation"] == "above",
                "below_vwap": current["vwap_relation"] == "below",
                "bullish_impulse": impulse == "bullish",
                "bearish_impulse": impulse == "bearish",
            },
        )
        levels = {}
        if current.get("session_vwap") is not None:
            levels["session_vwap"] = float(current["session_vwap"])
        return [
            self.build_detection(
                ctx=ctx,
                event_key=(
                    f"participation|{current['participation_state']}|"
                    f"{impulse}|{current['vwap_relation']}"
                ),
                price=float(window["close"].iloc[-1]),
                direction=None,
                indicators=IndicatorSnapshot(extras=numeric),
                levels=levels,
                evidence=evidence,
            )
        ]


def participation_snapshot(
    window: pd.DataFrame,
    ctx: CandleContext,
    *,
    point: float,
    baseline_bars: int,
    expansion_ratio: float,
    abnormal_ratio: float,
    contraction_ratio: float,
    impulse_range_ratio: float,
    close_extreme: float,
    vwap_neutral_points: float,
    real_volume_coverage: float,
) -> dict[str, object] | None:
    if len(window) < baseline_bars + 2:
        return None

    recent_real = window["real_volume"].astype(float).iloc[-(baseline_bars + 1):]
    coverage = float((recent_real > 0).mean())
    use_real = coverage >= real_volume_coverage and float(recent_real.iloc[-1]) > 0
    source = "real_volume" if use_real else "tick_volume"
    volumes = window[source].astype(float)

    baseline = volumes.iloc[-(baseline_bars + 1):-1]
    median_volume = float(baseline.median())
    current_volume = float(volumes.iloc[-1])
    if median_volume <= 0 or not math.isfinite(median_volume):
        return None
    relative_volume = current_volume / median_volume

    if relative_volume >= abnormal_ratio:
        state = "abnormal_expansion"
    elif relative_volume >= expansion_ratio:
        state = "expanding"
    elif relative_volume <= contraction_ratio:
        state = "contracting"
    else:
        state = "normal"

    candle_range = float(window["high"].iloc[-1] - window["low"].iloc[-1])
    previous_ranges = (window["high"] - window["low"]).astype(float).iloc[
        -(baseline_bars + 1):-1
    ]
    median_range = float(previous_ranges.median())
    range_ratio = 0.0 if median_range <= 0 else candle_range / median_range

    low = float(window["low"].iloc[-1])
    high = float(window["high"].iloc[-1])
    open_ = float(window["open"].iloc[-1])
    close = float(window["close"].iloc[-1])
    close_position = 0.5 if high <= low else (close - low) / (high - low)

    impulse = "none"
    if range_ratio >= impulse_range_ratio:
        if close > open_ and close_position >= close_extreme:
            impulse = "bullish"
        elif close < open_ and close_position <= (1.0 - close_extreme):
            impulse = "bearish"

    local_index = window.index.tz_convert(ctx.market_tz)
    dates = [stamp.date().isoformat() for stamp in local_index.to_pydatetime()]
    sessions = sessions_for_index(local_index)
    current_date = dates[-1]
    current_session = sessions[-1]
    session_pos = [
        i
        for i, (date, session) in enumerate(zip(dates, sessions, strict=True))
        if date == current_date and session is current_session
    ]
    # Outside named sessions use the broker day, rather than inventing an OFF-session VWAP.
    if current_session is SessionName.OFF:
        session_pos = [i for i, date in enumerate(dates) if date == current_date]

    session_vwap = None
    if session_pos:
        frame = window.iloc[session_pos]
        weights = frame[source].astype(float)
        total = float(weights.sum())
        if total > 0:
            typical = (
                frame["high"].astype(float)
                + frame["low"].astype(float)
                + frame["close"].astype(float)
            ) / 3.0
            session_vwap = float((typical * weights).sum() / total)

    vwap_relation = "unknown"
    distance_from_vwap_points = None
    if session_vwap is not None:
        distance_from_vwap_points = (close - session_vwap) / point
        if abs(distance_from_vwap_points) <= vwap_neutral_points:
            vwap_relation = "at"
        elif distance_from_vwap_points > 0:
            vwap_relation = "above"
        else:
            vwap_relation = "below"

    return {
        "current_volume": current_volume,
        "median_volume": median_volume,
        "relative_volume": relative_volume,
        "real_volume_coverage": coverage,
        "candle_range": candle_range,
        "median_candle_range": median_range,
        "candle_range_ratio": range_ratio,
        "close_position": close_position,
        "session_vwap": session_vwap,
        "distance_from_vwap_points": distance_from_vwap_points,
        "participation_state": state,
        "volume_source": source,
        "price_impulse": impulse,
        "vwap_relation": vwap_relation,
        "session": current_session.value,
        "volume_expanding": relative_volume >= expansion_ratio,
        "volume_contracting": relative_volume <= contraction_ratio,
        "abnormal_volume": relative_volume >= abnormal_ratio,
    }
