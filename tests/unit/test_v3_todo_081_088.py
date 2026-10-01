"""Aureon V3 TODO 081-088 acceptance tests."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from aureon.models.base import MarketTime
from aureon.models.detection import Detection, SessionContext
from aureon.models.ema_journey_v3 import (
    EMAAnchorFeaturesV3,
    EMAAnchorType,
    JourneyEndReason,
    JourneyOrigin,
)
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.market import Candle
from aureon.models.ml import ModelRegistryEntry, TargetMetrics
from aureon.services.ema_journey_tracker import EMAMovementJourneyTracker
from aureon.services.v3_ema_governance import (
    V3EMAGovernance,
    V3EMAGovernancePolicy,
)
from aureon.services.v3_ema_learning import (
    AgentConfidenceSnapshot,
    CanonicalEMAExampleV3,
    EMAFailureType,
    EMAOutcomeV3,
)
from aureon.services.v3_ema_model import journey_weights, walk_forward_v3
from aureon.services.v3_ema_research import continuation_report
from aureon.services.v3_target_ladder import target_ladder_for_symbol

BASE = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
TZ = "Europe/Athens"


def _detection(
    detection_id: str,
    agent: str,
    direction: Direction,
    *,
    minute: int = 0,
    price: float = 4200.0,
    symbol: str = "XAUUSD",
) -> Detection:
    at = BASE + timedelta(minutes=minute)
    return Detection(
        detection_id=detection_id,
        account_scope="test",
        symbol=symbol,
        timeframe=Timeframe.M5,
        agent_name=agent,
        agent_version="test",
        event_key="bullish" if direction is Direction.BUY else "bearish",
        direction=direction,
        detected_at=MarketTime.from_utc(at, TZ),
        candle_open_time=MarketTime.from_utc(at - timedelta(minutes=5), TZ),
        price=price,
        session=SessionContext(
            session=SessionName.LONDON,
            session_config_version=1,
        ),
        sequence_today=1,
        sequence_session=1,
    )


def _candle(minute: int, *, price: float = 4200.0) -> Candle:
    return Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(BASE + timedelta(minutes=minute), TZ),
        open=price,
        high=price + 1.0,
        low=price - 1.0,
        close=price,
    )


def _features(session: str = "london") -> EMAAnchorFeaturesV3:
    return EMAAnchorFeaturesV3(
        trend_direction="DOWN",
        pattern="TRENDING",
        cross_quality="STRONG",
        session=session,
        session_phase="MID",
        volatility_regime="normal",
        agent_confidence=AgentConfidenceSnapshot(
            label="HIGH",
            supportive=4,
            neutral=1,
            conflicting=0,
            coverage=5,
        ),
    )


def _example(
    index: int,
    *,
    journey_id: str,
    generated_at: datetime,
    resolved_at: datetime,
    session: str = "london",
    direction: Direction = Direction.SELL,
    clean: bool | None = None,
) -> CanonicalEMAExampleV3:
    hit = bool(index % 2) if clean is None else clean
    return CanonicalEMAExampleV3(
        example_id=f"e-{index}-{journey_id}",
        journey_id=journey_id,
        detection_id=f"d-{index}-{journey_id}",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction=direction,
        market_date=generated_at.date().isoformat(),
        anchor_type=EMAAnchorType.EMA20_50_CROSS.value,
        features=_features(session),
        movement_from_journey_start=0.0,
        independence_weight=1.0,
        outcome=EMAOutcomeV3(
            reached_3=True,
            reached_5=True,
            reached_10=hit,
            reached_20=False,
            reached_30=False,
            reached_40=False,
            clean_10=hit,
            bars_to_3=1,
            bars_to_5=2,
            bars_to_10=4 if hit else None,
            mfe=12.0 if hit else 4.0,
            mae=2.0 if hit else 7.0,
            failure_type=(
                EMAFailureType.NONE if hit else EMAFailureType.NO_CONTINUATION
            ),
            resolved_at=resolved_at,
        ),
        generated_at=generated_at,
    )


def test_pre_cross_without_confirmation_expires_explicitly() -> None:
    tracker = EMAMovementJourneyTracker(max_horizon_bars=2)
    journey = tracker.on_detection(
        _detection("pre", "ema200_pre_cross", Direction.SELL)
    )
    assert journey is not None
    assert journey.origin is JourneyOrigin.PRE_CROSS

    tracker.on_closed_candle(_candle(0))
    tracker.on_closed_candle(_candle(5, price=4199.0))
    tracker.on_closed_candle(_candle(10, price=4198.0))

    assert journey.end_reason is JourneyEndReason.PRE_CROSS_EXPIRED
    assert journey.has_confirmed_cross is False


def test_confirmed_cross_without_pre_pressure_starts_valid_journey() -> None:
    tracker = EMAMovementJourneyTracker()
    journey = tracker.on_detection(
        _detection("cross", "ema_cross", Direction.BUY)
    )

    assert journey is not None
    assert journey.origin is JourneyOrigin.CONFIRMED_CROSS
    assert journey.has_pre_cross is False
    assert journey.has_confirmed_cross is True


def test_opposite_pre_pressure_does_not_replace_open_journey() -> None:
    tracker = EMAMovementJourneyTracker()
    original = tracker.on_detection(
        _detection("sell-cross", "ema_cross", Direction.SELL)
    )
    ignored = tracker.on_detection(
        _detection(
            "buy-pre",
            "ema200_pre_cross",
            Direction.BUY,
            minute=5,
        )
    )

    assert ignored is None
    assert tracker.active_for("XAUUSD", "M5") is original


def test_opposite_confirmed_cross_closes_then_starts_new_journey() -> None:
    tracker = EMAMovementJourneyTracker()
    original = tracker.on_detection(
        _detection("sell-cross", "ema_cross", Direction.SELL)
    )
    replacement = tracker.on_detection(
        _detection("buy-cross", "ema_cross", Direction.BUY, minute=5)
    )

    assert original is not None and replacement is not None
    assert original.end_reason is JourneyEndReason.OPPOSITE_CONFIRMED_CROSS
    assert replacement.direction is Direction.BUY
    assert replacement.journey_id != original.journey_id


def test_target_ladders_are_symbol_specific() -> None:
    gold = target_ladder_for_symbol("XAUUSD")
    silver = target_ladder_for_symbol("XAGUSD")
    generic = target_ladder_for_symbol("EURUSD", point_size=0.00001)

    assert gold.targets == (3.0, 5.0, 10.0, 20.0, 30.0, 40.0)
    assert silver.targets == (0.03, 0.05, 0.10, 0.20, 0.30, 0.40)
    assert generic.targets[0] == 0.003
    assert gold.targets != silver.targets


def test_journey_weights_sum_to_one_per_underlying_move() -> None:
    examples = [
        _example(
            1,
            journey_id="j1",
            generated_at=BASE,
            resolved_at=BASE + timedelta(hours=1),
        ),
        _example(
            2,
            journey_id="j1",
            generated_at=BASE + timedelta(minutes=5),
            resolved_at=BASE + timedelta(hours=1),
        ),
        _example(
            3,
            journey_id="j1",
            generated_at=BASE + timedelta(minutes=10),
            resolved_at=BASE + timedelta(hours=1),
        ),
        _example(
            4,
            journey_id="j2",
            generated_at=BASE + timedelta(minutes=15),
            resolved_at=BASE + timedelta(hours=1),
        ),
    ]
    weights = journey_weights(examples)
    totals: dict[str, float] = defaultdict(float)
    for example, weight in zip(examples, weights, strict=True):
        totals[example.journey_id] += weight

    assert totals["j1"] == 1.0
    assert totals["j2"] == 1.0


def test_walk_forward_purges_outcomes_inside_embargo_window() -> None:
    examples: list[CanonicalEMAExampleV3] = []
    for index in range(50):
        day = BASE + timedelta(days=index // 10)
        # Resolve early enough except the last two examples of each training day.
        resolved = day + timedelta(hours=1)
        if index % 10 >= 8:
            resolved = day + timedelta(hours=23, minutes=30)
        examples.append(
            _example(
                index,
                journey_id=f"j-{index}",
                generated_at=day + timedelta(minutes=index % 10),
                resolved_at=resolved,
            )
        )

    report = walk_forward_v3(
        examples,
        min_train_samples=10,
        test_days=1,
        embargo_bars=12,
        timeframe_seconds=300,
        min_cell_samples=2,
    )

    assert report["complete"] is True
    assert any(
        fold["purged_or_embargoed"] > 0
        for fold in report["folds"]
    )


def test_small_research_cells_do_not_publish_hit_rates() -> None:
    rows = [
        _example(
            index,
            journey_id=f"j-{index}",
            generated_at=BASE + timedelta(days=index),
            resolved_at=BASE + timedelta(days=index, hours=1),
        )
        for index in range(5)
    ]

    report = continuation_report(rows, min_cell_samples=20)
    london = report["by_session"]["london:MID"]

    assert london["samples"] == 5
    assert london["insufficient"] is True
    assert "reach_10_rate" not in london


def test_governance_policy_has_numeric_probability_quality_gates() -> None:
    policy = V3EMAGovernancePolicy()

    assert policy.max_clean_brier == 0.22
    assert policy.max_clean_log_loss == 0.65
    assert policy.max_clean_calibration_error == 0.10
    assert policy.min_base_rate_brier_improvement == 0.01
    assert policy.max_shadow_brier == 0.22
    assert policy.max_shadow_log_loss == 0.65


def test_candidate_fails_when_it_does_not_beat_base_rate() -> None:
    clean = TargetMetrics(
        samples=40,
        positives=20,
        negatives=20,
        accuracy=0.65,
        precision=0.65,
        recall=0.65,
        brier=0.18,
        log_loss=0.55,
        false_positive_rate=0.25,
    )
    model = ModelRegistryEntry(
        model_id="v3-candidate",
        symbol="XAUUSD",
        status="candidate",
        feature_schema_version="AUREON_EMA_FEATURES_V3",
        label_schema_version="AUREON_EMA_MOVEMENT_V3",
        model_schema_version="AUREON_EMA_MODEL_V3",
        trained_from="2026-01-01",
        trained_through="2026-08-31",
        training_samples=100,
        target_metrics={"clean_10": clean},
        validation_metrics={
            "calibration_error": {"clean_10": 0.05},
            "base_rate_gate": {
                "clean_10": {
                    "eligible_validation_samples": 40,
                    "brier_improvement": 0.0,
                    "beats_base_rate": False,
                }
            },
            "walk_forward": {"complete": True},
        },
        created_at=BASE,
    )

    class Models:
        def get_model(self, model_id):
            return model

        def set_status(self, model_id, status, **kwargs):
            return {"status": status, **kwargs}

    governance = V3EMAGovernance(Models(), learning=object())
    result = governance.qualify_candidate("v3-candidate")

    assert result["status"] == "rejected"
    assert "base-rate Brier improvement" in result["promotion_reason"]
