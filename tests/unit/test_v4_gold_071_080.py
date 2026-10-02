"""Acceptance coverage for Aureon V4 Gold TODO 071-080."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aureon.models.ema_journey_v3 import EMAAnchorFeaturesV3
from aureon.models.ema_journey_v4 import V4MovementFeatures, V4RemainingMovementExample, V4RemainingMovementLabels
from aureon.models.enums import Direction, Timeframe
from aureon.services.v4_learning_loop import (
    V4OutcomeMemory,
    V4WeeklyTrainingPolicy,
    assess_prediction_reliability,
    historical_similarity_confidence,
    prediction_uncertainty,
    train_weekly_candidate,
    weekly_learning_report,
    weekly_training_due,
)


def _row(index: int, *, hit: bool = True, htf: str = "aligned") -> V4RemainingMovementExample:
    return V4RemainingMovementExample(
        journey_id=f"j-{index}",
        detection_id=f"d-{index}",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction=Direction.BUY,
        market_date=f"2026-09-{(index % 28) + 1:02d}",
        features=V4MovementFeatures(
            anchor_type="ema20_50_cross",
            base=EMAAnchorFeaturesV3(
                session="london",
                session_phase="EARLY",
                htf_alignment=htf,
                trend_direction="UP",
            ),
            movement_consumed=float(index % 5),
            pre_cross_movement=2.0,
            pre_cross_bars=3,
            pre_cross_seconds=900.0,
            cross_order="ema20_50_cross",
        ),
        labels=V4RemainingMovementLabels(
            reached_3=hit,
            reached_5=hit,
            reached_10=hit and index % 2 == 0,
            remaining_mfe=8.0 if hit else 1.0,
            remaining_mae=1.0 if hit else 6.0,
        ),
    )


def test_071_uncertainty_is_higher_for_ambiguous_probabilities() -> None:
    ambiguous = prediction_uncertainty({"reached_3": 0.5, "reached_5": 0.5}, similarity_confidence=0.8)
    decisive = prediction_uncertainty({"reached_3": 0.95, "reached_5": 0.05}, similarity_confidence=0.8)
    assert ambiguous > decisive


def test_072_similarity_confidence_requires_support_and_closeness() -> None:
    query = _row(100)
    history = [_row(i) for i in range(10)]
    reliability = assess_prediction_reliability(
        query,
        history,
        {"reached_3": 0.8, "reached_5": 0.7, "reached_10": 0.4},
        min_similarity_confidence=0.0,
        max_uncertainty=1.0,
    )
    assert 0.0 <= reliability.similarity_confidence <= 1.0
    assert reliability.similar_samples == 10


def test_073_ood_blocks_confidence_when_history_is_missing() -> None:
    query = _row(100)
    reliability = assess_prediction_reliability(
        query,
        [],
        {"reached_3": 0.9, "reached_5": 0.8},
    )
    assert reliability.out_of_distribution
    assert reliability.confidence == 0.0
    assert "insufficient_similar_history" in reliability.reasons


def test_074_077_outcome_memory_is_append_only_and_keeps_failures() -> None:
    memory = V4OutcomeMemory()
    good = _row(1, hit=True)
    failed = _row(2, hit=False)
    assert memory.save(good)
    assert memory.save(failed)
    assert not memory.save(failed)
    assert len(memory.all()) == 2
    assert memory.failure_count() == 1


def test_075_078_weekly_and_emergency_retraining_are_explicit() -> None:
    now = datetime(2026, 10, 2, tzinfo=UTC)
    weekly = weekly_training_due(
        last_trained_at=now - timedelta(days=8),
        now=now,
        new_examples=2,
        drift_detected=False,
    )
    assert weekly.due and not weekly.emergency and "weekly_cadence" in weekly.reasons

    emergency = weekly_training_due(
        last_trained_at=now - timedelta(days=1),
        now=now,
        new_examples=1,
        drift_detected=True,
        policy=V4WeeklyTrainingPolicy(),
    )
    assert emergency.due and emergency.emergency and "emergency_drift" in emergency.reasons


def test_076_077_079_candidate_is_versioned_keeps_failures_and_never_auto_promotes() -> None:
    rows = [_row(i, hit=i % 4 != 0) for i in range(30)]
    candidate = train_weekly_candidate(
        rows,
        created_at=datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
        min_samples=20,
    )
    assert candidate.version.startswith("v4-weekly-")
    assert candidate.status == "candidate"
    assert candidate.auto_promote is False
    assert candidate.failures == sum(not row.labels.reached_3 for row in rows)


def test_080_weekly_report_is_descriptive_and_requires_governance() -> None:
    rows = [_row(i, hit=i % 4 != 0) for i in range(30)]
    candidate = train_weekly_candidate(
        rows,
        created_at=datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
        min_samples=20,
    )
    report = weekly_learning_report(candidate, rows)
    assert report["samples"] == 30
    assert report["failed_outcomes_retained"] > 0
    assert report["promotion"] == "requires_explicit_governance"
    assert report["auto_promote"] is False
