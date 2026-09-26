"""EOD training memory built from frozen setup evidence and later outcomes.

The training layer is deliberately downstream of observation. It may read a setup, its setup
events and its completed evaluation, but it never edits any of them. That keeps future outcome
information out of the records that represented what Aureon knew at decision time.
"""

from __future__ import annotations

from pydantic import Field

from aureon.models.base import AureonDocument, AureonModel, UtcDatetime
from aureon.models.enums import DirectionContext, SetupFamily, Timeframe


class CanonicalOutcome(AureonModel):
    """Versioned V1 outcome. Measured only after the frozen setup snapshot."""

    clean_10: bool
    reached_5: bool
    reached_10: bool
    reached_20: bool
    reached_30: bool
    reached_40: bool
    mae_before_10: float | None = Field(default=None, ge=0)
    max_favourable_move: float = Field(default=0.0, ge=0)
    max_adverse_move: float = Field(default=0.0, ge=0)
    bars_to_5: int | None = Field(default=None, ge=1)
    bars_to_10: int | None = Field(default=None, ge=1)
    bars_to_20: int | None = Field(default=None, ge=1)
    bars_to_30: int | None = Field(default=None, ge=1)
    bars_to_40: int | None = Field(default=None, ge=1)
    ambiguous_clean_10_bar: bool = False


class TrainingExample(AureonDocument):
    """One immutable EOD training row for one setup under one feature/label contract."""

    example_id: str
    market_date: str
    symbol: str
    timeframe: Timeframe
    setup_id: str
    family: SetupFamily
    direction_context: DirectionContext
    setup_version: str

    feature_schema_version: str = "EOD_SETUP_FEATURES_V1"
    label_schema_version: str = "FAVOURABLE_MOVE_LADDER_V2"

    context: dict = Field(default_factory=dict)
    agent_read: dict = Field(default_factory=dict)

    # V1 canonical contract. Legacy rows keep these empty/null and retain their original
    # feature/label schema values, so historical meaning is never silently rewritten.
    features: dict = Field(default_factory=dict)
    outcome: CanonicalOutcome | None = None
    setup_created_at: UtcDatetime | None = None
    feature_frozen_at: UtcDatetime | None = None
    outcome_resolved_at: UtcDatetime | None = None
    entry_price: float | None = None
    direction: str | None = None

    six_dollar_status: str = Field(
        description=(
            "reached, not_reached_eod, or unavailable. EOD is the frozen label horizon."
        )
    )
    six_dollar_reached: bool | None = None
    six_dollar_reference_price: float | None = None
    six_dollar_threshold_price: float | None = None
    six_dollar_reached_at: UtcDatetime | None = None
    time_to_six_seconds: float | None = Field(default=None, ge=0)
    twenty_dollar_reached: bool | None = None
    forty_dollar_reached: bool | None = None
    time_to_twenty_seconds: float | None = Field(default=None, ge=0)
    time_to_forty_seconds: float | None = Field(default=None, ge=0)
    max_favourable_move_price: float | None = Field(default=None, ge=0)
    extension_after_six_price: float | None = Field(default=None, ge=0)

    mfe_points: float | None = None
    mae_points: float | None = None
    mae_before_six_price: float | None = Field(
        default=None,
        description=(
            "Maximum adverse quote-price excursion from the frozen detection reference "
            "before +6 was first observed. Null when the bar history cannot prove it."
        ),
    )

    evaluation_rule_id: str | None = None
    evaluation_complete: bool = False
    generated_at: UtcDatetime


class TrainingTimeframeStatus(AureonModel):
    timeframe: Timeframe
    setups: int = Field(default=0, ge=0)
    reached_six: int = Field(default=0, ge=0)
    not_reached_six: int = Field(default=0, ge=0)
    unavailable_six: int = Field(default=0, ge=0)
    complete_evaluations: int = Field(default=0, ge=0)
    mae_before_six_available: int = Field(default=0, ge=0)
    median_mae_before_six_price: float | None = Field(default=None, ge=0)
    max_mae_before_six_price: float | None = Field(default=None, ge=0)


class DailyTrainingStatus(AureonDocument):
    """One durable EOD checkpoint so a restart does not train the same day ambiguously."""

    status_id: str
    market_date: str
    symbol: str
    feature_schema_version: str = "EOD_SETUP_FEATURES_V1"
    label_schema_version: str = "FAVOURABLE_MOVE_LADDER_V2"

    examples_written: int = Field(default=0, ge=0)
    reached_six: int = Field(default=0, ge=0)
    not_reached_six: int = Field(default=0, ge=0)
    unavailable_six: int = Field(default=0, ge=0)
    complete_evaluations: int = Field(default=0, ge=0)
    mae_before_six_available: int = Field(default=0, ge=0)
    median_mae_before_six_price: float | None = Field(default=None, ge=0)
    max_mae_before_six_price: float | None = Field(default=None, ge=0)

    by_timeframe: tuple[TrainingTimeframeStatus, ...] = ()
    generated_at: UtcDatetime

class WeeklyAgentMovementRow(AureonModel):
    """One detector/timeframe row in the weekly movement-ladder report."""

    agent_name: str
    timeframe: Timeframe
    decisions: int = Field(default=0, ge=0)
    aligned_decisions: int = Field(default=0, ge=0)
    opposed_decisions: int = Field(default=0, ge=0)
    reached_six: int = Field(default=0, ge=0)
    reached_twenty: int = Field(default=0, ge=0)
    reached_forty: int = Field(default=0, ge=0)
    max_move_available: int = Field(default=0, ge=0)
    median_max_move_price: float | None = Field(default=None, ge=0)
    maximum_move_price: float | None = Field(default=None, ge=0)
    median_extension_after_six_price: float | None = Field(default=None, ge=0)


class WeeklyTrainingReport(AureonModel):
    symbol: str
    iso_year: int
    iso_week: int
    start_market_date: str
    end_market_date: str
    examples: int = Field(default=0, ge=0)
    rows: tuple[WeeklyAgentMovementRow, ...] = ()

