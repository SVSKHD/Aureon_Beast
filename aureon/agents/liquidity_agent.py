"""Liquidity sweep agent (§15).

A sweep is price trading **through** a level and then closing back on the side it came
from: the high exceeds a known high but the candle closes below it. The level's resting
orders are taken out and price rejects.

Emits **one detection per swept level** (§15). A single candle can sweep the previous
day's high and a swing high at once, and those are two distinct facts about two
distinct levels -- collapsing them into one detection would lose which level actually
mattered, and Phase 3 evaluates outcomes per detection.

## The two directions in this agent

They are not the same thing, and conflating them is the easy mistake here:

* ``event_key`` is ``"{direction}|{level_type}"`` where *direction* is **where price
  went** -- ``up`` through a high, ``down`` through a low;
* ``Detection.direction`` is the **implied reversal**: an up-sweep that rejects is a
  SELL bias, a down-sweep a BUY bias.

So ``event_key = "up|previous_day_high"`` carries ``direction = SELL``. This is the
standard reading of a sweep and is recorded as decision 40 for confirmation against
§15, since it is a strategy claim rather than an observation.

Levels come from the shared ``LevelTracker`` -- never computed here (§15, §17).
"""

from __future__ import annotations

import math

import pandas as pd

from aureon.agents.base_agent import BaseAgent, validate_window
from aureon.engine.levels import Level, LevelTracker
from aureon.models.detection import CandleContext, Detection, IndicatorSnapshot
from aureon.models.enums import Direction, Timeframe

SWEEP_UP = "up"
SWEEP_DOWN = "down"

# A penetration smaller than this is noise or a spread artefact, not a sweep.
DEFAULT_MIN_PENETRATION_POINTS = 5.0

# The close must come back at least this far inside the level to count as a rejection,
# expressed as a fraction of the penetration. 0.0 would accept a close sitting exactly
# on the level, which is indecision rather than rejection.
DEFAULT_MIN_REJECTION_FRACTION = 0.25


class LiquidityAgent(BaseAgent):
    """Detects levels swept and rejected (§15)."""

    agent_name = "liquidity"
    agent_version = "1.2.0"  # 11D

    def __init__(
        self,
        *,
        timeframe: Timeframe = Timeframe.M5,
        point: float = 0.01,
        level_tracker: LevelTracker | None = None,
        min_penetration_points: float = DEFAULT_MIN_PENETRATION_POINTS,
        min_rejection_fraction: float = DEFAULT_MIN_REJECTION_FRACTION,
    ) -> None:
        if point <= 0:
            raise ValueError("point must be positive")
        if not 0.0 < min_rejection_fraction <= 1.0:
            raise ValueError("min_rejection_fraction must be in (0, 1]")
        self.timeframe = timeframe
        self.point = point
        # Shared, never a private copy: the breakout agent must agree on where a level is.
        self.level_tracker = level_tracker or LevelTracker()
        self.min_penetration_points = min_penetration_points
        self.min_rejection_fraction = min_rejection_fraction

    def params_snapshot(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe.value,
            "point": self.point,
            "min_penetration_points": self.min_penetration_points,
            "min_rejection_fraction": self.min_rejection_fraction,
            "warmup_bars": self.min_window(),
            **self.level_tracker.params_snapshot(),
        }

    def bars_per_day(self) -> int:
        return math.ceil(24 * 60 / self.timeframe.minutes)

    def min_window(self) -> int:
        """Whatever the level tracker needs to know the previous day's range."""
        return self.level_tracker.min_window(bars_per_day=self.bars_per_day())

    def on_closed_candle(self, window: pd.DataFrame, ctx: CandleContext) -> list[Detection]:
        validate_window(window)
        if ctx.timeframe is not self.timeframe:
            raise ValueError(
                f"{self.agent_name} is configured for {self.timeframe.value} but received "
                f"{ctx.timeframe.value}; configure one agent per timeframe"
            )
        if len(window) < 3:
            return []

        # Levels as of the PREVIOUS bar: a level the current candle helped form cannot
        # also be a level that candle swept. Using levels that include the current bar
        # would let a candle sweep its own high, which is always true and meaningless.
        levels = self.level_tracker.levels_for(window.iloc[:-1], ctx.market_tz)
        if not len(levels):
            return []

        candle = window.iloc[-1]
        high, low, close = float(candle["high"]), float(candle["low"]), float(candle["close"])

        detections: list[Detection] = []
        for level in levels.levels.values():
            swept = (
                self._sweep_of_high(level, high=high, close=close)
                if level.is_high
                else self._sweep_of_low(level, low=low, close=close)
            )
            if swept is None:
                continue
            detections.append(self._build(ctx, level, close, swept))
        # Stable order, so replay and live agree on sequence within a candle.
        detections.sort(key=lambda d: d.event_key)
        return detections

    # ── Sweep rules ───────────────────────────────────────────────────────────

    def _sweep_of_high(
        self, level: Level, *, high: float, close: float
    ) -> dict[str, float] | None:
        penetration = (high - level.price) / self.point
        if penetration < self.min_penetration_points:
            return None
        if close >= level.price:
            return None  # closed above: a break, not a sweep -- that is the breakout agent's
        rejection = (level.price - close) / self.point
        if rejection < penetration * self.min_rejection_fraction:
            return None
        return {
            "level_price": level.price,
            "penetration_points": penetration,
            "rejection_points": rejection,
            "rejection_fraction": rejection / penetration,
        }

    def _sweep_of_low(
        self, level: Level, *, low: float, close: float
    ) -> dict[str, float] | None:
        penetration = (level.price - low) / self.point
        if penetration < self.min_penetration_points:
            return None
        if close <= level.price:
            return None
        rejection = (close - level.price) / self.point
        if rejection < penetration * self.min_rejection_fraction:
            return None
        return {
            "level_price": level.price,
            "penetration_points": penetration,
            "rejection_points": rejection,
            "rejection_fraction": rejection / penetration,
        }

    def _build(
        self, ctx: CandleContext, level: Level, close: float, measures: dict[str, float]
    ) -> Detection:
        sweep_direction = SWEEP_UP if level.is_high else SWEEP_DOWN
        # The implied reversal, NOT where price went -- see the module docstring.
        implied = Direction.SELL if level.is_high else Direction.BUY
        return self.build_detection(
            ctx=ctx,
            event_key=f"{sweep_direction}|{level.level_type}",
            price=close,
            direction=implied,
            indicators=IndicatorSnapshot(extras=dict(measures)),
            levels={level.level_type: level.price},
        )
