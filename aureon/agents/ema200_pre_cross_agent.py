"""Selective EMA200 pre-cross pressure detector.

Emits one early-warning detection when closed-candle evidence first becomes
sufficiently aligned toward an EMA200 cross. It does not predict a cross and does
not repeat every candle while the same pressure regime persists.
"""
from __future__ import annotations

import math

import pandas as pd

from aureon.agents.base_agent import BaseAgent, validate_window
from aureon.engine.indicators import ema, min_warmup, rsi
from aureon.models.detection import AgentEvidence, CandleContext, Detection, IndicatorSnapshot
from aureon.models.enums import Direction
from aureon.services.cross_analysis import (
    classify_pre_cross_pattern,
    recent_atr,
    simple_trend_direction,
)
from aureon.services.volume_features import volume_features

EVENT_BULLISH = "bullish_pressure"
EVENT_BEARISH = "bearish_pressure"


class Ema200PreCrossPressureAgent(BaseAgent):
    """Early EMA200 pressure warning from closed candles only."""

    agent_name = "ema200_pre_cross"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        ema200_period: int = 200,
        fast_period: int = 20,
        slow_period: int = 50,
        rsi_period: int = 14,
        max_distance_atr: float = 2.5,
        min_supporting_conditions: int = 4,
        rearm_bars: int = 6,
        price_field: str = "close",
    ) -> None:
        if ema200_period <= 0 or fast_period <= 0 or slow_period <= 0 or rsi_period <= 0:
            raise ValueError("indicator periods must be positive")
        if fast_period >= slow_period:
            raise ValueError("fast_period must be below slow_period")
        if max_distance_atr <= 0:
            raise ValueError("max_distance_atr must be positive")
        if not 1 <= min_supporting_conditions <= 6:
            raise ValueError("min_supporting_conditions must be between 1 and 6")
        if rearm_bars < 1:
            raise ValueError("rearm_bars must be positive")
        self.ema200_period = ema200_period
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.rsi_period = rsi_period
        self.max_distance_atr = max_distance_atr
        self.min_supporting_conditions = min_supporting_conditions
        self.rearm_bars = rearm_bars
        self.price_field = price_field

    def params_snapshot(self) -> dict[str, object]:
        return {
            "ema200_period": self.ema200_period,
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "rsi_period": self.rsi_period,
            "max_distance_atr": self.max_distance_atr,
            "min_supporting_conditions": self.min_supporting_conditions,
            "rearm_bars": self.rearm_bars,
            "price_field": self.price_field,
        }

    def min_window(self) -> int:
        return max(
            min_warmup(self.ema200_period),
            min_warmup(self.slow_period),
            self.rsi_period + 2,
        ) + self.rearm_bars + 2

    def on_closed_candle(self, window: pd.DataFrame, ctx: CandleContext) -> list[Detection]:
        validate_window(window)
        if len(window) < self.min_window():
            return []

        current = self._snapshot(window)
        if current is None:
            return []

        # Fresh-pressure semantics: once pressure is active, stay silent. Re-arm only
        # after several closed candles without the same pressure state.
        direction = current["direction"]
        for back in range(1, self.rearm_bars + 1):
            previous = self._snapshot(window.iloc[:-back])
            if previous is not None and previous["direction"] == direction:
                return []

        event_key = EVENT_BULLISH if direction == "bullish" else EVENT_BEARISH
        side = Direction.BUY if direction == "bullish" else Direction.SELL
        price = float(window[self.price_field].iloc[-1])

        volume_numeric, volume_categorical, volume_flags = volume_features(window)
        evidence = AgentEvidence(
            numeric={
                "ema200": float(current["ema200"]),
                "ema_fast": float(current["ema_fast"]),
                "ema_slow": float(current["ema_slow"]),
                "distance_to_ema200": float(current["distance"]),
                "distance_to_ema200_atr": float(current["distance_atr"]),
                "ema_gap": float(current["ema_gap"]),
                "ema_gap_change": float(current["ema_gap_change"]),
                "fast_slope": float(current["fast_slope"]),
                "rsi": float(current["rsi"]),
                "rsi_change": float(current["rsi_change"]),
                "supporting_conditions": float(current["supporting"]),
                **volume_numeric,
            },
            categorical={
                "pressure_direction": str(direction),
                "trend_direction": str(current["trend"]),
                "pre_cross_pattern": str(current["pattern"]),
                "price_relation": str(current["price_relation"]),
                "ema20_50_context": str(current["ema20_50_context"]),
                **volume_categorical,
            },
            flags={
                "distance_closing": bool(current["distance_closing"]),
                "fast_slope_aligned": bool(current["fast_slope_aligned"]),
                "ema_gap_moving_with_pressure": bool(current["gap_aligned"]),
                "rsi_moving_with_pressure": bool(current["rsi_aligned"]),
                "directional_candle": bool(current["directional_candle"]),
                "trend_aligned": bool(current["trend_aligned"]),
                **volume_flags,
            },
        )

        return [
            self.build_detection(
                ctx=ctx,
                event_key=event_key,
                price=price,
                direction=side,
                indicators=IndicatorSnapshot(
                    ema={
                        "ema200": float(current["ema200"]),
                        "fast": float(current["ema_fast"]),
                        "slow": float(current["ema_slow"]),
                    },
                    rsi=float(current["rsi"]),
                ),
                evidence=evidence,
            )
        ]

    def _snapshot(self, frame: pd.DataFrame) -> dict[str, object] | None:
        if len(frame) < max(min_warmup(self.ema200_period), self.rsi_period + 2):
            return None

        price = frame[self.price_field].astype(float)
        fast = ema(price, self.fast_period)
        slow = ema(price, self.slow_period)
        line = ema(price, self.ema200_period)
        rsi_values = rsi(price, self.rsi_period)

        values = [
            fast.iloc[-1],
            fast.iloc[-2],
            slow.iloc[-1],
            slow.iloc[-2],
            line.iloc[-1],
            line.iloc[-2],
            rsi_values.iloc[-1],
            rsi_values.iloc[-2],
        ]
        if any(_is_bad(value) for value in values):
            return None

        close_now = float(price.iloc[-1])
        close_prev = float(price.iloc[-2])
        ema200_now = float(line.iloc[-1])
        ema200_prev = float(line.iloc[-2])
        fast_now = float(fast.iloc[-1])
        fast_prev = float(fast.iloc[-2])
        slow_now = float(slow.iloc[-1])
        slow_prev = float(slow.iloc[-2])
        rsi_now = float(rsi_values.iloc[-1])
        rsi_prev = float(rsi_values.iloc[-2])
        atr = recent_atr(frame)
        if atr <= 0:
            return None

        distance_now = close_now - ema200_now
        distance_prev = close_prev - ema200_prev
        distance_atr = abs(distance_now) / atr
        if distance_atr > self.max_distance_atr or distance_now == 0:
            return None
        # This warning exists only BEFORE a cross. A side change belongs exclusively
        # to Ema200CrossAgent and must never be relabelled as opposite pre-cross pressure.
        if distance_now * distance_prev <= 0:
            return None

        direction = "bearish" if distance_now > 0 else "bullish"
        trend = simple_trend_direction(frame)
        pattern = classify_pre_cross_pattern(frame)
        ema_gap_now = fast_now - slow_now
        ema_gap_prev = fast_prev - slow_prev
        ema_gap_change = ema_gap_now - ema_gap_prev
        fast_slope = fast_now - fast_prev
        rsi_change = rsi_now - rsi_prev
        distance_closing = abs(distance_now) < abs(distance_prev)

        if direction == "bearish":
            trend_aligned = trend == "DOWN"
            fast_slope_aligned = fast_slope < 0
            gap_aligned = ema_gap_change < 0
            rsi_aligned = rsi_change < 0
            directional_candle = close_now < close_prev
        else:
            trend_aligned = trend == "UP"
            fast_slope_aligned = fast_slope > 0
            gap_aligned = ema_gap_change > 0
            rsi_aligned = rsi_change > 0
            directional_candle = close_now > close_prev

        supporting = sum(
            (
                trend_aligned,
                fast_slope_aligned,
                gap_aligned,
                rsi_aligned,
                distance_closing,
                directional_candle,
            )
        )
        if not distance_closing or supporting < self.min_supporting_conditions:
            return None
        if pattern == "CHOPPY":
            return None

        return {
            "direction": direction,
            "ema200": ema200_now,
            "ema_fast": fast_now,
            "ema_slow": slow_now,
            "distance": abs(distance_now),
            "distance_atr": distance_atr,
            "ema_gap": ema_gap_now,
            "ema_gap_change": ema_gap_change,
            "fast_slope": fast_slope,
            "rsi": rsi_now,
            "rsi_change": rsi_change,
            "supporting": supporting,
            "trend": trend,
            "pattern": pattern,
            "price_relation": "above_ema200" if distance_now > 0 else "below_ema200",
            "ema20_50_context": (
                "fast_above"
                if ema_gap_now > 0
                else "fast_below"
                if ema_gap_now < 0
                else "equal"
            ),
            "distance_closing": distance_closing,
            "fast_slope_aligned": fast_slope_aligned,
            "gap_aligned": gap_aligned,
            "rsi_aligned": rsi_aligned,
            "directional_candle": directional_candle,
            "trend_aligned": trend_aligned,
        }


def _is_bad(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return True
    return math.isnan(number) or math.isinf(number)
