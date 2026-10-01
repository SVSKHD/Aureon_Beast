"""Aureon V3 TODO 089-094 acceptance tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aureon.discord.service import _v3_model_confidence_field
from aureon.models.ema_journey_v3 import EMAAnchorFeaturesV3
from aureon.models.ml import ModelRegistryEntry, TargetMetrics
from aureon.services.v3_ema_ops import (
    DriftPolicy,
    RetrainingPolicy,
    V3ChampionDriftMonitor,
    retraining_due,
    rolling_unseen_summary,
)
from aureon.services.v3_reproducibility import (
    current_code_commit,
    dataset_snapshot_hash,
)
from aureon.services.v3_ema_learning import (
    AgentConfidenceSnapshot,
    CanonicalEMAExampleV3,
    EMAFailureType,
    EMAOutcomeV3,
)
from aureon.models.enums import Direction, Timeframe

BASE = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def _example(index: int) -> CanonicalEMAExampleV3:
    when = BASE + timedelta(days=index)
    return CanonicalEMAExampleV3(
        example_id=f"e-{index}",
        journey_id=f"j-{index}",
        detection_id=f"d-{index}",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction=Direction.SELL,
        market_date=when.date().isoformat(),
        anchor_type="ema20_50_cross",
        features=EMAAnchorFeaturesV3(
            session="london",
            trend_direction="DOWN",
            agent_confidence=AgentConfidenceSnapshot(
                label="HIGH",
                supportive=4,
                neutral=0,
                conflicting=0,
                coverage=4,
            ),
        ),
        movement_from_journey_start=0.0,
        independence_weight=1.0,
        outcome=EMAOutcomeV3(
            reached_3=True,
            reached_5=True,
            reached_10=index % 2 == 0,
            reached_20=False,
            reached_30=False,
            reached_40=False,
            clean_10=index % 2 == 0,
            mfe=12.0 if index % 2 == 0 else 4.0,
            mae=2.0 if index % 2 == 0 else 7.0,
            failure_type=(
                EMAFailureType.NONE
                if index % 2 == 0
                else EMAFailureType.NO_CONTINUATION
            ),
            resolved_at=when + timedelta(hours=1),
        ),
        generated_at=when + timedelta(hours=1),
    )


def test_dataset_snapshot_hash_is_order_independent_and_content_sensitive() -> None:
    rows = [_example(1), _example(2), _example(3)]
    same = list(reversed(rows))
    changed = [*rows[:-1], rows[-1].model_copy(update={"example_id": "changed"})]

    assert dataset_snapshot_hash(rows) == dataset_snapshot_hash(same)
    assert dataset_snapshot_hash(rows) != dataset_snapshot_hash(changed)


def test_code_commit_prefers_explicit_runtime_commit(monkeypatch) -> None:
    monkeypatch.setenv("AUREON_GIT_COMMIT", "abc123")
    assert current_code_commit() == "abc123"


def test_rolling_unseen_window_clamps_to_10_20_days() -> None:
    holdouts = []
    for index in range(25):
        holdouts.append(
            SimpleNamespace(
                status="scored",
                scored_at=BASE + timedelta(days=index),
                market_date=(BASE + timedelta(days=index)).date().isoformat(),
                metrics={
                    "samples": 2,
                    "false_positives_clean_10": 0,
                    "false_negatives_clean_10": 0,
                    "targets": {"clean_10": {"brier": 0.15}},
                },
            )
        )

    ten = rolling_unseen_summary(holdouts, window_days=3)
    twenty = rolling_unseen_summary(holdouts, window_days=99)

    assert ten["window_days"] == 10
    assert ten["days_available"] == 10
    assert twenty["window_days"] == 20
    assert twenty["days_available"] == 20


def test_retraining_due_on_weekly_cadence_new_examples_or_drift() -> None:
    policy = RetrainingPolicy(cadence_days=7, minimum_new_examples=30)

    scheduled = retraining_due(
        last_trained_at=BASE,
        now=BASE + timedelta(days=7),
        new_examples=0,
        drift_detected=False,
        policy=policy,
    )
    evidence = retraining_due(
        last_trained_at=BASE,
        now=BASE + timedelta(days=1),
        new_examples=30,
        drift_detected=False,
        policy=policy,
    )
    drift = retraining_due(
        last_trained_at=BASE,
        now=BASE + timedelta(days=1),
        new_examples=0,
        drift_detected=True,
        policy=policy,
    )

    assert scheduled.reasons == ("scheduled_cadence",)
    assert evidence.reasons == ("enough_new_examples",)
    assert drift.reasons == ("champion_drift",)


def test_drift_monitor_rolls_back_degraded_champion() -> None:
    model = SimpleNamespace(model_id="champion", status="champion")
    rolled_back = SimpleNamespace(model_id="previous-champion")

    rows = [
        {
            "payload": {"probability_clean_10": 0.9},
            "actual_outcome": {"clean_10": False},
        }
        for _ in range(40)
    ]

    class Models:
        def get_model(self, model_id):
            return model

        def rollback_champion_for_contract(self, model_id, **kwargs):
            return rolled_back

    class Learning:
        def predictions_for_model(self, model_id, reconciled_only=True):
            return rows

    monitor = V3ChampionDriftMonitor(
        models=Models(),
        learning=Learning(),
        policy=DriftPolicy(
            min_samples=30,
            max_brier=0.24,
            max_calibration_error=0.12,
            max_false_positive_rate=0.45,
        ),
        now=lambda: BASE,
    )
    result = monitor.evaluate("champion")

    assert result["drifted"] is True
    assert result["action"] == "rollback"
    assert result["rollback_model_id"] == "previous-champion"


def test_every_discord_probability_shows_its_sample_count() -> None:
    payload = {
        "sufficient_data": True,
        "sample_count": 250,
        "target_sample_counts": {
            "reach_3": 240,
            "reach_5": 238,
            "reach_10": 230,
            "reach_20": 220,
            "reach_30": 210,
            "reach_40": 205,
            "clean_10": 225,
        },
        "probability_reach_3": 0.90,
        "probability_reach_5": 0.82,
        "probability_reach_10": 0.70,
        "probability_reach_20": 0.50,
        "probability_reach_30": 0.35,
        "probability_reach_40": 0.20,
        "probability_clean_10": 0.64,
        "expected_mfe": 14.0,
        "expected_mae": 3.0,
    }

    _, text = _v3_model_confidence_field(payload)

    for n in (240, 238, 230, 220, 210, 205, 225):
        assert f"(n={n})" in text
