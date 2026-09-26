"""Champion/challenger lifecycle tests for Aureon V1."""

from datetime import UTC, datetime, timedelta

import pytest

from aureon.models.learning_v1 import (
    FEATURE_SCHEMA_V1,
    LABEL_SCHEMA_V1,
    MODEL_SCHEMA_V1,
    CanonicalTrainingExample,
    CleanMoveOutcomeV1,
    FeatureSnapshotV1,
    ModelLifecycleStatus,
)
from aureon.models.ml import ModelBacktest, ModelPrediction, ModelRegistryEntry, TargetMetrics
from aureon.models.enums import Direction, Timeframe
from aureon.services.evolution_agent import EvolutionAgent, EvolutionPolicy
from aureon.storage.local_database import LocalDatabase
from aureon.storage.postgres.repositories.models import ModelRepository
from aureon.storage.postgres.repositories.training import TrainingMemoryRepository


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


def _canonical(index: int) -> CanonicalTrainingExample:
    at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    clean = index % 2 == 0
    features = FeatureSnapshotV1(
        setup_id=f"canonical-{index}",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        timestamp=at,
        frozen_at=at,
        direction=Direction.BUY if index % 3 else Direction.SELL,
        reference_price=4500.0 + index,
        ema_gap=float((index % 5) - 2),
        rsi=45.0 + (index % 20),
        atr=8.0 + (index % 4),
        market_regime="trend" if clean else "range",
        supporting_agents=6 if clean else 2,
        opposing_agents=1 if clean else 5,
    )
    outcome = CleanMoveOutcomeV1(
        resolved_at=at + timedelta(hours=4),
        horizon_bars=48,
        clean_10=clean,
        reached_5=True,
        reached_10=clean,
        reached_20=clean and index % 4 == 0,
        reached_30=False,
        reached_40=False,
        bars_to_5=3,
        bars_to_10=8 if clean else None,
        max_favourable_move=22.0 if clean else 7.0,
        max_adverse_move=3.0 if clean else 12.0,
        mae_before_10=3.0 if clean else None,
    )
    return CanonicalTrainingExample(
        example_id=f"example-{index}",
        market_date=at.date().isoformat(),
        setup_id=features.setup_id,
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        features=features,
        outcome=outcome,
        generated_at=at + timedelta(hours=4),
    )


def test_evolution_cycle_trains_backtests_and_enters_shadow_without_promotion(tmp_path) -> None:
    db = LocalDatabase(tmp_path / "aureon.db")
    db.ensure_schema()
    models = ModelRepository(db)
    memory = TrainingMemoryRepository(db)
    for index in range(35):
        memory.write_canonical(_canonical(index))

    evolution = EvolutionAgent(
        models,
        policy=EvolutionPolicy(
            min_validation_samples=5,
            min_clean_precision=0.0,
            max_clean_false_positive_rate=1.0,
            min_shadow_samples=5,
            min_shadow_clean_precision=0.0,
            max_shadow_false_positive_rate=1.0,
        ),
        now=lambda: datetime(2026, 3, 1, tzinfo=UTC),
    )
    report = evolution.run_cycle(
        "XAUUSD",
        training_memory=memory,
        min_new_examples=20,
        min_samples=20,
        min_train_days=20,
        test_days=5,
    )

    assert len(report["trained"]) == 2
    assert report["shadow"] is not None
    assert models.active_shadow("XAUUSD") is not None
    # A fresh candidate may enter shadow, but the same cycle cannot promote it.
    assert models.champion("XAUUSD") is None
