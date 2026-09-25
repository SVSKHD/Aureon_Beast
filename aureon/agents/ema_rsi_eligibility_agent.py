"""Agent 20: EMA20/50 cross + RSI-60 eligibility layer.

This agent deliberately does not replace the raw EMA cross. It emits one research detection
for every warmed EMA cross and marks whether the cross is eligible under the configured RSI
rule:

- bullish cross + RSI < long_below  -> LONG_ELIGIBLE (direction BUY)
- bearish cross + RSI > short_above -> SHORT_ELIGIBLE (direction SELL)
- otherwise                         -> ineligible context event (direction None)

Keeping the rejected crosses is important: replay/training needs the counterfactual examples
rather than a dataset containing only setups the rule already accepted.
"""

from __future__ import annotations

import math

import pandas as pd

from aureon.agents.base_agent import BaseAgent, validate_window
from aureon.engine.indicators import crossed_at_last, ema, min_warmup, rsi
from aureon.models.detection import AgentEvidence, CandleContext, Detection, IndicatorSnapshot
from aureon.models.enums import Direction

LONG_ELIGIBLE = "long_eligible"
SHORT_ELIGIBLE = "short_eligible"
LONG_INELIGIBLE = "long_ineligible_rsi"
SHORT_INELIGIBLE = "short_ineligible_rsi"


class EmaRsiEligibilityAgent(BaseAgent):
    agent_name = "ema_rsi_eligibility"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        fast_period: int,
        slow_period: int,
        rsi_period: int = 14,
        long_below: float = 60.0,
        short_above: float = 60.0,
        price_field: str = "close",
    ) -> None:
        if fast_period <= 0 or slow_period <= 0 or fast_period >= slow_period:
            raise ValueError("require 0 < fast_period < slow_period")
        if rsi_period <= 0:
            raise ValueError("rsi_period must be positive")
        if not 0 < long_below < 100 or not 0 < short_above < 100:
            raise ValueError("RSI eligibility thresholds must be between 0 and 100")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.rsi_period = rsi_period
        self.long_below = long_below
        self.short_above = short_above
        self.price_field = price_field

    def params_snapshot(self) -> dict[str, object]:
        return {
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "rsi_period": self.rsi_period,
            "long_below": self.long_below,
            "short_above": self.short_above,
            "price_field": self.price_field,
            "warmup_bars": self.min_window(),
        }

    def min_window(self) -> int:
        return max(min_warmup(self.slow_period), min_warmup(self.rsi_period)) + 2

    def on_closed_candle(self, window: pd.DataFrame, ctx: CandleContext) -> list[Detection]:
        validate_window(window)
        if len(window) < self.min_window():
            return []

        price = window[self.price_field]
        fast = ema(price, self.fast_period)
        slow = ema(price, self.slow_period)
        signal = crossed_at_last(fast, slow)
        if signal == 0:
            return []

        rsi_value = float(rsi(price, self.rsi_period).iloc[-1])
        if not math.isfinite(rsi_value):
            return []

        bullish_cross = signal > 0
        eligible = (
            rsi_value < self.long_below
            if bullish_cross
            else rsi_value > self.short_above
        )
        if bullish_cross:
            event_key = LONG_ELIGIBLE if eligible else LONG_INELIGIBLE
            raw_direction = Direction.BUY
        else:
            event_key = SHORT_ELIGIBLE if eligible else SHORT_INELIGIBLE
            raw_direction = Direction.SELL

        fast_now = float(fast.iloc[-1])
        slow_now = float(slow.iloc[-1])
        fast_prev = float(fast.iloc[-2])
        slow_prev = float(slow.iloc[-2])
        gap_now = fast_now - slow_now
        gap_prev = fast_prev - slow_prev

        evidence = AgentEvidence(
            numeric={
                "rsi": rsi_value,
                "rsi_long_below": self.long_below,
                "rsi_short_above": self.short_above,
                "rsi_distance_from_60": rsi_value - 60.0,
                "ema_fast": fast_now,
                "ema_slow": slow_now,
                "ema_gap": gap_now,
                "previous_ema_gap": gap_prev,
                "ema_gap_change": gap_now - gap_prev,
                "fast_slope": fast_now - fast_prev,
                "slow_slope": slow_now - slow_prev,
            },
            categorical={
                "cross_direction": "bullish" if bullish_cross else "bearish",
                "eligibility": "eligible" if eligible else "ineligible",
                "eligible_side": (
                    "long" if bullish_cross and eligible
                    else "short" if (not bullish_cross and eligible)
                    else "none"
                ),
            },
            flags={
                "eligible": eligible,
                "rsi_below_long_threshold": rsi_value < self.long_below,
                "rsi_above_short_threshold": rsi_value > self.short_above,
                "rsi_exactly_60": math.isclose(rsi_value, 60.0, abs_tol=1e-12),
            },
        )
        return [
            self.build_detection(
                ctx=ctx,
                event_key=event_key,
                price=float(price.iloc[-1]),
                # Only eligible events contribute directional evidence downstream.
                direction=raw_direction if eligible else None,
                indicators=IndicatorSnapshot(
                    ema={"fast": fast_now, "slow": slow_now},
                    rsi=rsi_value,
                    extras={
                        "fast_minus_slow": gap_now,
                        "prev_fast_minus_slow": gap_prev,
                    },
                ),
                evidence=evidence,
            )
        ]
