"""RSI context agent (§14).

Emits **context-only** detections -- ``direction=None`` -- when RSI moves into or out
of an overbought/oversold zone. It never says "buy" or "sell", and nothing downstream
treats it as a signal.

## Why a zone *transition* and not a zone *state*

RSI sits above 70 for long stretches. An agent that emitted while the condition held
would produce hundreds of near-identical detections per week, all sharing a candle's
worth of information, and would drown the genuinely interesting moment: the bar where
the state changed. So this fires on the edge, once, and the review can reconstruct the
"still overbought" stretches from the entry and exit pair.

## Its relationship to the cross agent

The cross agent already stamps RSI into its own ``IndicatorSnapshot``, so a cross
detection carries the RSI reading at that candle without this agent's involvement --
both read the same ``indicators.rsi`` function, so there is one implementation, not
two. What this agent adds is the standalone zone context: "RSI had entered oversold
four bars before that bullish cross" is a question about two separate detections, and
that is exactly the kind of correlation Phase 7 is for. Deliberately kept as separate
documents rather than folded into the cross detection, because a detection must not
depend on another agent having run (§79 purity).
"""

from __future__ import annotations

import math

import pandas as pd

from aureon.agents.base_agent import BaseAgent, validate_window
from aureon.engine.indicators import min_warmup, rsi
from aureon.models.detection import CandleContext, Detection, IndicatorSnapshot

EVENT_OVERBOUGHT_ENTRY = "overbought_entry"
EVENT_OVERBOUGHT_EXIT = "overbought_exit"
EVENT_OVERSOLD_ENTRY = "oversold_entry"
EVENT_OVERSOLD_EXIT = "oversold_exit"

ZONE_OVERBOUGHT = "overbought"
ZONE_OVERSOLD = "oversold"
ZONE_NEUTRAL = "neutral"

#: The shipped boundaries. Named here rather than repeated as literals, because Discord
#: labels a stored RSI at render time (detections store the value, not the label -- see
#: ``IndicatorSnapshot``) and a second copy of 70/30 would drift from this one silently:
#: the embed would say "neutral" about a reading this agent had called overbought.
DEFAULT_OVERBOUGHT = 70.0
DEFAULT_OVERSOLD = 30.0


def rsi_zone(value: float, *, overbought: float, oversold: float) -> str:
    """Classify an RSI reading into a zone.

    Boundaries are inclusive on the extreme side (``>= overbought``), matching how a
    trader reads "RSI is at 70".
    """
    if value >= overbought:
        return ZONE_OVERBOUGHT
    if value <= oversold:
        return ZONE_OVERSOLD
    return ZONE_NEUTRAL


class RsiAgent(BaseAgent):
    """Context-only RSI zone transitions (§14)."""

    agent_name = "rsi"
    agent_version = "1.2.0"  # 11D

    def __init__(
        self,
        *,
        rsi_period: int = 14,
        overbought: float = DEFAULT_OVERBOUGHT,
        oversold: float = DEFAULT_OVERSOLD,
        price_field: str = "close",
    ) -> None:
        if not 0 < oversold < overbought < 100:
            # Inverted or out-of-range thresholds would classify every bar into the
            # wrong zone silently, so this is a configuration error, not a preference.
            raise ValueError(
                f"require 0 < oversold ({oversold}) < overbought ({overbought}) < 100"
            )
        self.rsi_period = rsi_period
        self.overbought = overbought
        self.oversold = oversold
        self.price_field = price_field

    def params_snapshot(self) -> dict[str, object]:
        return {
            "rsi_period": self.rsi_period,
            "overbought": self.overbought,
            "oversold": self.oversold,
            "price_field": self.price_field,
            "warmup_bars": self.min_window(),
        }

    def min_window(self) -> int:
        """Bars needed before a zone transition may be reported.

        Wilder's RSI is recursive, so the same warm-up reasoning as EMA applies: an
        early value still carries its seed and would report a transition on noise.
        ``+ 2`` covers needing the previous bar's zone as well as the current one.
        """
        return min_warmup(self.rsi_period) + 2

    def on_closed_candle(self, window: pd.DataFrame, ctx: CandleContext) -> list[Detection]:
        validate_window(window)
        if len(window) < self.min_window():
            return []

        values = rsi(window[self.price_field], self.rsi_period)
        current, previous = values.iloc[-1], values.iloc[-2]
        if _is_nan(current) or _is_nan(previous):
            return []

        current_zone = rsi_zone(
            float(current), overbought=self.overbought, oversold=self.oversold
        )
        previous_zone = rsi_zone(
            float(previous), overbought=self.overbought, oversold=self.oversold
        )
        if current_zone == previous_zone:
            return []

        event_key = self._event_key(previous_zone, current_zone)
        if event_key is None:
            # A zone flip straight from overbought to oversold (or back) in one bar is
            # possible on a violent candle but is not an "entry" or "exit" of either
            # zone; reporting it as one would mislabel it.
            return []

        return [
            self.build_detection(
                ctx=ctx,
                event_key=event_key,
                price=float(window[self.price_field].iloc[-1]),
                # Context only: this agent never asserts a direction (§14).
                direction=None,
                indicators=IndicatorSnapshot(
                    rsi=float(current),
                    extras={
                        # Numeric values stored alongside the labels, so a later
                        # threshold change does not make old detections unreadable.
                        "rsi_previous": float(previous),
                        "overbought": self.overbought,
                        "oversold": self.oversold,
                    },
                ),
            )
        ]

    def _event_key(self, previous_zone: str, current_zone: str) -> str | None:
        if current_zone == ZONE_OVERBOUGHT and previous_zone == ZONE_NEUTRAL:
            return EVENT_OVERBOUGHT_ENTRY
        if previous_zone == ZONE_OVERBOUGHT and current_zone == ZONE_NEUTRAL:
            return EVENT_OVERBOUGHT_EXIT
        if current_zone == ZONE_OVERSOLD and previous_zone == ZONE_NEUTRAL:
            return EVENT_OVERSOLD_ENTRY
        if previous_zone == ZONE_OVERSOLD and current_zone == ZONE_NEUTRAL:
            return EVENT_OVERSOLD_EXIT
        return None


def _is_nan(value: object) -> bool:
    try:
        return math.isnan(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True
