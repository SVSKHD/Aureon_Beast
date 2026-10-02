"""Acceptance coverage for V4 Aureon Gold TODO 001-020."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aureon.models.base import MarketTime
from aureon.models.detection import Detection, SessionContext
from aureon.models.ema_journey_v4 import (
    PreCrossFailureType,
    V4JourneyPhase,
)
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.market import Candle
from aureon.services.ema_journey_tracker import EMAMovementJourneyTracker
from aureon.services.v4_ema_journey import (
    build_v4_journey_snapshot,
    pre_cross_example,
    remaining_movement_examples,
)
from aureon.services.v4_ema_model import (
    fit_pre_cross_model,
    fit_remaining_move_model,
    predict_pre_cross,
    predict_remaining_move,
)

TZ = "Europe/Athens"
BASE = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def _detection(
    *,
    agent: str,
    direction: Direction,
    price: float,
    minutes: int,
    suffix: str,
) -> Detection:
    close_at = BASE + timedelta(minutes=minutes)
    return Detection(
        detection_id=f"v4-{suffix}",
        account_scope="test",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        agent_name=agent,
        agent_version="test",
        event_key="bullish" if direction is Direction.BUY else "bearish",
        direction=direction,
        detected_at=MarketTime.from_utc(close_at, TZ),
        candle_open_time=MarketTime.from_utc(
            close_at - timedelta(minutes=5),
            TZ,
        ),
        price=price,
        session=SessionContext(
            session=SessionName.LONDON,
            session_config_version=1,
        ),
        sequence_today=1,
        sequence_session=1,
    )


def _candle(
    *,
    minutes: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> Candle:
    return Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(BASE + timedelta(minutes=minutes), TZ),
        open=open_,
        high=high,
        low=low,
        close=close,
    )


def _completed_journey():
    tracker = EMAMovementJourneyTracker(max_horizon_bars=20)
    tracker.on_detection(
        _detection(
            agent="ema200_pre_cross",
            direction=Direction.BUY,
            price=4100.0,
            minutes=0,
            suffix="pre",
        )
    )
    tracker.on_detection(
        _detection(
            agent="ema_cross",
            direction=Direction.BUY,
            price=4107.0,
            minutes=15,
            suffix="20-50",
        )
    )
    journey = tracker.on_detection(
        _detection(
            agent="ema200_cross",
            direction=Direction.BUY,
            price=4110.0,
            minutes=25,
            suffix="200",
        )
    )
    assert journey is not None

    tracker.on_closed_candle(
        _candle(
            minutes=30,
            open_=4110.0,
            high=4116.5,
            low=4108.5,
            close=4115.0,
        )
    )
    tracker.on_closed_candle(
        _candle(
            minutes=35,
            open_=4115.0,
            high=4121.0,
            low=4114.0,
            close=4120.0,
        )
    )
    tracker.on_detection(
        _detection(
            agent="ema_cross",
            direction=Direction.SELL,
            price=4118.0,
            minutes=45,
            suffix="opposite",
        )
    )
    return journey


def test_v4_snapshot_tracks_pre_cross_and_cross_order() -> None:
    journey = _completed_journey()
    snapshot = build_v4_journey_snapshot(journey)

    assert snapshot.phase is V4JourneyPhase.CLOSED
    assert snapshot.pre_cross_movement == 7.0
    assert snapshot.pre_cross_bars == 3
    assert snapshot.pre_cross_seconds == 900.0
    assert snapshot.cross_order == "ema20_50_cross->ema200_cross"
    assert snapshot.movement_consumed_at_latest_cross == 10.0
    assert [event.event_type for event in snapshot.event_sequence] == [
        "pre_cross",
        "ema20_50_cross",
        "ema200_cross",
    ]


def test_v4_pre_cross_labels_cross_windows() -> None:
    example = pre_cross_example(_completed_journey())
    assert example is not None
    assert example.labels.bars_to_cross == 3
    assert example.labels.cross_within_1 is False
    assert example.labels.cross_within_3 is True
    assert example.labels.cross_within_6 is True
    assert example.labels.cross_within_12 is True
    assert example.labels.failure_type is PreCrossFailureType.NONE


def test_v4_failed_pressure_is_retained() -> None:
    tracker = EMAMovementJourneyTracker(max_horizon_bars=2)
    journey = tracker.on_detection(
        _detection(
            agent="ema200_pre_cross",
            direction=Direction.SELL,
            price=4200.0,
            minutes=0,
            suffix="failed-pre",
        )
    )
    assert journey is not None
    tracker.on_closed_candle(
        _candle(
            minutes=5,
            open_=4200.0,
            high=4201.0,
            low=4199.0,
            close=4200.0,
        )
    )
    tracker.on_closed_candle(
        _candle(
            minutes=10,
            open_=4200.0,
            high=4201.0,
            low=4199.0,
            close=4200.0,
        )
    )
    example = pre_cross_example(journey)
    assert example is not None
    assert example.labels.bars_to_cross is None
    assert example.labels.failure_type is PreCrossFailureType.EXPIRED


def test_v4_remaining_move_is_measured_from_each_cross() -> None:
    examples = remaining_movement_examples(_completed_journey())
    assert len(examples) == 2

    ema20_50, ema200 = examples
    assert ema20_50.features.movement_consumed == 7.0
    assert ema200.features.movement_consumed == 10.0
    assert ema20_50.labels.remaining_mfe >= ema200.labels.remaining_mfe
    assert ema200.labels.reached_5 is True
    assert ema200.labels.reached_10 is True


def test_v4_models_produce_monotonic_probabilities_and_remaining_movement() -> None:
    journey = _completed_journey()
    pre = pre_cross_example(journey)
    remaining = remaining_movement_examples(journey)
    assert pre is not None
    assert remaining

    pre_rows = [pre.model_copy(deep=True) for _ in range(20)]
    pre_bundle = fit_pre_cross_model(pre_rows)
    pre_prediction = predict_pre_cross(pre_bundle, pre.features)
    assert (
        pre_prediction["cross_within_1"]
        <= pre_prediction["cross_within_3"]
        <= pre_prediction["cross_within_6"]
        <= pre_prediction["cross_within_12"]
    )

    remaining_rows = [
        remaining[index % len(remaining)].model_copy(deep=True)
        for index in range(20)
    ]
    remaining_bundle = fit_remaining_move_model(remaining_rows)
    prediction = predict_remaining_move(
        remaining_bundle,
        remaining[0].features,
    )
    assert (
        prediction["reached_3"]
        >= prediction["reached_5"]
        >= prediction["reached_10"]
        >= prediction["reached_20"]
        >= prediction["reached_30"]
        >= prediction["reached_40"]
    )
    assert prediction["expected_remaining_mfe"] >= 0.0
    assert prediction["expected_remaining_mae"] >= 0.0
