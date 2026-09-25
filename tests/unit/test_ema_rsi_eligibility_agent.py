"""Agent 20 EMA/RSI eligibility rule."""

from datetime import UTC, datetime

import pandas as pd

from aureon.agents import ema_rsi_eligibility_agent as module
from aureon.agents.ema_rsi_eligibility_agent import EmaRsiEligibilityAgent
from aureon.models.base import MarketTime
from aureon.models.detection import CandleContext, SessionContext
from aureon.models.enums import Direction, SessionName, Timeframe


def _ctx() -> CandleContext:
    opened = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    return CandleContext(
        account_scope="test",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        market_tz="Europe/Athens",
        closed_at=MarketTime.from_utc(opened.replace(minute=5), "Europe/Athens"),
        candle_open_time=MarketTime.from_utc(opened, "Europe/Athens"),
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
    )


def _frame(n: int = 160) -> pd.DataFrame:
    index = pd.date_range("2026-09-24", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "tick_volume": [1] * n,
            "real_volume": [0] * n,
        },
        index=index,
    )


def _patch(monkeypatch, *, cross: int, rsi_value: float) -> None:
    monkeypatch.setattr(module, "crossed_at_last", lambda fast, slow: cross)
    monkeypatch.setattr(
        module,
        "ema",
        lambda series, period: pd.Series(
            [99.0] * (len(series) - 2) + ([99.0, 101.0] if period == 20 else [100.0, 100.0]),
            index=series.index,
        ),
    )
    monkeypatch.setattr(
        module,
        "rsi",
        lambda series, period: pd.Series([rsi_value] * len(series), index=series.index),
    )


def test_bullish_cross_below_60_is_long_eligible(monkeypatch) -> None:
    _patch(monkeypatch, cross=1, rsi_value=59.9)
    agent = EmaRsiEligibilityAgent(fast_period=20, slow_period=50)
    detection = agent.on_closed_candle(_frame(), _ctx())[0]
    assert detection.event_key == "long_eligible"
    assert detection.direction is Direction.BUY
    assert detection.evidence.flags["eligible"] is True


def test_bullish_cross_at_60_is_not_eligible(monkeypatch) -> None:
    _patch(monkeypatch, cross=1, rsi_value=60.0)
    agent = EmaRsiEligibilityAgent(fast_period=20, slow_period=50)
    detection = agent.on_closed_candle(_frame(), _ctx())[0]
    assert detection.event_key == "long_ineligible_rsi"
    assert detection.direction is None


def test_bearish_cross_above_60_is_short_eligible(monkeypatch) -> None:
    _patch(monkeypatch, cross=-1, rsi_value=60.1)
    agent = EmaRsiEligibilityAgent(fast_period=20, slow_period=50)
    detection = agent.on_closed_candle(_frame(), _ctx())[0]
    assert detection.event_key == "short_eligible"
    assert detection.direction is Direction.SELL


def test_bearish_cross_below_60_is_not_eligible(monkeypatch) -> None:
    _patch(monkeypatch, cross=-1, rsi_value=55.0)
    agent = EmaRsiEligibilityAgent(fast_period=20, slow_period=50)
    detection = agent.on_closed_candle(_frame(), _ctx())[0]
    assert detection.event_key == "short_ineligible_rsi"
    assert detection.direction is None
