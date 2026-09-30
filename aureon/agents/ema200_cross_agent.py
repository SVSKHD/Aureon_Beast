"""Aureon V2 price/EMA200 cross detector.

A detection is emitted only on a CLOSED candle that changes side relative to EMA200.
Staying above or below the EMA does not emit again.
"""
from __future__ import annotations

import math

import pandas as pd

from aureon.agents.base_agent import BaseAgent, validate_window
from aureon.engine.indicators import ema, min_warmup
from aureon.models.detection import AgentEvidence, CandleContext, Detection, IndicatorSnapshot
from aureon.models.enums import Direction
from aureon.services.cross_analysis import (
    classify_pre_cross_pattern,
    cross_candle_quality,
    simple_trend_direction,
)

EVENT_BULLISH = "bullish"
EVENT_BEARISH = "bearish"


class Ema200CrossAgent(BaseAgent):
    """Price crossing EMA200 on the latest closed candle."""

    agent_name = "ema200_cross"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        period: int = 200,
        fast_period: int = 20,
        slow_period: int = 50,
        price_field: str = "close",
    ) -> None:
        if period <= 0 or fast_period <= 0 or slow_period <= 0:
            raise ValueError("EMA periods must be positive")
        if fast_period >= slow_period:
            raise ValueError("fast_period must be below slow_period")
        self.period = period
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.price_field = price_field

    def params_snapshot(self) -> dict[str, object]:
        return {
            "period": self.period,
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "price_field": self.price_field,
            "warmup_bars": self.warmup_bars(),
        }

    def warmup_bars(self) -> int:
        return min_warmup(self.period)

    def min_window(self) -> int:
        return max(self.warmup_bars(), min_warmup(self.slow_period)) + 1

    def on_closed_candle(self, window: pd.DataFrame, ctx: CandleContext) -> list[Detection]:
        validate_window(window)
        if len(window) < self.min_window():
            return []

        price = window[self.price_field].astype(float)
        line = ema(price, self.period)
        current_price = _clean(price.iloc[-1])
        previous_price = _clean(price.iloc[-2])
        ema_now = _clean(line.iloc[-1])
        ema_prev = _clean(line.iloc[-2])

        bullish = previous_price <= ema_prev and current_price > ema_now
        bearish = previous_price >= ema_prev and current_price < ema_now
        if not bullish and not bearish:
            return []

        event_key = EVENT_BULLISH if bullish else EVENT_BEARISH
        direction = Direction.BUY if bullish else Direction.SELL
        trend = simple_trend_direction(window)
        pattern = classify_pre_cross_pattern(window)
        fast = ema(price, self.fast_period)
        slow = ema(price, self.slow_period)
        fast_now = _clean(fast.iloc[-1])
        slow_now = _clean(slow.iloc[-1])
        ema20_50_relation = "fast_above" if fast_now > slow_now else "fast_below" if fast_now < slow_now else "equal"
        quality = cross_candle_quality(
            window,
            reference_value=ema_now,
            direction=event_key,
        )

        evidence = AgentEvidence(
            numeric={
                "ema200": ema_now,
                "previous_ema200": ema_prev,
                "price_minus_ema200": current_price - ema_now,
                "previous_price_minus_ema200": previous_price - ema_prev,
                "cross_body_atr": float(quality["body_atr"]),
                "cross_close_beyond_atr": float(quality["close_beyond_atr"]),
                "cross_body_range_ratio": float(quality["body_range_ratio"]),
                "ema_fast": fast_now,
                "ema_slow": slow_now,
            },
            categorical={
                "cross_direction": event_key,
                "price_relation": "above_ema200" if bullish else "below_ema200",
                "trend_direction": trend,
                "cross_quality": str(quality["quality"]),
                "pre_cross_pattern": pattern,
                "ema20_50_context": ema20_50_relation,
            },
            flags={
                "clean_cross_close": bool(quality["clean_close"]),
                "trend_with_cross": trend == ("UP" if bullish else "DOWN"),
                "ema20_50_aligned": (
                    ema20_50_relation == "fast_above"
                    if bullish
                    else ema20_50_relation == "fast_below"
                ),
            },
        )

        return [
            self.build_detection(
                ctx=ctx,
                event_key=event_key,
                price=current_price,
                direction=direction,
                indicators=IndicatorSnapshot(
                    ema={"ema200": ema_now, "fast": fast_now, "slow": slow_now},
                    extras={
                        "previous_ema200": ema_prev,
                        "price_minus_ema200": current_price - ema_now,
                    },
                ),
                evidence=evidence,
            )
        ]


def _clean(value: object) -> float:
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"refusing to store non-finite EMA200 value: {value!r}")
    return number
