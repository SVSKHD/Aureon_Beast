"""Aureon V3 validation, research and data-quality acceptance tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import pandas as pd
import pytest

from aureon.engine.indicators import IncrementalEMA, IncrementalWilderRSI, ema, rsi
from aureon.models.base import MarketTime
from aureon.models.detection import AgentEvidence, Detection, SessionContext
from aureon.models.ema_journey_v3 import (
    AgentConfidenceSnapshot,
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
from aureon.models.market import Candle
from aureon.services.ema_journey_tracker import EMAMovementJourneyTracker
from aureon.services.high_impact_news import HighImpactNewsTagger
from aureon.services.v3_ema_learning import (
    CanonicalEMAExampleV3,
    EMAFailureType,
    EMAHoldoutDayV3,
    EMAOutcomeV3,
)
from aureon.services.v3_ema_research import (
    V3EMAValidationService,
    continuation_report,
    placement_report,
    score_predictions,
)
from aureon.services.volume_features import volume_features

TZ = "Europe/Athens"
BASE = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)


def _features(
    *,
    session: str = "london",
    phase: str = "MID",
    regime: str = "normal",
    confidence: str = "HIGH",
) -> EMAAnchorFeaturesV3:
    return EMAAnchorFeaturesV3(
        trend_direction="DOWN",
        pattern="TRENDING",
        cross_quality="STRONG",
        ema20=4155.0,
        ema50=4157.0,
        ema200=4160.0,
        ema_gap=-2.0,
        ema_gap_change=-0.5,
        ema_slope=-0.8,
        price_vs_ema200="below_ema200",
        ema20_50_relation="fast_below",
        rsi=43.0,
        rsi_change=-3.0,
        atr=4.2,
        volatility_regime=regime,
        tick_volume=1800.0,
        volume_ratio_to_median=1.5,
        volume_percentile=0.85,
        volume_state="expanding",
        volume_price_alignment="expanding_bearish",
        pre_cross_volume_3bar_mean=1700.0,
        pre_cross_volume_5bar_mean=1600.0,
        spread_points=50.0,
        session=session,
        session_phase=phase,
        market_structure="LH_LL",
        htf_alignment="aligned",
        agent_confidence=AgentConfidenceSnapshot(
            label=confidence,
            supportive=4,
            neutral=1,
            conflicting=0,
            coverage=5,
        ),
    )


def _example(
    index: int,
    *,
    anchor: str = "ema20_50_cross",
    session: str = "london",
    phase: str = "MID",
    regime: str = "normal",
) -> CanonicalEMAExampleV3:
    reached_10 = index % 3 != 0
    when = BASE + timedelta(days=index)
    return CanonicalEMAExampleV3(
        example_id=f"example-{index}-{anchor}",
        journey_id=f"journey-{index // 3}",
        detection_id=f"detection-{index}-{anchor}",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction=Direction.SELL,
        market_date=when.date().isoformat(),
        anchor_type=anchor,
        features=_features(session=session, phase=phase, regime=regime),
        movement_from_journey_start=float(index % 12),
        independence_weight=0.5,
        outcome=EMAOutcomeV3(
            reached_3=True,
            reached_5=index % 4 != 0,
            reached_10=reached_10,
            reached_20=index % 5 == 0,
            reached_30=False,
            reached_40=False,
            clean_10=reached_10 and index % 6 != 0,
            bars_to_3=1,
            bars_to_5=2,
            bars_to_10=4 if reached_10 else None,
            mfe=15.0 if reached_10 else 4.0,
            mae=2.0 if reached_10 else 7.0,
            first_favourable_bar=1,
            dollars_per_bar=2.0,
            adverse_before_targets={"3": 0.8, "5": 1.2, "10": 2.0},
            reference_price=4160.0,
            reference_price_kind="bid_at_detection",
            spread_accounted=True,
            failure_type=(
                EMAFailureType.NONE
                if reached_10
                else EMAFailureType.NO_CONTINUATION
            ),
            resolved_at=when + timedelta(hours=2),
        ),
        generated_at=when + timedelta(hours=2),
    )


def _detection(*, direction: Direction = Direction.SELL, price: float = 4160.0) -> Detection:
    return Detection(
        detection_id=f"d-{direction.value}-{price}",
        account_scope="test",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        agent_name="ema_cross",
        agent_version="test",
        event_key="bearish" if direction is Direction.SELL else "bullish",
        direction=direction,
        detected_at=MarketTime.from_utc(BASE, TZ),
        candle_open_time=MarketTime.from_utc(BASE - timedelta(minutes=5), TZ),
        price=price,
        evidence=AgentEvidence(
            numeric={"spread_points": 50.0},
            categorical={"trend_direction": "DOWN"},
        ),
        session=SessionContext(
            session=SessionName.LONDON,
            session_config_version=1,
        ),
        sequence_today=1,
        sequence_session=1,
    )


def _candle(minutes: int, high: float, low: float, close: float, spread: int = 50) -> Candle:
    return Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(BASE + timedelta(minutes=minutes), TZ),
        open=close,
        high=high,
        low=low,
        close=close,
        spread=spread,
    )


def test_holdout_scoring_counts_prediction_errors_and_requires_release_order() -> None:
    holdout = EMAHoldoutDayV3(
        holdout_id="h1",
        symbol="XAUUSD",
        market_date="2026-10-02",
        frozen_model_id="m1",
        status="open",
        created_at=BASE,
    )
    rows = [
        {
            "payload": {
                "probability_reach_3": 0.9,
                "probability_reach_5": 0.8,
                "probability_reach_10": 0.75,
                "probability_reach_20": 0.4,
                "probability_reach_30": 0.2,
                "probability_reach_40": 0.1,
                "probability_clean_10": 0.8,
                "expected_mfe": 15.0,
                "expected_mae": 2.0,
            },
            "actual_outcome": {
                "reached_3": True,
                "reached_5": True,
                "reached_10": False,
                "reached_20": False,
                "reached_30": False,
                "reached_40": False,
                "clean_10": False,
                "mfe": 7.0,
                "mae": 6.0,
            },
        }
    ]

    class Repo:
        def __init__(self):
            self.holdout = holdout

        def holdout_for(self, symbol, market_date):
            return self.holdout

        def predictions_for_market_date(self, *args, **kwargs):
            return rows

        def update_holdout(self, updated):
            self.holdout = updated
            return updated

    repo = Repo()
    service = V3EMAValidationService(repo, now=lambda: BASE + timedelta(days=2))

    with pytest.raises(ValueError, match="scored"):
        service.release_holdout("XAUUSD", "2026-10-02")

    scored = service.score_holdout("XAUUSD", "2026-10-02")
    assert scored.status == "scored"
    assert scored.metrics["false_positives_clean_10"] == 1
    assert scored.metrics["targets"]["10"]["samples"] == 1

    released = service.release_holdout("XAUUSD", "2026-10-02")
    assert released.status == "released"


def test_continuation_report_segments_anchor_regime_session_and_confidence() -> None:
    examples = [
        _example(1, anchor="ema20_50_cross", session="london", phase="EARLY"),
        _example(2, anchor="ema200_cross", session="london", phase="MID"),
        _example(
            3,
            anchor="pre_cross",
            session="new_york",
            phase="EARLY",
            regime="high",
        ),
    ]

    report = continuation_report(examples)

    assert report["by_anchor"]["ema20_50_cross"]["samples"] == 1
    assert report["by_anchor"]["ema200_cross"]["samples"] == 1
    assert report["by_anchor"]["pre_cross"]["samples"] == 1
    assert report["by_session"]["london:EARLY"]["samples"] == 1
    assert report["by_regime"]["high"]["samples"] == 1
    assert report["by_agent_confidence"]["HIGH"]["samples"] == 3


def test_score_predictions_reports_calibration_buckets() -> None:
    rows = [
        {
            "payload": {
                "probability_reach_3": 0.82,
                "probability_clean_10": 0.76,
            },
            "actual_outcome": {"reached_3": True, "clean_10": True},
        },
        {
            "payload": {
                "probability_reach_3": 0.78,
                "probability_clean_10": 0.74,
            },
            "actual_outcome": {"reached_3": False, "clean_10": False},
        },
    ]

    report = score_predictions(rows)

    clean = report["targets"]["clean_10"]
    assert clean["samples"] == 2
    assert clean["calibration"]["70-80"]["n"] == 2


def test_normalized_volume_features_capture_expansion_and_alignment() -> None:
    frame = pd.DataFrame(
        {
            "open": [100.0] * 21,
            "high": [101.0] * 21,
            "low": [99.0] * 21,
            "close": [100.0] * 20 + [99.0],
            "tick_volume": [100] * 20 + [200],
            "spread": [20] * 21,
        }
    )

    numeric, categorical, flags = volume_features(frame)

    assert numeric["tick_volume_ratio_to_median"] == pytest.approx(2.0)
    assert numeric["tick_volume_percentile"] == pytest.approx(1.0)
    assert numeric["spread_points"] == 20.0
    assert categorical["volume_state"] == "expanding"
    assert categorical["volume_price_alignment"] == "expanding_bearish"
    assert flags["volume_expanding"] is True


def test_spread_aware_sell_outcome_uses_ask_for_future_exit() -> None:
    tracker = EMAMovementJourneyTracker(
        max_horizon_bars=10,
        point_size_by_symbol={"XAUUSD": 0.01},
    )
    journey = tracker.on_detection(_detection(direction=Direction.SELL, price=4160.0))
    assert journey is not None
    anchor = journey.anchors[0]
    assert anchor.reference_price == 4160.0
    assert anchor.reference_price_kind == "bid_at_detection"

    tracker.on_closed_candle(_candle(0, 4160.5, 4159.5, 4160.0))
    tracker.on_closed_candle(_candle(5, 4161.0, 4156.0, 4157.0))

    outcome = tracker.active_for("XAUUSD", "M5").anchors[0].outcome
    # low bid 4156 + 0.50 spread = 4156.50 ask; sell continuation = 3.50.
    assert outcome.mfe == pytest.approx(3.5)
    assert outcome.mae == pytest.approx(1.5)
    assert outcome.targets["3"].reached is True
    assert outcome.targets["3"].adverse_before_reach == pytest.approx(1.5)
    assert outcome.first_favourable_bar == 1
    assert outcome.dollars_per_bar == pytest.approx(3.5)


def test_spread_aware_buy_reference_is_detection_ask() -> None:
    tracker = EMAMovementJourneyTracker(
        point_size_by_symbol={"XAUUSD": 0.01},
    )
    journey = tracker.on_detection(_detection(direction=Direction.BUY, price=4160.0))
    assert journey is not None
    anchor = journey.anchors[0]

    assert anchor.reference_price == pytest.approx(4160.5)
    assert anchor.reference_price_kind == "ask_at_detection"
    assert anchor.outcome.spread_accounted is True


def test_data_gap_invalidates_anchor_so_it_cannot_become_training_data() -> None:
    tracker = EMAMovementJourneyTracker(max_horizon_bars=96, gap_tolerance_bars=2)
    journey = tracker.on_detection(_detection())
    assert journey is not None

    tracker.on_closed_candle(_candle(0, 4160.5, 4159.0, 4160.0))
    tracker.on_closed_candle(_candle(5, 4160.0, 4158.0, 4159.0))
    tracker.on_closed_candle(_candle(30, 4159.0, 4150.0, 4152.0))

    assert journey.status is JourneyStatus.INVALID
    assert journey.end_reason is JourneyEndReason.DATA_GAP
    assert journey.anchors[0].outcome.valid is False
    assert journey.anchors[0].outcome.invalid_reason == "data_gap"


def test_market_day_rollover_closes_cleanly_not_as_gap() -> None:
    tracker = EMAMovementJourneyTracker()
    journey = tracker.on_detection(_detection())
    assert journey is not None

    next_day = Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(BASE + timedelta(days=1), TZ),
        open=4150.0,
        high=4151.0,
        low=4149.0,
        close=4150.0,
        spread=50,
    )
    tracker.on_closed_candle(next_day)

    assert journey.status is JourneyStatus.CLOSED
    assert journey.end_reason is JourneyEndReason.MARKET_DAY_CHANGE
    assert journey.anchors[0].outcome.valid is True


def test_news_tagger_marks_only_nearby_high_impact_event() -> None:
    tagger = HighImpactNewsTagger(
        ((BASE + timedelta(minutes=20), "US CPI"),),
        window_minutes=30,
    )

    near = tagger.context_at(BASE)
    far = tagger.context_at(BASE - timedelta(hours=2))

    assert near.high_impact is True
    assert near.event == "US CPI"
    assert near.minutes_to_event == pytest.approx(20.0)
    assert far.high_impact is False


def test_incremental_ema_matches_full_history_batch_value() -> None:
    values = pd.Series([100.0 + i * 0.3 for i in range(100)], dtype="float64")
    stream = IncrementalEMA(20)
    latest = None
    for value in values:
        latest = stream.update(float(value))

    assert latest == pytest.approx(float(ema(values, 20).iloc[-1]))


def test_incremental_wilder_rsi_matches_full_history_batch_value() -> None:
    values = pd.Series(
        [100.0 + ((i % 7) - 3) * 0.5 + i * 0.05 for i in range(100)],
        dtype="float64",
    )
    stream = IncrementalWilderRSI(14)
    latest = None
    for value in values:
        latest = stream.update(float(value))

    assert latest == pytest.approx(float(rsi(values, 14).iloc[-1]), abs=1e-8)


def test_placement_report_uses_only_observed_anchors() -> None:
    outcome = EMAAnchorOutcome(
        bars_observed=4,
        mfe=12.0,
        mae=2.0,
        reference_price=4160.0,
        reference_price_kind="bid_at_detection",
        spread_accounted=True,
        targets={
            "3": TargetOutcome(reached=True, bars_to=1, adverse_before_reach=0.5),
            "5": TargetOutcome(reached=True, bars_to=2, adverse_before_reach=1.0),
            "10": TargetOutcome(reached=True, bars_to=4, adverse_before_reach=2.0),
        },
        completed=True,
        completed_at=BASE + timedelta(minutes=20),
        end_reason=JourneyEndReason.MAX_HORIZON,
    )
    anchor = EMAJourneyAnchor(
        detection_id="d-observed",
        anchor_type=EMAAnchorType.EMA20_50_CROSS,
        direction=Direction.SELL,
        detected_at=BASE,
        price=4160.0,
        reference_price=4160.0,
        reference_price_kind="bid_at_detection",
        movement_from_journey_start=4.0,
        features=_features(),
        outcome=outcome,
    )
    journey = EMAMovementJourney(
        journey_id="j-observed",
        account_scope="test",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction=Direction.SELL,
        market_date="2026-10-01",
        started_at=BASE,
        start_price=4164.0,
        anchors=(anchor,),
        status=JourneyStatus.CLOSED,
        ended_at=BASE + timedelta(minutes=20),
        end_reason=JourneyEndReason.MAX_HORIZON,
    )

    report = placement_report([journey])

    assert set(report["placements"]) == {"ema20_50_cross"}
    assert report["placements"]["ema20_50_cross"]["reach_10_rate"] == 1.0
    assert "No hypothetical fill" in report["note"]
