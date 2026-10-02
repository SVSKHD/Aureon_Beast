"""Aureon V4 movement-intelligence contracts for EMA journeys.

V4 does not replace the deterministic V3 journey tracker.  It derives a richer,
future-safe view from the same immutable anchors so V3 history remains reproducible.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, Field

from aureon.models.base import AureonModel, UtcDatetime
from aureon.models.ema_journey_v3 import EMAAnchorFeaturesV3
from aureon.models.enums import Direction, Timeframe

EMA_FEATURE_SCHEMA_V4 = "AUREON_EMA_FEATURES_V4"
EMA_LABEL_SCHEMA_V4 = "AUREON_EMA_MOVEMENT_V4"
EMA_MODEL_SCHEMA_V4 = "AUREON_EMA_MODEL_V4"


class V4JourneyPhase(StrEnum):
    PRE_CROSS = "pre_cross"
    CONFIRMED_CROSS = "confirmed_cross"
    EXPANSION = "expansion"
    PULLBACK = "pullback"
    REENTRY_OBSERVATION = "reentry_observation"
    CONTINUATION = "continuation"
    EXHAUSTION = "exhaustion"
    REVERSAL = "reversal"
    CLOSED = "closed"
    INVALID = "invalid"


class PreCrossFailureType(StrEnum):
    NONE = "none"
    NO_CONFIRMED_CROSS = "no_confirmed_cross"
    EXPIRED = "expired"
    MARKET_DAY_CHANGE = "market_day_change"
    DATA_GAP = "data_gap"
    INVALID = "invalid"


class V4JourneyEvent(AureonModel):
    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=0)
    event_type: str
    detected_at: UtcDatetime
    price: float
    movement_from_start: float = 0.0


class V4MovementFeatures(AureonModel):
    """Facts knowable at one observable V4 anchor."""

    model_config = ConfigDict(frozen=True)

    feature_schema: str = EMA_FEATURE_SCHEMA_V4
    anchor_type: str
    base: EMAAnchorFeaturesV3
    movement_consumed: float = 0.0
    pre_cross_movement: float = 0.0
    pre_cross_bars: int = Field(default=0, ge=0)
    pre_cross_seconds: float = Field(default=0.0, ge=0)
    cross_order: str = "NONE"


class V4PreCrossLabels(AureonModel):
    model_config = ConfigDict(frozen=True)

    cross_within_1: bool = False
    cross_within_3: bool = False
    cross_within_6: bool = False
    cross_within_12: bool = False
    bars_to_cross: int | None = Field(default=None, ge=1)
    failure_type: PreCrossFailureType = PreCrossFailureType.NONE


class V4RemainingMovementLabels(AureonModel):
    model_config = ConfigDict(frozen=True)

    label_schema: str = EMA_LABEL_SCHEMA_V4
    reached_3: bool = False
    reached_5: bool = False
    reached_10: bool = False
    reached_20: bool = False
    reached_30: bool = False
    reached_40: bool = False
    remaining_mfe: float = Field(default=0.0, ge=0)
    remaining_mae: float = Field(default=0.0, ge=0)


class V4PreCrossExample(AureonModel):
    model_config = ConfigDict(frozen=True)

    journey_id: str
    detection_id: str
    symbol: str
    timeframe: Timeframe
    direction: Direction
    market_date: str
    features: V4MovementFeatures
    labels: V4PreCrossLabels


class V4RemainingMovementExample(AureonModel):
    model_config = ConfigDict(frozen=True)

    journey_id: str
    detection_id: str
    symbol: str
    timeframe: Timeframe
    direction: Direction
    market_date: str
    features: V4MovementFeatures
    labels: V4RemainingMovementLabels


class V4JourneySnapshot(AureonModel):
    """Derived V4 view of one persisted V3 journey."""

    model_config = ConfigDict(frozen=True)

    journey_id: str
    symbol: str
    timeframe: Timeframe
    direction: Direction
    phase: V4JourneyPhase
    event_sequence: tuple[V4JourneyEvent, ...] = ()
    pre_cross_movement: float = 0.0
    pre_cross_bars: int = Field(default=0, ge=0)
    pre_cross_seconds: float = Field(default=0.0, ge=0)
    movement_consumed_at_latest_cross: float | None = None
    cross_order: str = "NONE"
