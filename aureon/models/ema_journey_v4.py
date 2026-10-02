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


class V4PullbackClass(StrEnum):
    SHALLOW = "shallow_pullback"
    NORMAL = "normal_pullback"
    DEEP = "deep_pullback"


class V4PullbackStatus(StrEnum):
    ACTIVE = "active"
    REENTRY_OBSERVED = "reentry_observed"
    CONTINUED = "continued"
    FAILED = "failed"
    CLOSED = "closed"


class V4RetestKind(StrEnum):
    EMA20 = "ema20_retest"
    EMA50 = "ema50_retest"
    EMA200 = "ema200_retest"
    STRUCTURE = "structure_retest"


class V4VolumeTrajectory(AureonModel):
    """Volume facts separated by phase; MT5 tick volume remains relative context."""

    pre_cross_3bar_mean: float | None = None
    pre_cross_5bar_mean: float | None = None
    cross_tick_volume: float | None = None
    cross_volume_ratio: float | None = None
    expansion_volume_sum: float = Field(default=0.0, ge=0)
    expansion_volume_bars: int = Field(default=0, ge=0)
    expansion_volume_mean: float | None = Field(default=None, ge=0)

    def observe_expansion(self, tick_volume: float) -> None:
        value = max(0.0, float(tick_volume))
        self.expansion_volume_sum += value
        self.expansion_volume_bars += 1
        self.expansion_volume_mean = (
            self.expansion_volume_sum / self.expansion_volume_bars
        )


class V4PullbackState(AureonModel):
    """One observable retracement after a confirmed directional expansion."""

    pullback_id: str
    journey_id: str
    symbol: str
    timeframe: Timeframe
    direction: Direction
    started_at: UtcDatetime
    start_price: float
    expansion_origin_price: float
    expansion_extreme_price: float
    expansion_move: float = Field(ge=0)
    atr_at_start: float | None = Field(default=None, ge=0)
    bars: int = Field(default=0, ge=0)
    depth: float = Field(default=0.0, ge=0)
    depth_atr: float | None = Field(default=None, ge=0)
    retracement_fraction: float = Field(default=0.0, ge=0)
    classification: V4PullbackClass = V4PullbackClass.SHALLOW
    retests: tuple[V4RetestKind, ...] = ()
    structure_level: float | None = None
    structure_intact: bool = True
    ema_aligned: bool = False
    latest_price: float | None = None
    latest_at: UtcDatetime | None = None
    status: V4PullbackStatus = V4PullbackStatus.ACTIVE


class V4ReentryOutcome(AureonModel):
    bars_observed: int = Field(default=0, ge=0)
    mfe: float = Field(default=0.0, ge=0)
    mae: float = Field(default=0.0, ge=0)
    reached_3: bool = False
    reached_5: bool = False
    reached_10: bool = False
    failed: bool = False
    failure_reason: str | None = None
    completed: bool = False


class V4ReentryObservation(AureonModel):
    """Research-only pullback continuation anchor; never an execution request."""

    reentry_id: str
    pullback_id: str
    journey_id: str
    symbol: str
    timeframe: Timeframe
    direction: Direction
    observed_at: UtcDatetime
    price: float
    ema20: float
    ema50: float
    ema200: float | None = None
    atr: float | None = Field(default=None, ge=0)
    pullback_depth: float = Field(ge=0)
    pullback_fraction: float = Field(ge=0)
    pullback_class: V4PullbackClass
    structure_intact: bool
    ema_aligned: bool
    retests: tuple[V4RetestKind, ...] = ()
    volume: V4VolumeTrajectory = Field(default_factory=V4VolumeTrajectory)
    outcome: V4ReentryOutcome = Field(default_factory=V4ReentryOutcome)
