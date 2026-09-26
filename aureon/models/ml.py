"""Versioned ML research contracts for Aureon's offline/shadow model layer."""

from __future__ import annotations

from pydantic import Field

from aureon.models.base import AureonDocument, AureonModel, UtcDatetime
from aureon.models.enums import Timeframe


class TargetMetrics(AureonModel):
    samples: int = Field(default=0, ge=0)
    positives: int = Field(default=0, ge=0)
    negatives: int = Field(default=0, ge=0)
    accuracy: float | None = Field(default=None, ge=0, le=1)
    precision: float | None = Field(default=None, ge=0, le=1)
    recall: float | None = Field(default=None, ge=0, le=1)
    brier: float | None = Field(default=None, ge=0)
    log_loss: float | None = Field(default=None, ge=0)
    roc_auc: float | None = Field(default=None, ge=0, le=1)
    false_positive_rate: float | None = Field(default=None, ge=0, le=1)
    average_mae: float | None = Field(default=None, ge=0)
    average_mfe: float | None = Field(default=None, ge=0)


class ModelRegistryEntry(AureonDocument):
    model_id: str
    symbol: str
    algorithm: str = "logistic_regression_v1"
    status: str = Field(
        description="candidate, challenger, shadow, champion, retired, or rejected"
    )
    feature_schema_version: str
    label_schema_version: str
    model_schema_version: str = "AUREON_MOVE_MODEL_V1"
    parent_model_id: str | None = None
    hyperparameters: dict = Field(default_factory=dict)

    trained_from: str
    trained_through: str
    training_samples: int = Field(ge=0)
    target_metrics: dict[str, TargetMetrics] = Field(default_factory=dict)
    validation_metrics: dict = Field(default_factory=dict)
    shadow_metrics: dict = Field(default_factory=dict)
    artifact: dict = Field(default_factory=dict)
    promotion_reason: str | None = None

    created_at: UtcDatetime
    activated_at: UtcDatetime | None = None


class ModelTrainingRun(AureonDocument):
    run_id: str
    model_id: str | None = None
    symbol: str
    status: str
    algorithm: str = "logistic_regression_v1"
    feature_schema_version: str
    label_schema_version: str
    started_at: UtcDatetime
    completed_at: UtcDatetime | None = None
    sample_count: int = Field(default=0, ge=0)
    trained_from: str | None = None
    trained_through: str | None = None
    target_metrics: dict[str, TargetMetrics] = Field(default_factory=dict)
    failure_message: str | None = None


class BacktestFold(AureonModel):
    fold: int = Field(ge=1)
    train_from: str
    train_through: str
    test_from: str
    test_through: str
    train_samples: int = Field(ge=0)
    test_samples: int = Field(ge=0)
    target_metrics: dict[str, TargetMetrics] = Field(default_factory=dict)


class ModelBacktest(AureonDocument):
    backtest_id: str
    model_id: str | None = None
    symbol: str
    status: str
    algorithm: str = "logistic_regression_v1"
    feature_schema_version: str
    label_schema_version: str
    started_at: UtcDatetime
    completed_at: UtcDatetime | None = None
    start_market_date: str | None = None
    end_market_date: str | None = None
    folds: tuple[BacktestFold, ...] = ()
    aggregate_metrics: dict[str, TargetMetrics] = Field(default_factory=dict)
    out_of_sample_predictions: int = Field(default=0, ge=0)
    failure_message: str | None = None


class ModelPrediction(AureonDocument):
    prediction_id: str
    model_id: str
    setup_id: str
    event_id: str
    symbol: str
    timeframe: Timeframe
    predicted_at: UtcDatetime
    feature_schema_version: str
    label_schema_version: str
    probabilities: dict[str, float] = Field(default_factory=dict)
    feature_snapshot: dict = Field(default_factory=dict)
    actual_outcomes: dict[str, bool | float | None] | None = None
    reconciled_at: UtcDatetime | None = None


class ShadowPredictionSummary(AureonModel):
    predictions: int = Field(default=0, ge=0)
    reconciled: int = Field(default=0, ge=0)
    six_brier: float | None = Field(default=None, ge=0)
    twenty_brier: float | None = Field(default=None, ge=0)
    forty_brier: float | None = Field(default=None, ge=0)
    clean_10_brier: float | None = Field(default=None, ge=0)
