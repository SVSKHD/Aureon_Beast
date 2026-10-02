"""Aureon V4 Gold final acceptance checks (TODO 098-100)."""
from __future__ import annotations
from datetime import UTC, datetime
from pathlib import Path
import pytest
from aureon.discord.v4_journey_cards import journey_thread_key
from aureon.models.ema_journey_v3 import EMAAnchorFeaturesV3
from aureon.models.ema_journey_v4 import V4MovementFeatures, V4RemainingMovementExample, V4RemainingMovementLabels
from aureon.models.enums import Direction, Timeframe
from aureon.services.v4_governance import V4GovernancePolicy
from aureon.services.v4_learning_loop import V4WeeklyTrainingPolicy
from aureon.services.v4_ema_model import fit_remaining_move_model
from aureon.services.v4_parity import assert_v4_live_replay_parity

ROOT=Path(__file__).resolve().parents[2]

def _features(consumed=2.0):
    return V4MovementFeatures(anchor_type="ema20_50_cross",base=EMAAnchorFeaturesV3(session="london",session_phase="EARLY",htf_alignment="aligned",trend_direction="UP"),movement_consumed=consumed,pre_cross_movement=1,pre_cross_bars=2,pre_cross_seconds=600,cross_order="ema20_50_cross")

def _row(i:int):
    hit=i%3!=0
    return V4RemainingMovementExample(journey_id=f"j-{i}",detection_id=f"d-{i}",symbol="XAUUSD",timeframe=Timeframe.M5,direction=Direction.BUY,market_date=f"2026-09-{(i%28)+1:02d}",features=_features(float(i%5)),labels=V4RemainingMovementLabels(reached_3=hit,reached_5=hit,reached_10=hit and i%2==0,remaining_mfe=12 if hit else 2,remaining_mae=2 if hit else 7))

def test_098_live_replay_parity_accepts_identical_frozen_state():
    model=fit_remaining_move_model([_row(i) for i in range(30)],min_samples=20)
    features=_features()
    hashes=assert_v4_live_replay_parity(model,features,features,live_context={"phase":"confirmed_cross","session":"london"},replay_context={"phase":"confirmed_cross","session":"london"})
    assert set(hashes)=={"feature_hash","context_hash","prediction_hash"}

def test_098_live_replay_parity_rejects_feature_or_context_drift():
    model=fit_remaining_move_model([_row(i) for i in range(30)],min_samples=20)
    with pytest.raises(AssertionError,match="feature parity"):
        assert_v4_live_replay_parity(model,_features(2),_features(9))
    with pytest.raises(AssertionError,match="context parity"):
        assert_v4_live_replay_parity(model,_features(),_features(),live_context={"phase":"cross"},replay_context={"phase":"pullback"})

def test_099_v4_definition_of_done_contract():
    weekly=V4WeeklyTrainingPolicy()
    governance=V4GovernancePolicy()
    assert weekly.cadence_days==7
    assert weekly.emergency_drift_retraining is True
    assert governance.min_base_rate_brier_improvement>0
    assert governance.min_unseen_samples>0
    assert governance.drift_min_samples>0
    assert journey_thread_key("journey-1")=="v4_journey:journey-1"

def test_100_v4_architecture_and_definition_are_documented():
    architecture=(ROOT/"docs"/"V4_ARCHITECTURE.md").read_text(encoding="utf-8")
    done=(ROOT/"docs"/"V4_DEFINITION_OF_DONE.md").read_text(encoding="utf-8")
    assert "PRE-CROSS PRESSURE" in architecture
    assert "PULLBACK" in architecture
    assert "Candidate -> Challenger -> Shadow -> Champion" in architecture
    assert "Discord" in architecture
    assert "Live/replay parity" in done
    assert "No automatic promotion" in done
    assert "research-only" in done
