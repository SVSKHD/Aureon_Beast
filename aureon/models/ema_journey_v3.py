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
EMA_MOVEMENT_TARGETS: tuple[float, ...] = (3.0, 5.0, 10.0, 20.0, 30.0, 40.0)
EMA_FEATURE_SCHEMA_V3 = "AUREON_EMA_FEATURES_V3"
EMA_LABEL_SCHEMA_V3 = "AUREON_EMA_MOVEMENT_V3"
EMA_MODEL_SCHEMA_V3 = "AUREON_EMA_MODEL_V3"





class AgentConfidenceSnapshot(AureonModel):
    """Same-candle deterministic agent agreement; never a probability."""

    model_config = ConfigDict(frozen=True)

    label: str = "INSUFFICIENT"
    supportive: int = Field(default=0, ge=0)
    neutral: int = Field(default=0, ge=0)
    conflicting: int = Field(default=0, ge=0)
    coverage: int = Field(default=0, ge=0)
    votes: dict[str, str] = Field(default_factory=dict)


class EMAAnchorFeaturesV3(AureonModel):
    """Immutable, no-lookahead feature snapshot frozen at event time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    feature_schema: str = EMA_FEATURE_SCHEMA_V3
    trend_direction: str = "UNKNOWN"
    pattern: str = "UNKNOWN"
    cross_quality: str = "UNKNOWN"

    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    ema_gap: float | None = None
    ema_gap_change: float | None = None
    ema_slope: float | None = None
    price_vs_ema200: str = "UNKNOWN"
    ema20_50_relation: str = "UNKNOWN"

    rsi: float | None = None
    rsi_change: float | None = None

    atr: float | None = None
    volatility_regime: str = "UNKNOWN"
    tick_volume: float | None = None
    volume_ratio_to_median: float | None = None
    volume_percentile: float | None = None
    volume_state: str = "UNKNOWN"
    volume_price_alignment: str = "UNKNOWN"
    pre_cross_volume_3bar_mean: float | None = None
    pre_cross_volume_5bar_mean: float | None = None
    spread_points: float | None = None
    high_impact_news: bool = False
    news_event: str | None = None
    minutes_to_news: float | None = None
    session: str = "UNKNOWN"
    session_phase: str = "UNKNOWN"
    market_structure: str = "UNKNOWN"
    htf_alignment: str = "UNKNOWN"

    internal_agents: dict[str, str] = Field(default_factory=dict)
    agent_confidence: AgentConfidenceSnapshot = Field(
        default_factory=AgentConfidenceSnapshot
    )


class EMAAnchorType(StrEnum):
    PRE_CROSS = "pre_cross"
    EMA20_50_CROSS = "ema20_50_cross"
    EMA200_CROSS = "ema200_cross"


class JourneyStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    INVALID = "invalid"


class JourneyOrigin(StrEnum):
    PRE_CROSS = "pre_cross"
    CONFIRMED_CROSS = "confirmed_cross"


class JourneyEndReason(StrEnum):
    OPPOSITE_CONFIRMED_CROSS = "opposite_confirmed_cross"
    PRE_CROSS_EXPIRED = "pre_cross_expired"
    MAX_HORIZON = "max_horizon"
    MARKET_DAY_CHANGE = "market_day_change"
    DATA_GAP = "data_gap"


class TargetOutcome(AureonModel):
    model_config = ConfigDict(frozen=True)

    reached: bool = False
    bars_to: int | None = Field(default=None, ge=1)
    seconds_to: float | None = Field(default=None, ge=0)
    adverse_before_reach: float = Field(default=0.0, ge=0)


class EMAAnchorOutcome(AureonModel):
    """Future-only movement measured from one EMA event anchor."""

    model_config = ConfigDict(extra="forbid")

    bars_observed: int = Field(default=0, ge=0)
    mfe: float = Field(default=0.0, ge=0)
    mae: float = Field(default=0.0, ge=0)
    first_favourable_bar: int | None = Field(default=None, ge=1)
    dollars_per_bar: float | None = Field(default=None, ge=0)
    reference_price: float | None = None
    reference_price_kind: str = "detection_close"
    spread_accounted: bool = False
    target_ladder: tuple[float, ...] = ()
    target_ladder_source: str = "unknown"
    valid: bool = True
    invalid_reason: str | None = None
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
    reference_price: float | None = None
    reference_price_kind: str = "detection_close"
    spread_points: float | None = None
    point_size: float | None = None
    movement_from_journey_start: float = 0.0
    features: EMAAnchorFeaturesV3 = Field(default_factory=EMAAnchorFeaturesV3)
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
    origin: JourneyOrigin = JourneyOrigin.CONFIRMED_CROSS
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
    def has_pre_cross(self) -> bool:
        return any(anchor.anchor_type is EMAAnchorType.PRE_CROSS for anchor in self.anchors)

    @property
    def has_confirmed_cross(self) -> bool:
        return any(
            anchor.anchor_type in {
                EMAAnchorType.EMA20_50_CROSS,
                EMAAnchorType.EMA200_CROSS,
            }
            for anchor in self.anchors
        )

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
