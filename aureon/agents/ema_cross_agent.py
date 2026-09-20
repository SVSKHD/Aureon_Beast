"""EMA crossover agent (§13).

Emits one detection when the fast EMA crosses the slow EMA on a closed candle:
``event_key = "bullish"`` when fast crosses up through slow, ``"bearish"`` when it
crosses down.

## What counts as a cross

The comparison is between the **two most recent closed candles**. A cross is a
change of side: fast was at or below slow on the previous bar and is strictly above
it now (bullish), or the mirror (bearish). Requiring a strict inequality on the
current bar and allowing equality on the previous one means a series that touches
and separates produces exactly one detection, not two or none.

Nothing is emitted until ``min_warmup(slow_period)`` bars have been seen. Both EMAs
are recursive, so an early value still carries its seed and would cross on noise
rather than on price -- the first warmed bar of every run would otherwise look like
a signal.

RSI is attached as **context only** (§14). It never gates the detection. Keeping the
gate out means the stored detections record what the crossover rule saw, and any
question about whether RSI would have helped stays answerable later from
``detection_evaluations`` rather than being silently baked into what was recorded.
"""

from __future__ import annotations

import math

import pandas as pd

from aureon.agents.base_agent import BaseAgent, validate_window
from aureon.engine.indicators import crossed_at_last, ema, min_warmup, rsi
from aureon.models.detection import CandleContext, Detection, IndicatorSnapshot
from aureon.models.enums import Direction

EVENT_BULLISH = "bullish"
EVENT_BEARISH = "bearish"


class EmaCrossAgent(BaseAgent):
    """Fast/slow EMA crossover on closed candles (§13)."""

    agent_name = "ema_cross"
    #: 2.0.0: the periods moved to configuration and the shipped pair changed from
    #: 9/21 to 20/50. Under §12 the version is part of the detection id, so this bump
    #: forks history rather than rewriting it -- 1.0.0's detections stay exactly where
    #: they are and the two pairs can be compared over the same week.
    agent_version = "2.1.0"  # 9B

    def __init__(
        self,
        *,
        fast_period: int,
        slow_period: int,
        rsi_period: int = 14,
        price_field: str = "close",
    ) -> None:
        """Periods are REQUIRED -- there is no default pair.

        A default here is the bug this signature exists to prevent: the observer would
        read 20/50 from config while a replay, a script or a test silently constructed
        9/21, and the two would disagree about which candles produced a cross while
        both looked correct. Callers pass ``cfg.ema_fast`` / ``cfg.ema_slow``.
        """
        if fast_period <= 0 or slow_period <= 0:
            raise ValueError("periods must be positive")
        if fast_period >= slow_period:
            # A fast period at or above the slow one inverts the meaning of every
            # signal, so it is a configuration error rather than a preference.
            raise ValueError(
                f"fast_period ({fast_period}) must be below slow_period ({slow_period})"
            )
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.rsi_period = rsi_period
        self.price_field = price_field

    def params_snapshot(self) -> dict[str, object]:
        """Everything needed to reproduce this agent's output."""
        return {
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "rsi_period": self.rsi_period,
            "price_field": self.price_field,
            "warmup_bars": self.warmup_bars(),
        }

    def warmup_bars(self) -> int:
        """Bars of warm-up before a cross may be confirmed: ``slow_period × 3`` (§14).

        Three times the slow period is where an EMA has effectively forgotten its
        seed. Shorter, and the first crosses are artefacts of how the average was
        initialised rather than of the market.
        """
        return min_warmup(self.slow_period)

    def min_window(self) -> int:
        """Bars this agent needs before it will emit anything (§14).

        ``+ 1`` because a cross needs the previous bar's relationship as well as the
        current one. The RSI term is here because the snapshot carries RSI as context;
        it never gates the detection.
        """
        return max(self.warmup_bars(), self.rsi_period + 1) + 1

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

        event_key = EVENT_BULLISH if signal > 0 else EVENT_BEARISH
        direction = Direction.BUY if signal > 0 else Direction.SELL

        rsi_value = self._rsi_value(price)
        indicators = IndicatorSnapshot(
            ema={"fast": _clean(fast.iloc[-1]), "slow": _clean(slow.iloc[-1])},
            rsi=rsi_value,
            extras={
                "fast_minus_slow": _clean(fast.iloc[-1] - slow.iloc[-1]),
                # The previous bar's gap, so the crossover is auditable from the
                # stored record alone without re-reading the candle history.
                "prev_fast_minus_slow": _clean(fast.iloc[-2] - slow.iloc[-2]),
            },
        )

        return [
            self.build_detection(
                ctx=ctx,
                event_key=event_key,
                price=float(window[self.price_field].iloc[-1]),
                direction=direction,
                indicators=indicators,
            )
        ]

    def _rsi_value(self, price: pd.Series) -> float | None:
        """RSI at the latest bar, or None when it has not warmed up.

        None rather than a partial value: a half-warmed RSI stored as context would
        be indistinguishable from a real reading later.
        """
        if len(price) <= self.rsi_period:
            return None
        value = rsi(price, self.rsi_period).iloc[-1]
        return None if _is_nan(value) else float(value)


def _is_nan(value: object) -> bool:
    try:
        return math.isnan(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True


def _clean(value: object) -> float:
    """Float for storage, rejecting NaN.

    A NaN would serialise into Firestore and then compare unequal to itself,
    quietly breaking the parity comparison. If one reaches here the warm-up logic
    above is wrong and we want to know immediately.
    """
    number = float(value)  # type: ignore[arg-type]
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"refusing to store non-finite indicator value: {value!r}")
    return number
