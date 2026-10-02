"""Acceptance coverage for V4 Gold TODO 081-084."""
from __future__ import annotations
from datetime import UTC, datetime
import pytest
from aureon.models.ema_journey_v3 import EMAAnchorFeaturesV3
from aureon.models.ema_journey_v4 import V4MovementFeatures, V4RemainingMovementExample, V4RemainingMovementLabels
from aureon.models.enums import Direction, Timeframe
from aureon.services.v4_governance import V4Governance, V4GovernancePolicy, V4GovernedModel, monitor_champion_drift
from aureon.services.v4_learning_loop import train_weekly_candidate

def _row(i: int, *, date_prefix: str = "2026-09", hit: bool | None = None) -> V4RemainingMovementExample:
    hit = (i % 4 != 0) if hit is None else hit
    return V4RemainingMovementExample(
        journey_id=f"j-{date_prefix}-{i}", detection_id=f"d-{date_prefix}-{i}", symbol="XAUUSD", timeframe=Timeframe.M5, direction=Direction.BUY,
        market_date=f"{date_prefix}-{(i % 28) + 1:02d}",
        features=V4MovementFeatures(anchor_type="ema20_50_cross", base=EMAAnchorFeaturesV3(session="london", session_phase="EARLY", htf_alignment="aligned", trend_direction="UP"), movement_consumed=float(i % 5), pre_cross_movement=2.0, pre_cross_bars=3, pre_cross_seconds=900.0, cross_order="ema20_50_cross"),
        labels=V4RemainingMovementLabels(reached_3=hit, reached_5=hit, reached_10=hit and i % 2 == 0, remaining_mfe=8.0 if hit else 1.0, remaining_mae=1.0 if hit else 6.0),
    )

def _candidate():
    rows = [_row(i) for i in range(40)]
    return train_weekly_candidate(rows, created_at=datetime(2026, 10, 1, tzinfo=UTC), min_samples=20)

def test_081_lifecycle_cannot_skip_states() -> None:
    governance = V4Governance()
    model = V4GovernedModel(_candidate())
    with pytest.raises(ValueError, match="expected challenger"):
        governance.admit_shadow(model, [_row(i, date_prefix="2026-10") for i in range(10)])
    with pytest.raises(ValueError, match="expected shadow"):
        governance.evaluate_shadow(model, [_row(i, date_prefix="2026-10") for i in range(20)])

def test_082_candidate_must_beat_base_rate() -> None:
    policy = V4GovernancePolicy(min_validation_samples=20, min_base_rate_brier_improvement=1.0)
    result = V4Governance(policy).qualify_candidate(V4GovernedModel(_candidate()), [_row(i) for i in range(30)])
    assert result.status == "rejected"
    assert result.validation_metrics["brier_improvement"] < 1.0

def test_083_weekly_unseen_data_cannot_overlap_training() -> None:
    governance = V4Governance(V4GovernancePolicy(min_base_rate_brier_improvement=-1.0))
    challenger = governance.qualify_candidate(V4GovernedModel(_candidate()), [_row(i) for i in range(30)])
    assert challenger.status == "challenger"
    with pytest.raises(ValueError, match="overlaps"):
        governance.admit_shadow(challenger, [_row(i) for i in range(10)])

def test_083_unseen_week_is_recorded_before_shadow() -> None:
    policy = V4GovernancePolicy(min_base_rate_brier_improvement=-1.0, max_unseen_brier=1.0)
    governance = V4Governance(policy)
    challenger = governance.qualify_candidate(V4GovernedModel(_candidate()), [_row(i) for i in range(30)])
    shadow = governance.admit_shadow(challenger, [_row(i, date_prefix="2026-10") for i in range(12)])
    assert shadow.status == "shadow"
    assert shadow.unseen_metrics["samples"] == 12

def test_084_drift_never_auto_promotes_replacement() -> None:
    candidate = _candidate()
    champion = V4GovernedModel(candidate, status="champion", shadow_metrics={"brier": 0.05})
    recent = [_row(i, date_prefix="2026-10", hit=False) for i in range(25)]
    result = monitor_champion_drift(champion, recent, V4GovernancePolicy(drift_max_brier=0.0))
    assert result["drifted"]
    assert result["action"] == "trigger_emergency_retraining_and_review"
    assert result["auto_promote_replacement"] is False
