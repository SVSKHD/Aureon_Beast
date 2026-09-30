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
from aureon.services.cross_analysis import cross_candle_quality, simple_trend_direction

EVENT_BULLISH = "bullish"
EVENT_BEARISH = "bearish"


class Ema200CrossAgent(BaseAgent):
    """Price crossing EMA200 on the latest closed candle."""

    agent_name = "ema200_cross"
    agent_version = "1.0.0"

    def __init__(self, *, period: int = 200, price_field: str = "close") -> None:
        if period <= 0:
            raise ValueError("period must be positive")
        self.period = period
        self.price_field = price_field

    def params_snapshot(self) -> dict[str, object]:
        return {
            "period": self.period,
            "price_field": self.price_field,
            "warmup_bars": self.warmup_bars(),
        }

    def warmup_bars(self) -> int:
        return min_warmup(self.period)

    def min_window(self) -> int:
        return self.warmup_bars() + 1

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
            },
            categorical={
                "cross_direction": event_key,
                "price_relation": "above_ema200" if bullish else "below_ema200",
                "trend_direction": trend,
                "cross_quality": str(quality["quality"]),
            },
            flags={
                "clean_cross_close": bool(quality["clean_close"]),
                "trend_with_cross": trend == ("UP" if bullish else "DOWN"),
            },
        )

        return [
            self.build_detection(
                ctx=ctx,
                event_key=event_key,
                price=current_price,
                direction=direction,
                indicators=IndicatorSnapshot(
                    ema={"ema200": ema_now},
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
