"""Champion/challenger lifecycle tests for Aureon V1."""

from datetime import UTC, datetime, timedelta

import pytest

from aureon.models.learning_v1 import (
    FEATURE_SCHEMA_V1,
    LABEL_SCHEMA_V1,
    MODEL_SCHEMA_V1,
    ModelLifecycleStatus,
)
from aureon.models.ml import ModelBacktest, ModelPrediction, ModelRegistryEntry, TargetMetrics
from aureon.models.enums import Timeframe
from aureon.services.evolution_agent import EvolutionAgent
from aureon.storage.local_database import LocalDatabase
from aureon.storage.postgres.repositories.models import ModelRepository


def _metrics() -> TargetMetrics:
    return TargetMetrics(
        samples=30,
        positives=15,
        negatives=15,
        accuracy=0.8,
        precision=0.8,
        recall=0.8,
        brier=0.16,
        log_loss=0.4,
        roc_auc=0.8,
        false_positive_rate=0.2,
        average_mae=3.0,
        average_mfe=16.0,
    )


def _model(model_id: str, at: datetime) -> ModelRegistryEntry:
    return ModelRegistryEntry(
        model_id=model_id,
        symbol="XAUUSD",
        algorithm="logistic_regression_v1",
        status=ModelLifecycleStatus.CANDIDATE.value,
        feature_schema_version=FEATURE_SCHEMA_V1,
        label_schema_version=LABEL_SCHEMA_V1,
        model_schema_version=MODEL_SCHEMA_V1,
        trained_from="2026-01-01",
        trained_through="2026-02-28",
        training_samples=100,
        target_metrics={"clean_10": _metrics()},
        artifact={},
        created_at=at,
    )


def test_candidate_cannot_overwrite_champion_without_shadow(tmp_path) -> None:
    db = LocalDatabase(tmp_path / "aureon.db")
    db.ensure_schema()
    repo = ModelRepository(db)
    now = datetime(2026, 3, 1, tzinfo=UTC)
    repo.write_model(_model("m1", now))

    with pytest.raises(ValueError, match="only shadow may become champion"):
        repo.promote_champion("m1", at=now, reason="should fail")

    assert repo.champion("XAUUSD") is None


def test_evolution_requires_validation_then_shadow_evidence(tmp_path) -> None:
    db = LocalDatabase(tmp_path / "aureon.db")
    db.ensure_schema()
    repo = ModelRepository(db)
    now = datetime(2026, 3, 1, tzinfo=UTC)
    repo.write_model(_model("m1", now))
    evolution = EvolutionAgent(repo, now=lambda: now)

    challenger = evolution.qualify_candidate("m1")
    assert challenger.status == ModelLifecycleStatus.CHALLENGER.value

    repo.write_backtest(
        ModelBacktest(
            backtest_id="bt1",
            model_id="m1",
            symbol="XAUUSD",
            status="complete",
            algorithm="logistic_regression_v1",
            feature_schema_version=FEATURE_SCHEMA_V1,
            label_schema_version=LABEL_SCHEMA_V1,
            started_at=now,
            completed_at=now,
            start_market_date="2026-01-01",
            end_market_date="2026-02-28",
            aggregate_metrics={"clean_10": _metrics()},
            out_of_sample_predictions=30,
        )
    )
    shadow = evolution.admit_shadow("m1")
    assert shadow.status == ModelLifecycleStatus.SHADOW.value
    assert repo.champion("XAUUSD") is None

    # Twenty persistent reconciled shadow predictions: 10 clean wins at high P,
    # 10 failures at low P. Shadow has zero execution authority; only predictions are stored.
    for index in range(20):
        actual = index < 10
        repo.write_prediction(
            ModelPrediction(
                prediction_id=f"p{index}",
                model_id="m1",
                setup_id=f"s{index}",
                event_id=f"e{index}",
                symbol="XAUUSD",
                timeframe=Timeframe.M5,
                predicted_at=now + timedelta(minutes=index),
                feature_schema_version=FEATURE_SCHEMA_V1,
                label_schema_version=LABEL_SCHEMA_V1,
                probabilities={"clean_10": 0.8 if actual else 0.2},
                feature_snapshot={},
                actual_outcomes={"clean_10": actual},
                reconciled_at=now + timedelta(hours=1),
            )
        )

    promoted = evolution.evaluate_shadow("m1")
    assert promoted.status == ModelLifecycleStatus.CHAMPION.value
    assert repo.champion("XAUUSD").model_id == "m1"

    # New training creates another candidate but cannot silently replace the Champion.
    repo.write_model(_model("m2", now + timedelta(days=1)))
    assert repo.champion("XAUUSD").model_id == "m1"
    with pytest.raises(ValueError, match="only shadow may become champion"):
        repo.promote_champion("m2", at=now, reason="skip governance")
