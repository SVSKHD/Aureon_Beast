"""Phase-1 EMA sequence labeler tests."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aureon.models.base import MarketTime
from aureon.models.detection import AgentEvidence, Detection, IndicatorSnapshot, SessionContext
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.services.ema_sequence_labeler import EMASequenceConfig, EMASequenceLabeler


def _cross(direction: Direction = Direction.BUY) -> Detection:
    at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    return Detection(
        detection_id="x",
        agent_name="ema_cross",
        agent_version="2.3.0",
        account_scope="test",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        detected_at=MarketTime.from_utc(at, "Europe/Athens"),
        candle_open_time=MarketTime.from_utc(at - timedelta(minutes=5), "Europe/Athens"),
        sequence_today=1,
        sequence_session=1,
        event_key="bullish" if direction is Direction.BUY else "bearish",
        price=100.0,
        direction=direction,
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        indicators=IndicatorSnapshot(ema={"fast": 101.0, "slow": 100.0}, rsi=55.0),
        evidence=AgentEvidence(
            numeric={
                "ema_gap": 1.0,
                "ema_gap_change": 0.2,
                "fast_slope": 0.3,
                "slow_slope": 0.1,
            },
            categorical={"cross_direction": "bullish" if direction is Direction.BUY else "bearish"},
            flags={},
        ),
    )


def _bar(i: int, high: float, low: float, close: float = 100.0):
    at = datetime(2026, 1, 5, 10, 5, tzinfo=UTC) + timedelta(minutes=5 * i)
    return SimpleNamespace(
        high=high,
        low=low,
        close=close,
        open_time=SimpleNamespace(utc=at),
    )


def test_snapshot_contains_only_frozen_context() -> None:
    labeler = EMASequenceLabeler(EMASequenceConfig(horizon_bars=2))
    future = [_bar(0, 103, 99), _bar(1, 107, 100)]
    record = labeler.label(
        cross_detection=_cross(),
        frozen_context={"session": "london", "market_regime": "trend"},
        future_candles=future,
    )
    assert record.snapshot.session == "london"
    assert record.snapshot.market_regime == "trend"
    assert record.snapshot.cross_price == 100.0
    assert "future" not in record.snapshot.context


def test_immediate_continuation_is_labeled() -> None:
    labeler = EMASequenceLabeler(
        EMASequenceConfig(horizon_bars=3, continuation_move=6, pullback_min_move=2)
    )
    record = labeler.label(
        cross_detection=_cross(),
        frozen_context={},
        future_candles=[_bar(0, 107, 99.5), _bar(1, 109, 100), _bar(2, 111, 100)],
    )
    assert record.label.outcome.value == "IMMEDIATE_CONTINUATION"
    assert record.label.continuation.reached_6


def test_pullback_then_continuation_is_labeled() -> None:
    labeler = EMASequenceLabeler(
        EMASequenceConfig(
            horizon_bars=4,
            continuation_move=6,
            pullback_min_move=2,
            deep_pullback_move=6,
            failure_move=8,
        )
    )
    record = labeler.label(
        cross_detection=_cross(),
        frozen_context={},
        future_candles=[
            _bar(0, 101, 97),
            _bar(1, 102, 96),
            _bar(2, 105, 98),
            _bar(3, 108, 100),
        ],
    )
    assert record.label.outcome.value == "PULLBACK_THEN_CONTINUATION"
    assert record.label.pullback_move == 4.0


def test_deep_pullback_then_continuation_is_labeled() -> None:
    labeler = EMASequenceLabeler(
        EMASequenceConfig(
            horizon_bars=3,
            continuation_move=6,
            pullback_min_move=2,
            deep_pullback_move=6,
            failure_move=10,
        )
    )
    record = labeler.label(
        cross_detection=_cross(),
        frozen_context={},
        future_candles=[_bar(0, 101, 93), _bar(1, 103, 95), _bar(2, 108, 99)],
    )
    assert record.label.outcome.value == "DEEP_PULLBACK_THEN_CONTINUATION"


def test_failed_direction_is_labeled() -> None:
    labeler = EMASequenceLabeler(
        EMASequenceConfig(
            horizon_bars=3,
            continuation_move=6,
            pullback_min_move=2,
            failure_move=6,
        )
    )
    record = labeler.label(
        cross_detection=_cross(),
        frozen_context={},
        future_candles=[_bar(0, 101, 97), _bar(1, 100, 93), _bar(2, 99, 92)],
    )
    assert record.label.outcome.value == "FAILED_DIRECTION"


def test_same_m5_bar_both_sides_is_ambiguous() -> None:
    labeler = EMASequenceLabeler(
        EMASequenceConfig(horizon_bars=1, continuation_move=6, pullback_min_move=2)
    )
    record = labeler.label(
        cross_detection=_cross(),
        frozen_context={},
        future_candles=[_bar(0, 107, 97)],
    )
    assert record.label.outcome.value == "AMBIGUOUS_PATH"
    assert record.label.path_ambiguous


def test_requires_full_future_horizon() -> None:
    labeler = EMASequenceLabeler(EMASequenceConfig(horizon_bars=2))
    with pytest.raises(ValueError, match="full configured future horizon"):
        labeler.label(
            cross_detection=_cross(),
            frozen_context={},
            future_candles=[_bar(0, 101, 99)],
        )


def test_rejects_non_cross_detection() -> None:
    bad = _cross().model_copy(update={"agent_name": "ema_rsi_eligibility"})
    labeler = EMASequenceLabeler(EMASequenceConfig(horizon_bars=1))
    with pytest.raises(ValueError, match="ema_cross"):
        labeler.label(
            cross_detection=bad,
            frozen_context={},
            future_candles=[_bar(0, 101, 99)],
        )
