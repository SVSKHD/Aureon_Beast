"""Canonical Aureon V1 learning contracts.

These schemas are additive. Legacy EOD/+6 training rows stay readable and keep their
original meaning; V1 examples use new schema identifiers and are never silently
reinterpreted.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, Field

from aureon.models.base import AureonDocument, AureonModel, UtcDatetime
from aureon.models.enums import Direction, Timeframe

FEATURE_SCHEMA_V1 = "AUREON_FEATURES_V1"
LABEL_SCHEMA_V1 = "AUREON_CLEAN_MOVE_V1"
MODEL_SCHEMA_V1 = "AUREON_ENTRY_MODEL_V1"


class ModelLifecycleStatus(StrEnum):
    CANDIDATE = "candidate"
    CHALLENGER = "challenger"
    SHADOW = "shadow"
    CHAMPION = "champion"
    RETIRED = "retired"
    REJECTED = "rejected"


class AgentFeatureState(AureonModel):
    """One agent's frozen setup-time state."""

    model_config = ConfigDict(extra="allow", frozen=True)

    stance: str | None = None
    confidence: float | None = None
    alignment: str | None = None
    observation: str | None = None
    state: str | None = None


class FeatureSnapshotV1(AureonModel):
    """Complete setup-time state consumed by V1 entry learning.

    Everything here is frozen at setup time. Later outcome builders must receive this
    object; they never recompute it from future candles.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    feature_schema: str = FEATURE_SCHEMA_V1
    setup_id: str
    symbol: str
    timeframe: Timeframe
    timestamp: UtcDatetime
    frozen_at: UtcDatetime
    direction: Direction
    reference_price: float

    ema_fast: float | None = None
    ema_slow: float | None = None
    ema_gap: float | None = None
    ema_gap_change: float | None = None
    ema_slope: float | None = None
    cross_direction: str | None = None
    bars_since_cross: int | None = Field(default=None, ge=0)

    rsi: float | None = None
    rsi_zone: str | None = None
    rsi_change: float | None = None

    atr: float | None = Field(default=None, ge=0)
    ema_gap_atr: float | None = None
    volatility_regime: str | None = None

    session: str | None = None
    time_of_day: str | None = None
    day_of_week: int | None = Field(default=None, ge=0, le=6)

    htf_trend: str | None = None
    htf_alignment: str | None = None
    market_regime: str | None = None

    wick_state: str | None = None
    wick_direction: str | None = None
    wick_strength: float | None = None

    liquidity_state: str | None = None
    liquidity_sweep: bool | None = None
    liquidity_detail: str | None = None

    breakout_state: str | None = None
    breakout_direction: str | None = None
    breakout_strength: float | None = None

    participation_state: str | None = None
    market_structure_state: str | None = None

    supporting_agents: int = Field(default=0, ge=0)
    opposing_agents: int = Field(default=0, ge=0)
    neutral_agents: int = Field(default=0, ge=0)

    agents: dict[str, AgentFeatureState] = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)


class CleanMoveOutcomeV1(AureonModel):
    """Canonical post-setup outcome.

    clean_10 means +$10 was reached and adverse excursion before the first +$10
    did not exceed the configured $7 boundary. Same-candle target/adverse ordering
    that cannot be proven is marked path_ambiguous and is not considered clean.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label_schema: str = LABEL_SCHEMA_V1
    resolved_at: UtcDatetime | None = None
    horizon_bars: int = Field(ge=1)
    clean_target: float = Field(default=10.0, gt=0)
    clean_max_mae: float = Field(default=7.0, ge=0)

    clean_10: bool
    path_ambiguous: bool = False
    reached_5: bool = False
    reached_10: bool = False
    reached_20: bool = False
    reached_30: bool = False
    reached_40: bool = False

    bars_to_5: int | None = Field(default=None, ge=1)
    bars_to_10: int | None = Field(default=None, ge=1)
    bars_to_20: int | None = Field(default=None, ge=1)
    bars_to_30: int | None = Field(default=None, ge=1)
    bars_to_40: int | None = Field(default=None, ge=1)

    max_favourable_move: float = Field(default=0.0, ge=0)
    max_adverse_move: float = Field(default=0.0, ge=0)
    mae_before_10: float | None = Field(default=None, ge=0)


class CanonicalTrainingExample(AureonDocument):
    """One immutable V1 learning example used by historical and live pipelines."""

    example_id: str
    market_date: str
    setup_id: str
    symbol: str
    timeframe: Timeframe
    feature_schema: str = FEATURE_SCHEMA_V1
    label_schema: str = LABEL_SCHEMA_V1
    features: FeatureSnapshotV1
    outcome: CleanMoveOutcomeV1
    generated_at: UtcDatetime


class EvolutionDecision(AureonDocument):
    """Persistent audit row for a model-governance action."""

    decision_id: str
    symbol: str
    model_id: str
    champion_model_id: str | None = None
    action: str
    reason: str
    metrics: dict = Field(default_factory=dict)
    created_at: UtcDatetime
