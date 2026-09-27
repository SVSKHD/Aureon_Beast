"""GAP 10/11/12: walk-forward leakage guards, the unseen-month workflow, no duplicate promotion."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aureon.models.learning_v1 import LearningExamStatus
from aureon.services.evolution_agent import EvolutionAgent
from aureon.services.foundation_pipeline import (
    LeakageError,
    UnseenMonthWorkflow,
    assert_no_exam_leakage,
)
from aureon.services.model_backtest import V1WalkForwardBacktester
from aureon.services.v1_model_training import V1ModelTrainer
from aureon.storage.postgres.repositories.models import ModelRepository
from aureon.storage.postgres.repositories.training import TrainingMemoryRepository
from tests.unit.v1_fixtures import constant_model, examples


def _seed(local_store, count=60, days=30):
    memory = TrainingMemoryRepository(local_store)
    models = ModelRepository(local_store)
    for example in examples(count, days=days, per_day=count // days):
        memory.write_canonical(example)
    return memory, models


def test_walk_forward_trains_strictly_before_it_tests_and_excludes_unresolved(local_store) -> None:
    memory, models = _seed(local_store, count=90, days=45)
    # One example whose outcome has not resolved yet must never be trained on.
    unresolved = examples(1)[0].model_copy(
        update={
            "outcome": examples(1)[0].outcome.model_copy(update={"resolved_at": None}),
            "setup_id": "unresolved",
            "market_date": "2026-01-06",
        }
    )
    memory.write_canonical(unresolved)
    result = V1WalkForwardBacktester(training_memory=memory, models=models).run(
        "XAUUSD", min_train_days=20, test_days=5, min_train_samples=20
    )
    assert result.status == "complete"
    assert result.folds
    for fold in result.folds:
        assert fold.train_through < fold.test_from
    first_train = [f for f in result.folds if f.test_from > "2026-01-06"][0]
    same_window_examples = memory.canonical_between("XAUUSD", "0001-01-01", first_train.test_from)
    assert first_train.train_samples == len(same_window_examples) - 1  # the unresolved row is out
    assert result.aggregate_metrics["clean_10"].samples == result.out_of_sample_predictions


def test_february_is_unseen_until_scored_and_released(local_store) -> None:
    memory, models = _seed(local_store, count=50, days=25)  # Jan 5 .. Jan 29
    models.write_model(constant_model("frozen", clean=0.8, trained_through="2026-01-31"))
    for example in examples(20, days=10, per_day=2):
        february = example.model_copy(
            update={
                "market_date": "2026-02-" + f"{10 + int(example.market_date[-2:]) % 15:02d}",
                "setup_id": "feb-" + example.setup_id,
            }
        )
        memory.write_canonical(february)
    workflow = UnseenMonthWorkflow(models=models, training_memory=memory)
    exam = workflow.open_exam("XAUUSD", period_from="2026-02-01", period_to="2026-02-28")
    assert exam.status is LearningExamStatus.OPEN and exam.frozen_model_id == "frozen"

    # Training refuses the open period; training on January alone is still fine.
    trainer = V1ModelTrainer(training_memory=memory, models=models)
    with pytest.raises(LeakageError):
        trainer.train_candidates("XAUUSD", min_samples=30)
    logistic, _ = trainer.train_candidates("XAUUSD", end_market_date="2026-02-01", min_samples=30)
    assert logistic.trained_through < "2026-02-01"
    assert logistic.generation == 1 and logistic.parent_model_id == "frozen"

    with pytest.raises(ValueError):
        workflow.release_exam(exam.exam_id)  # not scored yet

    scored = workflow.score_exam(exam.exam_id)
    assert scored.status is LearningExamStatus.SCORED
    assert scored.metrics["samples"] == 20
    experience = scored.metrics["experience"]
    assert experience["false_positive"] > 0 and experience["true_positive"] > 0
    prediction = models.prediction_for("frozen", "feb-setup-0")
    assert prediction.decision == "ENTER" and prediction.outcome_class in {
        "true_positive",
        "false_positive",
    }
    assert prediction.predicted_at < prediction.reconciled_at

    released = workflow.release_exam(exam.exam_id)
    assert released.status is LearningExamStatus.RELEASED
    challenger, _ = trainer.train_candidates("XAUUSD", min_samples=30)
    assert challenger.trained_through >= "2026-02-10"

    # March: the frozen model must predate the next exam entirely.
    with pytest.raises(LeakageError):
        workflow.open_exam(
            "XAUUSD", period_from="2026-01-20", period_to="2026-03-31", model_id="frozen"
        )
    march = workflow.open_exam(
        "XAUUSD", period_from="2026-03-01", period_to="2026-03-31", model_id="frozen"
    )
    assert march.status is LearningExamStatus.OPEN


def test_leakage_guard_is_a_no_op_for_a_repository_without_exams() -> None:
    class Plain:
        pass

    assert assert_no_exam_leakage(Plain(), "XAUUSD", examples(5)) is None


def test_a_promotion_cannot_be_repeated_after_a_restart(local_store) -> None:
    models = ModelRepository(local_store)
    models.write_model(constant_model("shadow-1", status="shadow"))
    promoted = models.promote_champion(
        "shadow-1", at=datetime(2026, 3, 1, tzinfo=UTC), reason="test"
    )
    assert promoted.status == "champion"
    with pytest.raises(ValueError):
        models.promote_champion("shadow-1", at=datetime(2026, 3, 1, tzinfo=UTC), reason="again")
    with pytest.raises(ValueError):
        EvolutionAgent(models).evaluate_shadow("shadow-1")
    assert [m.model_id for m in models.models_for_symbol("XAUUSD") if m.status == "champion"] == [
        "shadow-1"
    ]


def test_pending_learning_setup_is_never_re_frozen_after_resolution(local_store) -> None:
    from types import SimpleNamespace

    from aureon.models.enums import DirectionContext, Timeframe
    from aureon.services.learning_memory import LearningMemoryService

    memory = TrainingMemoryRepository(local_store)
    service = LearningMemoryService(memory, horizon_bars=2)
    at = datetime(2026, 2, 2, 12, 0, tzinfo=UTC)
    setup = SimpleNamespace(
        setup_id="s-freeze",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        market_date="2026-02-02",
        direction_context=DirectionContext.BULLISH,
        anchor=SimpleNamespace(price=2400.0),
        context_summary=SimpleNamespace(volatility_regime=None, session=None, mtf_alignment=None),
    )
    event = SimpleNamespace(event_id="e1", market_time=SimpleNamespace(utc=at), context_snapshot={})
    assert service.freeze_setup(setup, event) is not None
    assert service.freeze_setup(setup, event).bars_seen == 0
    from datetime import timedelta

    for index in range(1, 3):
        candle = SimpleNamespace(
            symbol="XAUUSD",
            timeframe=Timeframe.M5,
            high=2415.0,
            low=2398.0,
            close=2410.0,
            close_time=at + timedelta(minutes=5 * index),
            open_time=SimpleNamespace(utc=at + timedelta(minutes=5 * (index - 1))),
        )
        service.on_closed_candle(candle)
    assert memory.canonical_for_setup("s-freeze") is not None
    assert memory.pending_count("XAUUSD") == 0
    assert service.freeze_setup(setup, event) is None  # a restart replaying CONFIRMED adds nothing
    assert memory.pending_count("XAUUSD") == 0
    assert len(memory.canonical_between("XAUUSD", "0001-01-01", "9999-12-31")) == 1
