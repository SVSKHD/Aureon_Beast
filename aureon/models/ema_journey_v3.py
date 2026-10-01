"""Aureon V3 EMA movement-journey contracts.

One journey links a developing pre-cross pressure event and later EMA20/50 or
price/EMA200 crosses that belong to the same directional market move.

The anchor snapshot is immutable. Only its future-only outcome accumulator changes.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, Field

from aureon.models.base import AureonDocument, AureonModel, UtcDatetime
from aureon.models.enums import Direction, Timeframe

EMA_JOURNEY_SCHEMA_V1 = "AUREON_EMA_JOURNEY_V1"
EMA_MOVEMENT_TARGETS: tuple[float, ...] = (5.0, 10.0, 20.0, 30.0, 40.0)


class EMAAnchorType(StrEnum):
    PRE_CROSS = "pre_cross"
    EMA20_50_CROSS = "ema20_50_cross"
    EMA200_CROSS = "ema200_cross"


class JourneyStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    INVALID = "invalid"


class JourneyEndReason(StrEnum):
    OPPOSITE_CONFIRMED_CROSS = "opposite_confirmed_cross"
    MAX_HORIZON = "max_horizon"
    MARKET_DAY_CHANGE = "market_day_change"
    DATA_GAP = "data_gap"


class TargetOutcome(AureonModel):
    model_config = ConfigDict(frozen=True)

    reached: bool = False
    bars_to: int | None = Field(default=None, ge=1)
    seconds_to: float | None = Field(default=None, ge=0)


class EMAAnchorOutcome(AureonModel):
    """Future-only movement measured from one EMA event anchor."""

    model_config = ConfigDict(extra="forbid")

    bars_observed: int = Field(default=0, ge=0)
    mfe: float = Field(default=0.0, ge=0)
    mae: float = Field(default=0.0, ge=0)
    targets: dict[str, TargetOutcome] = Field(default_factory=dict)
    completed: bool = False
    end_reason: JourneyEndReason | None = None
    completed_at: UtcDatetime | None = None


class EMAJourneyAnchor(AureonModel):
    """One immutable detection anchor plus its future-only outcome."""

    model_config = ConfigDict(extra="forbid")

    detection_id: str
    anchor_type: EMAAnchorType
    direction: Direction
    detected_at: UtcDatetime
    price: float
    movement_from_journey_start: float = 0.0
    outcome: EMAAnchorOutcome = Field(default_factory=EMAAnchorOutcome)


class EMAMovementJourney(AureonDocument):
    """One directional move containing one or more related EMA anchors."""

    model_config = ConfigDict(extra="forbid")

    journey_schema: str = EMA_JOURNEY_SCHEMA_V1
    journey_id: str
    account_scope: str
    symbol: str
    timeframe: Timeframe
    direction: Direction
    market_date: str
    started_at: UtcDatetime
    start_price: float
    anchors: tuple[EMAJourneyAnchor, ...] = ()
    status: JourneyStatus = JourneyStatus.OPEN
    ended_at: UtcDatetime | None = None
    end_reason: JourneyEndReason | None = None
    max_favourable_move: float = Field(default=0.0, ge=0)
    max_adverse_move: float = Field(default=0.0, ge=0)
    bars_observed: int = Field(default=0, ge=0)
    last_candle_close_at: UtcDatetime | None = None

    @property
    def latest_anchor(self) -> EMAJourneyAnchor | None:
        return self.anchors[-1] if self.anchors else None

    @property
    def movement_consumed_before_latest_cross(self) -> float | None:
        for anchor in reversed(self.anchors):
            if anchor.anchor_type in {
                EMAAnchorType.EMA20_50_CROSS,
                EMAAnchorType.EMA200_CROSS,
            }:
                return anchor.movement_from_journey_start
        return None

    @property
    def movement_remaining_after_latest_cross(self) -> float | None:
        for anchor in reversed(self.anchors):
            if anchor.anchor_type in {
                EMAAnchorType.EMA20_50_CROSS,
                EMAAnchorType.EMA200_CROSS,
            }:
                return anchor.outcome.mfe if anchor.outcome.completed else None
        return None
