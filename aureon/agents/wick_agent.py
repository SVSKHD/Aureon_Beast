"""Wick rejection agent (§16).

**Context only** (§16): ``direction`` is always ``None``. A long wick says price was
rejected at a price, which is information about the candle, not an instruction. Phase 7
correlates it with what actually happened; this agent does not claim to know.

Emits a detection **only for rejection classifications** (§16). Most candles are
ordinary, and a detection per candle would bury the interesting ones and make the
reached-N statistics meaningless by flooding them with non-events.

## Classification

A rejection needs all three, because any one alone is common:

1. the wick is a large fraction of the candle's total range -- price went there and
   came back;
2. the wick is large relative to the body -- the move was rejected, not just a strong
   directional candle that happened to have a tail;
3. the close sits at the opposite end of the range -- confirming which side won.

Doji-ish candles with tiny bodies would otherwise pass (2) trivially, which is why (1)
and (3) are required alongside it.
"""

from __future__ import annotations

import pandas as pd

from aureon.agents.base_agent import BaseAgent, validate_window
from aureon.models.detection import CandleContext, Detection, IndicatorSnapshot

EVENT_UPPER_REJECTION = "upper_rejection"
EVENT_LOWER_REJECTION = "lower_rejection"
CLASSIFICATION_NONE = "none"

DEFAULT_MIN_WICK_RANGE_RATIO = 0.55
DEFAULT_MIN_WICK_BODY_RATIO = 1.5
DEFAULT_MAX_CLOSE_POSITION = 0.35
DEFAULT_MIN_RANGE_POINTS = 20.0


class WickAgent(BaseAgent):
    """Classifies wick rejections (§16)."""

    agent_name = "wick"
    agent_version = "1.1.0"  # 9B

    def __init__(
        self,
        *,
        point: float = 0.01,
        min_wick_range_ratio: float = DEFAULT_MIN_WICK_RANGE_RATIO,
        min_wick_body_ratio: float = DEFAULT_MIN_WICK_BODY_RATIO,
        max_close_position: float = DEFAULT_MAX_CLOSE_POSITION,
        min_range_points: float = DEFAULT_MIN_RANGE_POINTS,
    ) -> None:
        if point <= 0:
            raise ValueError("point must be positive")
        if not 0.0 < min_wick_range_ratio <= 1.0:
            raise ValueError("min_wick_range_ratio must be in (0, 1]")
        if not 0.0 < max_close_position < 0.5:
            # At or above 0.5 the close is mid-range, which is indecision; calling that
            # a rejection would classify half of all candles as one.
            raise ValueError("max_close_position must be in (0, 0.5)")
        self.point = point
        self.min_wick_range_ratio = min_wick_range_ratio
        self.min_wick_body_ratio = min_wick_body_ratio
        self.max_close_position = max_close_position
        self.min_range_points = min_range_points

    def params_snapshot(self) -> dict[str, object]:
        return {
            "point": self.point,
            "min_wick_range_ratio": self.min_wick_range_ratio,
            "min_wick_body_ratio": self.min_wick_body_ratio,
            "max_close_position": self.max_close_position,
            "min_range_points": self.min_range_points,
            "warmup_bars": self.min_window(),
        }

    def min_window(self) -> int:
        """One candle. Wick structure is a property of the bar itself."""
        return 1

    def on_closed_candle(self, window: pd.DataFrame, ctx: CandleContext) -> list[Detection]:
        validate_window(window)
        if window.empty:
            return []

        candle = window.iloc[-1]
        high, low = float(candle["high"]), float(candle["low"])
        open_, close = float(candle["open"]), float(candle["close"])

        total_range = high - low
        if total_range <= 0 or total_range / self.point < self.min_range_points:
            # A doji at the tick level has no meaningful structure, and dividing by a
            # zero range would blow up below.
            return []

        body = abs(close - open_)
        upper_wick = high - max(open_, close)
        lower_wick = min(open_, close) - low
        # 0 at the low, 1 at the high.
        close_position = (close - low) / total_range

        classification, wick = self._classify(
            upper_wick=upper_wick,
            lower_wick=lower_wick,
            body=body,
            total_range=total_range,
            close_position=close_position,
        )
        if classification == CLASSIFICATION_NONE:
            return []

        return [
            self.build_detection(
                ctx=ctx,
                event_key=classification,
                price=close,
                # Context only (§16).
                direction=None,
                indicators=IndicatorSnapshot(
                    extras={
                        # Numeric values alongside the label, so retuning a threshold
                        # does not make old detections unreadable.
                        "wick_points": wick / self.point,
                        "body_points": body / self.point,
                        "range_points": total_range / self.point,
                        "wick_range_ratio": wick / total_range,
                        # A zero body gives an unbounded ratio; the body in points is
                        # stored alongside, so a reader can see it was zero.
                        "wick_body_ratio": (wick / body) if body > 0 else -1.0,
                        "close_position": close_position,
                        "upper_wick_points": upper_wick / self.point,
                        "lower_wick_points": lower_wick / self.point,
                    }
                ),
                levels={"rejected_price": high if classification == EVENT_UPPER_REJECTION else low},
            )
        ]

    def _classify(
        self,
        *,
        upper_wick: float,
        lower_wick: float,
        body: float,
        total_range: float,
        close_position: float,
    ) -> tuple[str, float]:
        """Return the classification and the wick that produced it."""
        # A zero body makes the wick/body ratio infinite; treat it as passing that test
        # rather than dividing by zero, since the other two tests still gate it.
        def body_ratio(wick: float) -> float:
            return wick / body if body > 0 else float("inf")

        upper_ok = (
            upper_wick / total_range >= self.min_wick_range_ratio
            and body_ratio(upper_wick) >= self.min_wick_body_ratio
            and close_position <= self.max_close_position
        )
        lower_ok = (
            lower_wick / total_range >= self.min_wick_range_ratio
            and body_ratio(lower_wick) >= self.min_wick_body_ratio
            and close_position >= 1.0 - self.max_close_position
        )
        # Both cannot hold: the close cannot sit in the bottom third and the top third
        # at once. The guard is kept so a future threshold change cannot produce two
        # contradictory detections for one candle.
        if upper_ok and lower_ok:
            return (
                (EVENT_UPPER_REJECTION, upper_wick)
                if upper_wick >= lower_wick
                else (EVENT_LOWER_REJECTION, lower_wick)
            )
        if upper_ok:
            return EVENT_UPPER_REJECTION, upper_wick
        if lower_ok:
            return EVENT_LOWER_REJECTION, lower_wick
        return CLASSIFICATION_NONE, 0.0
