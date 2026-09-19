"""Breakout agent (§17).

The mirror of the liquidity agent: where a sweep pierces a level and closes back
inside, a breakout **closes beyond** it. The two are mutually exclusive by
construction on any given level and candle -- the close is either beyond the level or
it is not -- so one candle can never produce both a sweep and a breakout of the same
level. That is checked by a test, because a bar reported as both would be
self-contradictory and would double-count in every review.

``event_key`` is ``"{direction}|{level_type}"`` where *direction* is where price broke:
``up`` through a high, ``down`` through a low. Unlike a sweep, ``Detection.direction``
agrees with it -- a break upward is a BUY bias, a continuation rather than a reversal.

Levels come from the shared ``LevelTracker`` (§15, §17). Never a second implementation:
the breakout and liquidity agents must agree on where a level is, or one will report a
break of a high the other never considered a high.
"""

from __future__ import annotations

import math

import pandas as pd

from aureon.agents.base_agent import BaseAgent, validate_window
from aureon.engine.levels import Level, LevelTracker
from aureon.models.detection import CandleContext, Detection, IndicatorSnapshot
from aureon.models.enums import Direction, Timeframe

BREAK_UP = "up"
BREAK_DOWN = "down"

# The close must clear the level by at least this much. A close a tick beyond is
# indistinguishable from spread noise, and would fire on every brush of the level.
DEFAULT_MIN_CLOSE_BEYOND_POINTS = 10.0


class BreakoutAgent(BaseAgent):
    """Detects levels broken and held through the close (§17)."""

    agent_name = "breakout"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        timeframe: Timeframe = Timeframe.M5,
        point: float = 0.01,
        level_tracker: LevelTracker | None = None,
        min_close_beyond_points: float = DEFAULT_MIN_CLOSE_BEYOND_POINTS,
    ) -> None:
        if point <= 0:
            raise ValueError("point must be positive")
        self.timeframe = timeframe
        self.point = point
        self.level_tracker = level_tracker or LevelTracker()
        self.min_close_beyond_points = min_close_beyond_points

    def params_snapshot(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe.value,
            "point": self.point,
            "min_close_beyond_points": self.min_close_beyond_points,
            "warmup_bars": self.min_window(),
            **self.level_tracker.params_snapshot(),
        }

    def bars_per_day(self) -> int:
        return math.ceil(24 * 60 / self.timeframe.minutes)

    def min_window(self) -> int:
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

        # Levels as of the previous bar, for the same reason as the liquidity agent: a
        # candle must not be able to break a level it helped create.
        levels = self.level_tracker.levels_for(window.iloc[:-1], ctx.market_tz)
        if not len(levels):
            return []

        candle = window.iloc[-1]
        close = float(candle["close"])
        previous_close = float(window["close"].iloc[-2])

        detections: list[Detection] = []
        for level in levels.levels.values():
            measures = self._break_of(level, close=close, previous_close=previous_close)
            if measures is None:
                continue
            detections.append(self._build(ctx, level, close, measures))
        detections.sort(key=lambda d: d.event_key)
        return detections

    def _break_of(
        self, level: Level, *, close: float, previous_close: float
    ) -> dict[str, float] | None:
        if level.is_high:
            beyond = (close - level.price) / self.point
            # The previous close must have been on the other side, or this is simply a
            # continuation of a break that already happened -- reporting it every bar
            # while price stayed above would produce hundreds of duplicates.
            already_through = previous_close > level.price
        else:
            beyond = (level.price - close) / self.point
            already_through = previous_close < level.price

        if beyond < self.min_close_beyond_points or already_through:
            return None
        return {
            "level_price": level.price,
            "close_beyond_points": beyond,
            "previous_close": previous_close,
        }

    def _build(
        self, ctx: CandleContext, level: Level, close: float, measures: dict[str, float]
    ) -> Detection:
        break_direction = BREAK_UP if level.is_high else BREAK_DOWN
        # Continuation: agrees with where price broke, unlike a sweep's reversal bias.
        implied = Direction.BUY if level.is_high else Direction.SELL
        return self.build_detection(
            ctx=ctx,
            event_key=f"{break_direction}|{level.level_type}",
            price=close,
            direction=implied,
            indicators=IndicatorSnapshot(extras=dict(measures)),
            levels={level.level_type: level.price},
        )
