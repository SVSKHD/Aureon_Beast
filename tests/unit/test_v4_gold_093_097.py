"""Acceptance coverage for V4 Gold Discord TODO 093-097."""
from __future__ import annotations
from datetime import UTC, datetime
from aureon.discord.v4_journey_cards import journey_thread_key, pullback_observation_card, reentry_observation_card, remaining_move_card, render_card_text, similar_journey_evidence_card
from aureon.models.ema_journey_v3 import EMAAnchorFeaturesV3
from aureon.models.ema_journey_v4 import V4MovementFeatures, V4PullbackClass, V4PullbackState, V4ReentryObservation, V4RemainingMovementExample, V4RemainingMovementLabels
from aureon.models.enums import Direction, Timeframe
from aureon.services.v4_specialist_similarity import retrieve_similar_journeys

def _features(consumed=2.0):
    return V4MovementFeatures(anchor_type="ema20_50_cross",base=EMAAnchorFeaturesV3(session="london",session_phase="EARLY",htf_alignment="aligned",trend_direction="UP"),movement_consumed=consumed)

def _row(i:int,mfe=10.0):
    return V4RemainingMovementExample(journey_id=f"j-{i}",detection_id=f"d-{i}",symbol="XAUUSD",timeframe=Timeframe.M5,direction=Direction.BUY,market_date="2026-09-01",features=_features(float(i)),labels=V4RemainingMovementLabels(reached_3=mfe>=3,reached_5=mfe>=5,reached_10=mfe>=10,remaining_mfe=mfe,remaining_mae=2))

def _pullback():
    return V4PullbackState(pullback_id="p1",journey_id="j1",symbol="XAUUSD",timeframe=Timeframe.M5,direction=Direction.BUY,started_at=datetime(2026,9,1,tzinfo=UTC),start_price=4200,expansion_origin_price=4195,expansion_extreme_price=4210,expansion_move=15,depth=3,retracement_fraction=.2,classification=V4PullbackClass.SHALLOW,structure_intact=True,ema_aligned=True)

def test_093_thread_key_is_stable_per_journey():
    assert journey_thread_key("abc")==journey_thread_key("abc")
    assert journey_thread_key("abc")!=journey_thread_key("xyz")

def test_094_remaining_move_is_single_glance():
    card=remaining_move_card("XAUUSD","buy",{"reached_3":.8,"reached_5":.7,"reached_10":.55},expected_mfe=12,expected_mae=3,uncertainty=.2,similarity_confidence=.75)
    text=render_card_text(card)
    assert "+3 80%" in text and "+10 55%" in text
    assert "uncertainty 20%" in text

def test_095_pullback_card_labels_observation():
    text=render_card_text(pullback_observation_card(_pullback()))
    assert "PULLBACK OBSERVATION" in text
    assert "observing the journey" in text

def test_096_reentry_is_never_an_execution_instruction():
    pb=_pullback()
    row=V4ReentryObservation(reentry_id="r1",pullback_id=pb.pullback_id,journey_id=pb.journey_id,symbol=pb.symbol,timeframe=pb.timeframe,direction=pb.direction,observed_at=datetime(2026,9,1,tzinfo=UTC),price=4205,ema20=4204,ema50=4202,pullback_depth=pb.depth,pullback_fraction=pb.retracement_fraction,pullback_class=pb.classification,structure_intact=True,ema_aligned=True)
    text=render_card_text(reentry_observation_card(row))
    assert "RE-ENTRY OBSERVATION · research only" in text
    assert "No order is requested" in text
    assert "BUY NOW" not in text and "SELL NOW" not in text

def test_097_similar_journey_card_shows_sample_size_and_outcomes():
    query=_row(99)
    history=[_row(i,mfe=10 if i%2 else 5) for i in range(6)]
    matches=retrieve_similar_journeys(query,history,limit=5)
    text=render_card_text(similar_journey_evidence_card("XAUUSD",matches))
    assert "SIMILAR JOURNEYS" in text
    assert "n=5" in text
    assert "+3" in text and "+5" in text and "+10" in text
