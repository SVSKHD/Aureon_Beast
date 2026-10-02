"""Acceptance coverage for V4 Gold TODO 085-092."""
from __future__ import annotations
from datetime import UTC, datetime
from aureon.models.ema_journey_v3 import EMAAnchorFeaturesV3
from aureon.models.ema_journey_v4 import V4MovementFeatures, V4PullbackClass, V4ReentryObservation, V4ReentryOutcome, V4RemainingMovementExample, V4RemainingMovementLabels
from aureon.models.enums import Direction, Timeframe
from aureon.services.v4_placement_management import compare_pre_cross_vs_cross, compare_cross_vs_pullback_reentry, missed_large_movements, opportunity_cost, fixed_target_research, partial_runner_research, pullback_continuation_management

def _row(i:int, *, anchor="ema20_50_cross", consumed=0.0, mfe=10.0, mae=2.0):
    return V4RemainingMovementExample(journey_id=f"j-{i}", detection_id=f"d-{i}-{anchor}", symbol="XAUUSD", timeframe=Timeframe.M5, direction=Direction.BUY, market_date="2026-09-01", features=V4MovementFeatures(anchor_type=anchor, base=EMAAnchorFeaturesV3(session="london", session_phase="EARLY", htf_alignment="aligned", trend_direction="UP"), movement_consumed=consumed), labels=V4RemainingMovementLabels(reached_3=mfe>=3,reached_5=mfe>=5,reached_10=mfe>=10,remaining_mfe=mfe,remaining_mae=mae))

def _reentry(i:int, *, mfe=10.0, mae=2.0, failed=False):
    return V4ReentryObservation(reentry_id=f"r-{i}",pullback_id=f"p-{i}",journey_id=f"j-{i}",symbol="XAUUSD",timeframe=Timeframe.M5,direction=Direction.BUY,observed_at=datetime(2026,9,1,tzinfo=UTC),price=4200,ema20=4201,ema50=4199,pullback_depth=2,pullback_fraction=.25,pullback_class=V4PullbackClass.NORMAL,structure_intact=True,ema_aligned=True,outcome=V4ReentryOutcome(mfe=mfe,mae=mae,reached_3=mfe>=3,reached_5=mfe>=5,reached_10=mfe>=10,failed=failed,completed=True))

def test_085_pre_cross_vs_cross_are_separate_placements():
    report=compare_pre_cross_vs_cross([_row(1,anchor="pre_cross",mfe=20)],[_row(1,consumed=5,mfe=15)])
    assert report["pre_cross"]["mean_remaining_mfe"]==20
    assert report["confirmed_cross"]["mean_remaining_mfe"]==15

def test_086_cross_vs_reentry_uses_resolved_reentries():
    report=compare_cross_vs_pullback_reentry([_row(1,mfe=12)],[_reentry(1,mfe=8)])
    assert report["confirmed_cross"]["samples"]==1
    assert report["pullback_reentry"]["samples"]==1

def test_087_tracks_late_large_move_opportunities():
    report=missed_large_movements([_row(1,consumed=12,mfe=25),_row(2,consumed=2,mfe=25)])
    assert report["missed_large_movements"]==1
    assert report["journey_ids"]==["j-1"]

def test_088_opportunity_cost_is_paired_by_journey():
    report=opportunity_cost([_row(1,mfe=20),_row(2,mfe=15)],[_row(1,mfe=12),_row(3,mfe=4)])
    assert report["paired_journeys"]==1
    assert report["mean_opportunity_cost"]==8

def test_089_090_fixed_three_and_five_research():
    rows=[_row(1,mfe=6),_row(2,mfe=4),_row(3,mfe=2)]
    assert fixed_target_research(rows,3)["hit_rate"]==2/3
    assert fixed_target_research(rows,5)["hit_rate"]==1/3

def test_091_partial_runner_never_claims_more_than_observed_cap():
    report=partial_runner_research([_row(1,mfe=30)],partial_target=5,partial_fraction=.5,runner_cap=20)
    assert report["mean_capture"]==12.5

def test_092_pullback_management_keeps_failed_reentries():
    report=pullback_continuation_management([_reentry(1,mfe=10),_reentry(2,mfe=2,failed=True)])
    assert report["samples"]==2
    assert report["failed_reentries"]==1
    assert report["success_rate"]==0.5
