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

    # ── Daily/session context (additive, version-safe) ────────────────────────
    # Frozen from the Daily Market Bias Agent at setup time. Every field is optional so
    # examples written before the agent existed still validate; the encoder treats a
    # missing value as "unknown" rather than inventing a bias after the fact.
    daily_bias_at_entry: str | None = None
    daily_bias_strength_at_entry: float | None = Field(default=None, ge=0, le=1)
    current_session: str | None = None
    session_bias_at_entry: str | None = None
    session_bias_strength_at_entry: float | None = Field(default=None, ge=0, le=1)
    preferred_direction_at_entry: str | None = None
    entry_aligned_with_daily_bias: bool | None = None
    entry_aligned_with_session_bias: bool | None = None
    trend_quality_at_entry: str | None = None
    opportunity_quality_at_entry: float | None = Field(default=None, ge=0, le=1)
    reversal_risk_at_entry: str | None = None
    previous_session_bias: str | None = None
    session_transition_state: str | None = None
    volatility_state_at_entry: str | None = None
    best_session_so_far: str | None = None
    best_direction_so_far: str | None = None

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


class EntryDecision(StrEnum):
    """What the Champion recommends. ExecutionGuard remains the execution authority."""

    ENTER = "ENTER"
    WAIT = "WAIT"
    REJECT = "REJECT"
    HOLD_EXISTING_POSITION = "HOLD_EXISTING_POSITION"


class CoverageStatus(StrEnum):
    """How well the current context is represented in the Champion's training data."""

    NORMAL_COVERAGE = "NORMAL_COVERAGE"
    LOW_TRAINING_COVERAGE = "LOW_TRAINING_COVERAGE"
    OUT_OF_DISTRIBUTION = "OUT_OF_DISTRIBUTION"
    UNKNOWN = "UNKNOWN"


class CoverageAssessment(AureonModel):
    """Deterministic similarity of one context to the training set. Not a probability."""

    model_config = ConfigDict(frozen=True)

    status: CoverageStatus = CoverageStatus.UNKNOWN
    similar_samples: int = Field(default=0, ge=0)
    context_key: str = ""
    matched_dimensions: tuple[str, ...] = ()
    unseen_values: dict[str, str] = Field(
        default_factory=dict,
        description="dimension -> value that never appeared in training (an OOD signal).",
    )
    low_threshold: int = Field(default=50, ge=0)
    ood_threshold: int = Field(default=10, ge=0)
    training_samples: int = Field(default=0, ge=0)
    reason: str = ""


class EntryIntelligence(AureonModel):
    """Champion intelligence supplied to the decision/risk layer; never an order.

    Probabilities come from the trained model. ``daily_bias``/``session_bias`` and their
    strengths are the deterministic context frozen with the setup. ``training_coverage``
    says how much history looks like this moment. ``decision`` is a recommendation and
    nothing here can reach a broker.
    """

    model_id: str
    generation: int = Field(default=0, ge=0)
    setup_id: str
    symbol: str
    timeframe: Timeframe
    predicted_at: UtcDatetime
    direction: Direction

    confidence: float | None = Field(default=None, ge=0, le=1)
    probability_clean_10: float | None = Field(default=None, ge=0, le=1)
    probability_reach_5: float | None = Field(default=None, ge=0, le=1)
    probability_reach_10: float | None = Field(default=None, ge=0, le=1)
    probability_reach_20: float | None = Field(default=None, ge=0, le=1)
    probability_reach_30: float | None = Field(default=None, ge=0, le=1)
    probability_reach_40: float | None = Field(default=None, ge=0, le=1)
    probabilities: dict[str, float] = Field(default_factory=dict)
    recommended_target: int = Field(default=0, ge=0)

    supporting_agents: tuple[str, ...] = ()
    opposing_agents: tuple[str, ...] = ()

    daily_bias: str | None = None
    daily_bias_strength: float | None = Field(default=None, ge=0, le=1)
    session: str | None = None
    session_bias: str | None = None
    session_strength: float | None = Field(default=None, ge=0, le=1)
    market_regime: str | None = None
    volatility_regime: str | None = None
    htf_alignment: str | None = None

    training_coverage: CoverageAssessment = Field(default_factory=CoverageAssessment)

    decision: EntryDecision
    reason: str
    addon_opportunity: bool = Field(
        default=False,
        description="Research flag only: this setup would have been ENTER had no Aureon "
        "position been open. V1 never pyramids.",
    )
    would_enter: bool = False


class CoverageBucket(AureonModel):
    """One cell of the training-coverage report. Rates are shown WITH their sample count."""

    model_config = ConfigDict(frozen=True)

    dimension: str
    value: str
    sample_count: int = Field(default=0, ge=0)
    clean_10_count: int = Field(default=0, ge=0)
    clean_10_rate: float | None = Field(default=None, ge=0, le=1)
    average_mae: float | None = Field(default=None, ge=0)
    average_mfe: float | None = Field(default=None, ge=0)
    reach_5_rate: float | None = Field(default=None, ge=0, le=1)
    reach_10_rate: float | None = Field(default=None, ge=0, le=1)
    reach_20_rate: float | None = Field(default=None, ge=0, le=1)
    reach_30_rate: float | None = Field(default=None, ge=0, le=1)
    reach_40_rate: float | None = Field(default=None, ge=0, le=1)
    small_sample: bool = Field(
        default=False,
        description="True when sample_count is below the minimum for a trustworthy rate.",
    )


class GenerationComparison(AureonModel):
    """Historical evidence comparing two model generations on the same frozen examples.

    This is governance evidence, not a forecast: it says how each model would have
    scored the same past setups, never that the challenger will do better next month.
    """

    previous_model_id: str | None
    challenger_model_id: str
    symbol: str
    previous_training_period: tuple[str | None, str | None] = (None, None)
    new_learning_period: tuple[str | None, str | None] = (None, None)
    samples_before: int = Field(default=0, ge=0)
    samples_added: int = Field(default=0, ge=0)
    comparison_samples: int = Field(default=0, ge=0)
    previous_metrics: dict = Field(default_factory=dict)
    challenger_metrics: dict = Field(default_factory=dict)
    previous_buckets: dict = Field(default_factory=dict)
    challenger_buckets: dict = Field(default_factory=dict)
    regimes_improved: tuple[str, ...] = ()
    regimes_degraded: tuple[str, ...] = ()
    sessions_improved: tuple[str, ...] = ()
    sessions_degraded: tuple[str, ...] = ()
    directional_contexts_improved: tuple[str, ...] = ()
    directional_contexts_degraded: tuple[str, ...] = ()
    new_failure_patterns: tuple[str, ...] = ()
    previous_failure_patterns_reduced: tuple[str, ...] = ()
    generated_at: UtcDatetime
    disclaimer: str = (
        "Historical evidence and model governance only; not a prediction of future "
        "performance."
    )


class LearningExamStatus(StrEnum):
    OPEN = "open"
    SCORED = "scored"
    RELEASED = "released"


class LearningExam(AureonDocument):
    """A held-out period a frozen Champion is examined on before it may be learned from.

    While OPEN, examples inside ``[period_from, period_to]`` are refused by V1 training.
    SCORED means the frozen model's predictions were recorded and scored against the
    period's outcomes. RELEASED means the period may now enter Challenger training.
    """

    exam_id: str
    symbol: str
    period_from: str
    period_to: str
    frozen_model_id: str | None = None
    status: LearningExamStatus = LearningExamStatus.OPEN
    created_at: UtcDatetime
    scored_at: UtcDatetime | None = None
    released_at: UtcDatetime | None = None
    metrics: dict = Field(default_factory=dict)


class PendingLearningSetup(AureonDocument):
    """Durable live outcome accumulator for one frozen setup."""

    learning_id: str
    setup_id: str
    symbol: str
    timeframe: Timeframe
    market_date: str
    feature_schema: str = FEATURE_SCHEMA_V1
    label_schema: str = LABEL_SCHEMA_V1
    features: FeatureSnapshotV1
    horizon_bars: int = Field(default=864, ge=1)
    bars_seen: int = Field(default=0, ge=0)
    status: str = "pending"

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
    path_ambiguous: bool = False

    created_at: UtcDatetime
    updated_at: UtcDatetime
