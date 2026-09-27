"""GAP 1/6/7: canonical schema is authoritative; coverage report; OOD warning."""

from __future__ import annotations

import pytest

from aureon.models.learning_v1 import CoverageStatus
from aureon.services.model_backtest import V1WalkForwardBacktester
from aureon.services.training_coverage import assess_coverage, coverage_report, render_coverage
from aureon.services.v1_model_training import (
    SchemaContractError,
    V1ModelTrainer,
    evaluate_v1_artifact,
    fit_v1_bundle,
    refit_v1_artifact,
)
from aureon.storage.postgres.repositories.models import ModelRepository
from aureon.storage.postgres.repositories.training import TrainingMemoryRepository
from tests.unit.v1_fixtures import constant_model, examples, snapshot


def _legacy(example):
    """A row that LOOKS canonical but carries the legacy +6 schema ids."""
    return example.model_copy(
        update={
            "feature_schema": "EOD_SETUP_FEATURES_V1",
            "label_schema": "FAVOURABLE_MOVE_LADDER_V2",
        }
    )


def test_legacy_schema_rows_are_rejected_not_silently_converted() -> None:
    rows = examples(40)
    tainted = rows[:-1] + [_legacy(rows[-1])]
    with pytest.raises(SchemaContractError, match="EOD_SETUP_FEATURES_V1"):
        fit_v1_bundle(tainted, algorithm="logistic_regression_v1")
    with pytest.raises(SchemaContractError):
        refit_v1_artifact(tainted, algorithm="logistic_regression_v1")
    with pytest.raises(SchemaContractError):
        evaluate_v1_artifact(constant_model("m"), tainted)


def test_the_trainer_and_walk_forward_fail_clearly_on_mixed_schemas(local_store) -> None:
    memory = TrainingMemoryRepository(local_store)
    models = ModelRepository(local_store)
    for example in examples(40, days=40, per_day=1):
        memory.write_canonical(example)
    memory.write_canonical(_legacy(examples(41, days=41, per_day=1)[-1]))
    trainer = V1ModelTrainer(training_memory=memory, models=models)
    with pytest.raises(SchemaContractError):
        trainer.train_candidates("XAUUSD", min_samples=30)
    with pytest.raises(SchemaContractError):
        V1WalkForwardBacktester(training_memory=memory, models=models).run("XAUUSD")


def test_training_carries_generation_and_a_coverage_report(local_store) -> None:
    memory = TrainingMemoryRepository(local_store)
    models = ModelRepository(local_store)
    for example in examples(60, days=30, per_day=2):
        memory.write_canonical(example)
    logistic, boosted = V1ModelTrainer(training_memory=memory, models=models).train_candidates(
        "XAUUSD", min_samples=30
    )
    assert logistic.generation == 0 and boosted.generation == 0
    coverage = logistic.validation_metrics["training_coverage"]
    assert coverage["schema"] == "AUREON_TRAINING_COVERAGE_V1"
    assert coverage["total_samples"] == 60
    # Retraining on identical data must not rewind a governed model to candidate.
    models.set_status(logistic.model_id, "rejected")
    again, _ = V1ModelTrainer(training_memory=memory, models=models).train_candidates(
        "XAUUSD", min_samples=30
    )
    assert again.model_id == logistic.model_id
    assert models.get_model(logistic.model_id).status == "rejected"


def test_coverage_report_buckets_show_sample_counts_and_flag_small_samples() -> None:
    rows = examples(50, session="london") + examples(3, session="new_york", clean_every=1)
    report = coverage_report(rows, min_samples=30)
    by_key = {(b["dimension"], b["value"]): b for b in report["buckets"]}
    london = by_key[("session", "london")]
    assert london["sample_count"] == 50 and london["small_sample"] is False
    assert london["clean_10_count"] == 25 and london["clean_10_rate"] == 0.5
    assert london["average_mae"] is not None and london["reach_5_rate"] == 1.0
    ny = by_key[("session", "new_york")]
    assert ny["sample_count"] == 3 and ny["small_sample"] is True and ny["clean_10_rate"] == 1.0
    assert ("daily_bias", "bullish") in by_key and ("htf_alignment", "aligned") in by_key
    text = render_coverage(report)
    assert "new_york" in text and "*" in text and "sample_count below" in text


def test_coverage_assessment_distinguishes_normal_low_and_out_of_distribution() -> None:
    rows = examples(120, session="london") + examples(20, session="asia")
    report = coverage_report(rows, low_threshold=50, ood_threshold=10)
    normal = assess_coverage(report, snapshot(1, session="london"))
    assert normal.status is CoverageStatus.NORMAL_COVERAGE and normal.similar_samples == 120
    low = assess_coverage(report, snapshot(2, session="asia"))
    assert low.status is CoverageStatus.LOW_TRAINING_COVERAGE and low.similar_samples == 20
    ood = assess_coverage(report, snapshot(3, session="new_york"))
    assert ood.status is CoverageStatus.OUT_OF_DISTRIBUTION
    assert ood.unseen_values == {"session": "new_york"}
    assert assess_coverage(None, snapshot(4)).status is CoverageStatus.UNKNOWN
