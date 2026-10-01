"""Aureon V3 EMA learning, confidence and validation tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aureon.discord.service import build_notification
from aureon.models.base import MarketTime
from aureon.models.detection import (
    AgentEvidence,
    Detection,
    IndicatorSnapshot,
    SessionContext,
)
from aureon.models.ema_journey_v3 import (
    EMAAnchorFeaturesV3,
    EMAAnchorOutcome,
    EMAAnchorType,
    EMAJourneyAnchor,
    EMAMovementJourney,
    JourneyEndReason,
    JourneyStatus,
    TargetOutcome,
)
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.ml import ModelRegistryEntry
from aureon.services.agent_consensus import build_agent_consensus
from aureon.services.v3_ema_learning import (
    AgentConfidenceSnapshot,
    CanonicalEMAExampleV3,
    EMAFailureType,
    EMAOutcomeV3,
    canonical_example,
    classify_failure,
    freeze_anchor_features,
)
from aureon.services.v3_ema_model import fit_v3_bundle, predict_v3

TZ = "Europe/Athens"
BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _detection(
    *,
    detection_id: str,
    agent: str = "ema_cross",
    direction: Direction = Direction.SELL,
    minute: int = 0,
    price: float = 4200.0,
) -> Detection:
    close_at = BASE + timedelta(minutes=minute)
    return Detection(
        detection_id=detection_id,
        account_scope="test",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        agent_name=agent,
        agent_version="test",
        event_key="bearish" if direction is Direction.SELL else "bullish",
        direction=direction,
        detected_at=MarketTime.from_utc(close_at, TZ),
        candle_open_time=MarketTime.from_utc(close_at - timedelta(minutes=5), TZ),
        price=price,
        indicators=IndicatorSnapshot(
            ema={"fast": 4198.0, "slow": 4200.0, "ema200": 4185.0},
            rsi=42.0,
        ),
        evidence=AgentEvidence(
            numeric={
                "ema_gap": -2.0,
                "ema_gap_change": -0.8,
                "fast_slope": -1.1,
                "rsi_change": -4.0,
            },
            categorical={
                "trend_direction": "DOWN",
                "pre_cross_pattern": "TRENDING",
                "cross_quality": "STRONG",
                "ema_relation": "fast_below",
                "ema200_context": "price_above_ema200",
                "market_structure": "LH_LL",
            },
            flags={},
        ),
        session=SessionContext(
            session=SessionName.LONDON,
            session_config_version=1,
        ),
        sequence_today=40,
        sequence_session=30,
    )


def _features(index: int) -> EMAAnchorFeaturesV3:
    bullish = index % 2 == 0
    return EMAAnchorFeaturesV3(
        trend_direction="UP" if bullish else "DOWN",
        pattern="TRENDING" if index % 3 else "GRADUAL_MOMENTUM_SHIFT",
        cross_quality="STRONG" if index % 4 else "NORMAL",
        ema20=4200.0 + index,
        ema50=4198.0 + index,
        ema200=4180.0 + index,
        ema_gap=2.0 if bullish else -2.0,
        ema_gap_change=0.5 if bullish else -0.5,
        ema_slope=1.0 if bullish else -1.0,
        price_vs_ema200="above_ema200" if bullish else "below_ema200",
        ema20_50_relation="fast_above" if bullish else "fast_below",
        rsi=58.0 if bullish else 42.0,
        rsi_change=2.0 if bullish else -2.0,
        atr=4.0 + (index % 5) * 0.2,
        volatility_regime="normal",
        session="london" if index % 3 else "new_york",
        session_phase="MID",
        market_structure="HH_HL" if bullish else "LH_LL",
        htf_alignment="aligned" if index % 4 else "mixed",
        internal_agents={"breakout": "supportive:break"},
        agent_confidence=AgentConfidenceSnapshot(
            label="HIGH",
            supportive=4,
            neutral=1,
            conflicting=0,
            coverage=5,
            votes={"breakout": "supportive"},
        ),
    )


def _example(index: int) -> CanonicalEMAExampleV3:
    reached_10 = index % 3 != 0
    reached_20 = index % 4 == 0
    reached_30 = index % 7 == 0
    reached_40 = index % 11 == 0
    when = BASE + timedelta(days=index)
    return CanonicalEMAExampleV3(
        example_id=f"e-{index}",
        journey_id=f"j-{index // 2}",
        detection_id=f"d-{index}",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction=Direction.BUY if index % 2 == 0 else Direction.SELL,
        market_date=when.date().isoformat(),
        anchor_type=EMAAnchorType.EMA20_50_CROSS.value,
        features=_features(index),
        movement_from_journey_start=float(index % 10),
        outcome=EMAOutcomeV3(
            reached_5=index % 5 != 0,
            reached_10=reached_10,
            reached_20=reached_20,
            reached_30=reached_30,
            reached_40=reached_40,
            clean_10=reached_10 and index % 6 != 0,
            bars_to_5=2 if index % 5 != 0 else None,
            bars_to_10=4 if reached_10 else None,
            bars_to_20=8 if reached_20 else None,
            bars_to_30=12 if reached_30 else None,
            bars_to_40=16 if reached_40 else None,
            mfe=25.0 if reached_20 else 12.0 if reached_10 else 3.0,
            mae=2.5 if reached_10 else 8.0,
            failure_type=(
                EMAFailureType.NONE
                if reached_10
                else EMAFailureType.NO_CONTINUATION
            ),
            resolved_at=when + timedelta(hours=2),
        ),
        generated_at=when + timedelta(hours=2),
    )


def test_feature_freeze_contains_context_and_agent_consensus() -> None:
    trigger = _detection(detection_id="trigger")
    peers = [
        trigger,
        _detection(detection_id="breakout", agent="breakout"),
        _detection(detection_id="liquidity", agent="liquidity"),
        _detection(
            detection_id="opposite",
            agent="ema_rsi_eligibility",
            direction=Direction.BUY,
        ),
    ]

    frozen = freeze_anchor_features(trigger, same_candle=peers)

    assert frozen.trend_direction == "DOWN"
    assert frozen.cross_quality == "STRONG"
    assert frozen.market_structure == "LH_LL"
    assert frozen.rsi == 42.0
    assert frozen.session == "london"
    assert frozen.session_phase == "MID"
    assert frozen.agent_confidence.supportive == 2
    assert frozen.agent_confidence.conflicting == 1
    assert frozen.agent_confidence.label == "HIGH"


def test_failed_outcome_is_preserved_as_training_example() -> None:
    anchor = EMAJourneyAnchor(
        detection_id="d-fail",
        anchor_type=EMAAnchorType.EMA200_CROSS,
        direction=Direction.SELL,
        detected_at=BASE,
        price=4180.0,
        movement_from_journey_start=18.0,
        features=_features(1),
        outcome=EMAAnchorOutcome(
            bars_observed=12,
            mfe=2.0,
            mae=11.0,
            targets={
                "5": TargetOutcome(),
                "10": TargetOutcome(),
                "20": TargetOutcome(),
                "30": TargetOutcome(),
                "40": TargetOutcome(),
            },
            completed=True,
            end_reason=JourneyEndReason.MAX_HORIZON,
            completed_at=BASE + timedelta(hours=1),
        ),
    )
    journey = EMAMovementJourney(
        journey_id="j-fail",
        account_scope="test",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction=Direction.SELL,
        market_date="2026-09-01",
        started_at=BASE,
        start_price=4198.0,
        anchors=(anchor,),
        status=JourneyStatus.CLOSED,
        ended_at=BASE + timedelta(hours=1),
        end_reason=JourneyEndReason.MAX_HORIZON,
    )

    assert classify_failure(anchor) is EMAFailureType.IMMEDIATE_REVERSAL
    example = canonical_example(
        journey,
        anchor,
        generated_at=BASE + timedelta(hours=1),
    )
    assert example.outcome.clean_10 is False
    assert example.outcome.failure_type is EMAFailureType.IMMEDIATE_REVERSAL


def test_multi_output_model_is_monotonic_and_includes_mfe_mae() -> None:
    examples = [_example(index) for index in range(45)]
    bundle = fit_v3_bundle(examples, min_samples=30)
    model = ModelRegistryEntry(
        model_id="xau-v3-test",
        symbol="XAUUSD",
        algorithm="logistic_linear_v3",
        status="candidate",
        feature_schema_version="AUREON_EMA_FEATURES_V3",
        label_schema_version="AUREON_EMA_MOVEMENT_V3",
        model_schema_version="AUREON_EMA_MODEL_V3",
        trained_from=examples[0].market_date,
        trained_through=examples[-1].market_date,
        training_samples=len(examples),
        target_metrics=bundle.metrics,
        validation_metrics=bundle.validation,
        artifact=bundle.artifact(),
        created_at=BASE,
    )

    prediction = predict_v3(model, _features(10), min_samples=30)

    assert prediction.sufficient_data is True
    values = [
        prediction.probability_reach_5,
        prediction.probability_reach_10,
        prediction.probability_reach_20,
        prediction.probability_reach_30,
        prediction.probability_reach_40,
    ]
    assert all(value is not None for value in values)
    numeric = [float(value) for value in values if value is not None]
    assert numeric == sorted(numeric, reverse=True)
    assert prediction.expected_mfe is not None
    assert prediction.expected_mae is not None


def test_model_confidence_fails_closed_on_unseen_context() -> None:
    examples = [_example(index) for index in range(45)]
    bundle = fit_v3_bundle(examples, min_samples=30)
    model = ModelRegistryEntry(
        model_id="xau-v3-ood",
        symbol="XAUUSD",
        algorithm="logistic_linear_v3",
        status="candidate",
        feature_schema_version="AUREON_EMA_FEATURES_V3",
        label_schema_version="AUREON_EMA_MOVEMENT_V3",
        model_schema_version="AUREON_EMA_MODEL_V3",
        trained_from=examples[0].market_date,
        trained_through=examples[-1].market_date,
        training_samples=len(examples),
        target_metrics=bundle.metrics,
        validation_metrics=bundle.validation,
        artifact=bundle.artifact(),
        created_at=BASE,
    )
    unseen = _features(10).model_copy(update={"session": "never_seen_session"})

    prediction = predict_v3(model, unseen, min_samples=30)

    assert prediction.sufficient_data is False
    assert prediction.out_of_distribution is True
    assert "OUT_OF_DISTRIBUTION" in prediction.reason


def test_discord_keeps_agent_and_model_confidence_separate() -> None:
    detection = _detection(detection_id="discord")
    peers = [
        detection,
        _detection(detection_id="b", agent="breakout"),
        _detection(detection_id="l", agent="liquidity"),
        _detection(detection_id="w", agent="wick", direction=Direction.SELL),
    ]
    consensus = build_agent_consensus(detection, peers)
    screen = build_notification(
        detection,
        consensus=consensus,
        model_confidence={
            "sufficient_data": True,
            "sample_count": 212,
            "probability_reach_5": 0.84,
            "probability_reach_10": 0.71,
            "probability_reach_20": 0.48,
            "probability_reach_30": 0.30,
            "probability_reach_40": 0.18,
            "probability_clean_10": 0.63,
            "expected_mfe": 16.4,
            "expected_mae": 3.1,
        },
        movement_since_pre_cross=14.2,
    )
    fields = dict(screen.fields)

    assert "Agent consensus" in fields
    assert "Model confidence" in fields
    assert "Combined view" in fields
    assert "Agents" in fields["Combined view"]
    assert "P(+10) 71%" in fields["Combined view"]
    assert fields["Move since pre-cross"] == "+14.20 dollars"
