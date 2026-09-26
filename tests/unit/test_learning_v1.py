"""Aureon V1 learning-contract tests."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from aureon.models.enums import Direction
from aureon.models.learning_v1 import AgentFeatureState, FeatureSnapshotV1
from aureon.services.learning_contract import outcome_from_future_candles


def _bar(index: int, *, high: float, low: float, close: float = 100.0):
    opened = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * index)
    return SimpleNamespace(
        high=high, low=low, close=close,
        open_time=SimpleNamespace(utc=opened),
        close_time=SimpleNamespace(utc=opened + timedelta(minutes=5)),
    )


def test_clean_10_buy_at_mae_seven_boundary_is_clean() -> None:
    outcome = outcome_from_future_candles(
        candles=[_bar(0, high=105.0, low=93.0), _bar(1, high=110.0, low=96.0)],
        entry_price=100.0, direction=Direction.BUY, horizon_bars=2,
        clean_target=10.0, clean_max_mae=7.0,
    )
    assert outcome.reached_10 is True
    assert outcome.mae_before_10 == 7.0
    assert outcome.clean_10 is True


def test_clean_10_buy_above_mae_boundary_is_not_clean() -> None:
    outcome = outcome_from_future_candles(
        candles=[_bar(0, high=104.0, low=92.9), _bar(1, high=111.0, low=99.0)],
        entry_price=100.0, direction=Direction.BUY, horizon_bars=2,
        clean_target=10.0, clean_max_mae=7.0,
    )
    assert outcome.reached_10 is True
    assert outcome.mae_before_10 > 7.0
    assert outcome.clean_10 is False


def test_clean_10_sell_calculation() -> None:
    outcome = outcome_from_future_candles(
        candles=[_bar(0, high=104.0, low=96.0), _bar(1, high=103.0, low=89.5)],
        entry_price=100.0, direction=Direction.SELL, horizon_bars=2,
        clean_target=10.0, clean_max_mae=7.0,
    )
    assert outcome.reached_10 is True
    assert outcome.mae_before_10 == 4.0
    assert outcome.clean_10 is True


def test_same_bar_target_and_mae_breach_is_conservatively_ambiguous() -> None:
    outcome = outcome_from_future_candles(
        candles=[_bar(0, high=111.0, low=92.0)],
        entry_price=100.0, direction=Direction.BUY, horizon_bars=1,
        clean_target=10.0, clean_max_mae=7.0,
    )
    assert outcome.path_ambiguous is True
    assert outcome.clean_10 is False


def test_multitarget_ladder_is_nested_by_price_path() -> None:
    outcome = outcome_from_future_candles(
        candles=[_bar(0, high=123.0, low=99.0)],
        entry_price=100.0, direction=Direction.BUY, horizon_bars=1,
    )
    assert outcome.reached_5
    assert outcome.reached_10
    assert outcome.reached_20
    assert not outcome.reached_30
    assert not outcome.reached_40


def test_feature_snapshot_is_immutable() -> None:
    snapshot = FeatureSnapshotV1(
        setup_id="s1", symbol="XAUUSD", timeframe="M5",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        frozen_at=datetime(2026, 1, 1, tzinfo=UTC),
        direction=Direction.BUY, reference_price=100.0,
        agents={"ema_cross": AgentFeatureState(stance="buy")},
    )
    with pytest.raises(ValidationError):
        snapshot.reference_price = 101.0
