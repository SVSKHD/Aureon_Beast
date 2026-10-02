"""Acceptance coverage for Aureon V4 Gold TODO 061-070."""
from __future__ import annotations

from datetime import UTC, datetime

from aureon.models.ema_journey_v3 import EMAAnchorFeaturesV3
from aureon.models.ema_journey_v4 import (
    V4MovementFeatures,
    V4PullbackClass,
    V4ReentryObservation,
    V4ReentryOutcome,
    V4RemainingMovementExample,
    V4RemainingMovementLabels,
    V4VolumeTrajectory,
)
from aureon.models.enums import Direction, Timeframe
from aureon.services.v4_specialist_similarity import (
    build_regime_router,
    explain_model_evidence,
    fit_exhaustion_specialist,
    fit_mae_specialist,
    fit_pullback_specialist,
    fit_remaining_move_specialist,
    journey_embedding,
    predict_exhaustion,
    predict_mae,
    predict_pullback_continuation,
    retrieve_similar_journeys,
    route_regime,
    summarize_similar_outcomes,
)
from aureon.services.v4_ema_model import predict_remaining_move


def _features(index: int = 0, *, regime: str = "aligned") -> V4MovementFeatures:
    return V4MovementFeatures(
        anchor_type="ema20_50_cross",
        base=EMAAnchorFeaturesV3(
            session="london",
            session_phase="EARLY",
            htf_alignment=regime,
            trend_direction="UP",
        ),
        movement_consumed=float(index % 6),
        pre_cross_movement=2.0 + (index % 3),
        pre_cross_bars=2 + (index % 4),
        pre_cross_seconds=600.0 + index * 5,
        cross_order="ema20_50_cross",
    )


def _example(index: int, *, hit: bool | None = None, regime: str = "aligned") -> V4RemainingMovementExample:
    if hit is None:
        hit = index % 3 != 0
    return V4RemainingMovementExample(
        journey_id=f"j-{index}",
        detection_id=f"d-{index}",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction=Direction.BUY,
        market_date=f"2026-09-{(index % 28) + 1:02d}",
        features=_features(index, regime=regime),
        labels=V4RemainingMovementLabels(
            reached_3=hit,
            reached_5=hit and index % 4 != 0,
            reached_10=hit and index % 5 == 0,
            remaining_mfe=9.0 if hit else 1.5,
            remaining_mae=1.0 if hit else 5.0,
        ),
    )


def _pullback(index: int, *, continued: bool) -> V4ReentryObservation:
    volume = V4VolumeTrajectory(
        expansion_volume_sum=360,
        expansion_volume_bars=2,
        expansion_volume_mean=180,
        pullback_volume_sum=180,
        pullback_volume_bars=2,
        pullback_volume_mean=90,
        pullback_volume_contracting=True,
    )
    return V4ReentryObservation(
        reentry_id=f"re-{index}",
        pullback_id=f"pb-{index}",
        journey_id=f"j-{index}",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction=Direction.BUY,
        observed_at=datetime(2026, 9, 1, 8, index % 60, tzinfo=UTC),
        price=4100.0 + index,
        ema20=4099.0,
        ema50=4097.0,
        ema200=4080.0,
        atr=5.0,
        pullback_depth=1.5 if continued else 4.0,
        pullback_fraction=0.25 if continued else 0.65,
        pullback_class=V4PullbackClass.NORMAL if continued else V4PullbackClass.DEEP,
        structure_intact=continued,
        ema_aligned=continued,
        volume=volume,
        outcome=V4ReentryOutcome(
            mfe=6.0 if continued else 1.0,
            mae=1.0 if continued else 4.0,
            reached_3=continued,
            reached_5=continued,
            failed=not continued,
            completed=True,
        ),
    )


def test_061_remaining_move_specialist_reuses_full_target_ladder() -> None:
    rows = [_example(i) for i in range(30)]
    specialist = fit_remaining_move_specialist(rows, min_samples=20)
    prediction = predict_remaining_move(specialist, rows[1].features)
    ladder = [prediction[f"reached_{target}"] for target in (3, 5, 10, 20, 30, 40)]
    assert ladder == sorted(ladder, reverse=True)
    assert "expected_remaining_mfe" in prediction


def test_062_pullback_specialist_uses_resolved_reentry_evidence() -> None:
    rows = [_pullback(i, continued=i % 3 != 0) for i in range(30)]
    specialist = fit_pullback_specialist(rows, min_samples=20)
    probability = predict_pullback_continuation(specialist, rows[1])
    assert 0.0 <= probability <= 1.0
    assert specialist.samples == 30


def test_063_exhaustion_specialist_predicts_probability() -> None:
    rows = [_example(i) for i in range(30)]
    specialist = fit_exhaustion_specialist(rows, min_samples=20)
    probability = predict_exhaustion(specialist, rows[0].features)
    assert 0.0 <= probability <= 1.0


def test_064_mae_specialist_returns_non_negative_expected_adverse_excursion() -> None:
    rows = [_example(i) for i in range(30)]
    specialist = fit_mae_specialist(rows, min_samples=20)
    assert predict_mae(specialist, rows[1].features) >= 0.0


def test_065_regime_router_requires_support_and_falls_back_when_sparse() -> None:
    rows = [
        *[_example(i, regime="aligned") for i in range(25)],
        *[_example(100 + i, regime="against") for i in range(5)],
    ]
    routes = build_regime_router(rows, min_samples=20)
    aligned = route_regime(routes, rows[0].features)
    sparse = route_regime(routes, rows[-1].features)
    assert aligned is not None and aligned.specialist is not None and not aligned.fallback
    assert sparse is not None and sparse.specialist is None and sparse.fallback


def test_066_068_similarity_embedding_retrieval_and_summary_are_deterministic() -> None:
    query = _example(200, hit=True)
    near = query.model_copy(
        update={
            "journey_id": "near",
            "detection_id": "near-d",
            "features": query.features.model_copy(update={"movement_consumed": query.features.movement_consumed + 0.1}),
        }
    )
    far = _example(201, hit=False, regime="against")
    history = [far, near, *[_example(i) for i in range(10)]]

    embedding = journey_embedding(query)
    assert embedding.journey_id == query.journey_id
    matches = retrieve_similar_journeys(query, history, limit=5)
    assert matches[0].journey_id == "near"
    assert all(matches[i].distance <= matches[i + 1].distance for i in range(len(matches) - 1))

    summary = summarize_similar_outcomes(matches)
    assert summary["samples"] == 5
    assert 0.0 <= summary["reach_5_rate"] <= 1.0
    assert summary["mean_remaining_mfe"] >= 0.0


def test_069_070_explanations_keep_positive_and_negative_evidence_separate() -> None:
    query = _example(2, hit=True)
    history = [_example(i) for i in range(30)]
    matches = retrieve_similar_journeys(query, history, limit=10)
    evidence = explain_model_evidence(query, matches)
    assert set(evidence) == {"positive", "negative"}
    assert evidence["positive"]
    assert all(isinstance(item, str) for item in evidence["positive"] + evidence["negative"])
