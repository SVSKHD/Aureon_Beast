"""Phase-1 acceptance gates for the historical V1 foundation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aureon.models.learning_v1 import ModelLifecycleStatus
from aureon.models.ml import BacktestFold, ModelBacktest
from aureon.services.phase1_acceptance import (
    bootstrap_champion_from_historical_shadow,
    build_phase1_acceptance,
)
from tests.unit.v1_fixtures import constant_model, examples


def _complete_backtest(model_id: str) -> ModelBacktest:
    metric = constant_model("tmp", clean=0.7).target_metrics["clean_10"]
    fold = BacktestFold(
        fold=1,
        train_from="2023-01-01",
        train_through="2025-12-31",
        test_from="2026-01-01",
        test_through="2026-01-31",
        train_samples=100,
        test_samples=30,
        target_metrics={"clean_10": metric},
    )
    return ModelBacktest(
        backtest_id=f"bt-{model_id}",
        model_id=model_id,
        symbol="XAUUSD",
        status="complete",
        algorithm="logistic_regression_v1",
        feature_schema_version="AUREON_FEATURES_V1",
        label_schema_version="AUREON_CLEAN_MOVE_V1",
        started_at=datetime(2026, 2, 1, tzinfo=UTC),
        completed_at=datetime(2026, 2, 1, tzinfo=UTC),
        start_market_date="2023-01-01",
        end_market_date="2026-01-31",
        folds=(fold,),
        aggregate_metrics={"clean_10": metric},
        out_of_sample_predictions=30,
    )


def test_phase1_acceptance_fails_without_champion(storage) -> None:
    rows = examples(40)
    for index, row in enumerate(rows):
        row = row.model_copy(
            update={"market_date": "2023-01-01" if index == 0 else "2026-01-31"}
        )
        storage.training_memory.write_canonical(row)
    model = constant_model("shadow", clean=0.7).model_copy(
        update={
            "status": ModelLifecycleStatus.SHADOW.value,
            "trained_from": "2023-01-01",
            "trained_through": "2026-01-31",
            "validation_metrics": {
                "training_coverage": {
                    "schema": "AUREON_TRAINING_COVERAGE_V1",
                    "total_samples": 40,
                    "training_from": "2023-01-01",
                    "training_through": "2026-01-31",
                    "dimensions": ["direction"],
                    "buckets": [{"dimension": "direction", "value": "buy"}],
                    "context_counts": {"x": 40},
                }
            },
        }
    )
    storage.models.write_model(model)
    storage.models.write_backtest(_complete_backtest(model.model_id))

    result = build_phase1_acceptance(storage=storage, symbol="XAUUSD")
    assert not result.passed
    assert result.checks["champion_frozen"]["passed"] is False
    assert result.checks["walk_forward"]["passed"] is True


def test_bootstrap_requires_full_historical_evidence(storage) -> None:
    model = constant_model("shadow", clean=0.7).model_copy(
        update={
            "status": ModelLifecycleStatus.SHADOW.value,
            "trained_from": "2024-01-01",
            "trained_through": "2026-01-31",
            "validation_metrics": {
                "training_coverage": {
                    "schema": "AUREON_TRAINING_COVERAGE_V1",
                    "total_samples": 40,
                    "training_from": "2024-01-01",
                    "training_through": "2026-01-31",
                    "buckets": [{}],
                    "context_counts": {"x": 40},
                }
            },
        }
    )
    storage.models.write_model(model)
    storage.models.write_backtest(_complete_backtest(model.model_id))

    with pytest.raises(ValueError, match="does not cover required foundation"):
        bootstrap_champion_from_historical_shadow(
            storage=storage, model_id=model.model_id
        )


def test_bootstrap_promotes_only_first_valid_shadow(storage) -> None:
    model = constant_model("shadow", clean=0.7).model_copy(
        update={
            "status": ModelLifecycleStatus.SHADOW.value,
            "trained_from": "2023-01-01",
            "trained_through": "2026-01-31",
            "validation_metrics": {
                "training_coverage": {
                    "schema": "AUREON_TRAINING_COVERAGE_V1",
                    "total_samples": 100,
                    "training_from": "2023-01-01",
                    "training_through": "2026-01-31",
                    "buckets": [{"dimension": "direction", "value": "buy"}],
                    "context_counts": {"x": 100},
                }
            },
        }
    )
    storage.models.write_model(model)
    storage.models.write_backtest(_complete_backtest(model.model_id))

    promoted = bootstrap_champion_from_historical_shadow(
        storage=storage, model_id=model.model_id
    )
    assert promoted.status == ModelLifecycleStatus.CHAMPION.value

    second = model.model_copy(update={"model_id": "shadow-2"})
    storage.models.write_model(second)
    storage.models.write_backtest(_complete_backtest(second.model_id))
    with pytest.raises(ValueError, match="no Champion"):
        bootstrap_champion_from_historical_shadow(
            storage=storage, model_id=second.model_id
        )
