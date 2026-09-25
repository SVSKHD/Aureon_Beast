"""EOD training memory built from frozen setup evidence and later outcomes.

The training layer is deliberately downstream of observation. It may read a setup, its setup
events and its completed evaluation, but it never edits any of them. That keeps future outcome
information out of the records that represented what Aureon knew at decision time.
"""

from __future__ import annotations

from pydantic import Field

from aureon.models.base import AureonDocument, AureonModel, UtcDatetime
from aureon.models.enums import DirectionContext, SetupFamily, Timeframe


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
    label_schema_version: str = "FAVOURABLE_MOVE_6_V1"

    context: dict = Field(default_factory=dict)
    agent_read: dict = Field(default_factory=dict)

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


class DailyTrainingStatus(AureonDocument):
    """One durable EOD checkpoint so a restart does not train the same day ambiguously."""

    status_id: str
    market_date: str
    symbol: str
    feature_schema_version: str = "EOD_SETUP_FEATURES_V1"
    label_schema_version: str = "FAVOURABLE_MOVE_6_V1"

    examples_written: int = Field(default=0, ge=0)
    reached_six: int = Field(default=0, ge=0)
    not_reached_six: int = Field(default=0, ge=0)
    unavailable_six: int = Field(default=0, ge=0)
    complete_evaluations: int = Field(default=0, ge=0)
    mae_before_six_available: int = Field(default=0, ge=0)

    by_timeframe: tuple[TrainingTimeframeStatus, ...] = ()
    generated_at: UtcDatetime
