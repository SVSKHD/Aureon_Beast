"""Aureon V2 EMA cross foundations for TODO-001 through TODO-010."""
from __future__ import annotations

import pandas as pd

from aureon.agents.ema200_cross_agent import Ema200CrossAgent
from aureon.models.base import MarketTime
from aureon.models.detection import CandleContext, SessionContext
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.services.cross_analysis import cross_candle_quality, simple_trend_direction

MARKET_TZ = "Europe/Athens"


def _ctx(frame: pd.DataFrame) -> CandleContext:
    opened = frame.index[-1].to_pydatetime()
    return CandleContext(
        account_scope="test",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        market_tz=MARKET_TZ,
        closed_at=MarketTime.from_utc(opened + pd.Timedelta(minutes=5), MARKET_TZ),
        candle_open_time=MarketTime.from_utc(opened, MARKET_TZ),
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
    )


def _frame(bars: int = 610, base: float = 2400.0) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=bars, freq="5min", tz="UTC")
    values = [base] * bars
    return pd.DataFrame(
        {
            "open": values.copy(),
            "high": [base + 0.5] * bars,
            "low": [base - 0.5] * bars,
            "close": values.copy(),
            "tick_volume": [100.0] * bars,
            "real_volume": [0.0] * bars,
        },
        index=index,
    )


def test_ema200_emits_once_when_price_crosses_above() -> None:
    frame = _frame()
    frame.iloc[-2, frame.columns.get_loc("close")] = 2399.0
    frame.iloc[-2, frame.columns.get_loc("open")] = 2399.2
    frame.iloc[-1, frame.columns.get_loc("open")] = 2399.2
    frame.iloc[-1, frame.columns.get_loc("high")] = 2401.5
    frame.iloc[-1, frame.columns.get_loc("low")] = 2399.0
    frame.iloc[-1, frame.columns.get_loc("close")] = 2401.0

    detections = Ema200CrossAgent().on_closed_candle(frame, _ctx(frame))

    assert len(detections) == 1
    assert detections[0].direction is Direction.BUY
    assert detections[0].event_key == "bullish"
    assert detections[0].evidence.categorical["price_relation"] == "above_ema200"


def test_ema200_does_not_repeat_while_price_stays_above() -> None:
    frame = _frame()
    frame.iloc[-2, frame.columns.get_loc("close")] = 2401.0
    frame.iloc[-1, frame.columns.get_loc("close")] = 2401.5

    assert Ema200CrossAgent().on_closed_candle(frame, _ctx(frame)) == []


def test_ema200_emits_bearish_cross() -> None:
    frame = _frame()
    frame.iloc[-2, frame.columns.get_loc("close")] = 2401.0
    frame.iloc[-2, frame.columns.get_loc("open")] = 2400.8
    frame.iloc[-1, frame.columns.get_loc("open")] = 2400.8
    frame.iloc[-1, frame.columns.get_loc("high")] = 2401.0
    frame.iloc[-1, frame.columns.get_loc("low")] = 2398.5
    frame.iloc[-1, frame.columns.get_loc("close")] = 2399.0

    detections = Ema200CrossAgent().on_closed_candle(frame, _ctx(frame))
    assert len(detections) == 1
    assert detections[0].direction is Direction.SELL
    assert detections[0].event_key == "bearish"


def test_simple_trend_direction_is_up_for_consistent_rise() -> None:
    frame = _frame(100)
    for i in range(len(frame)):
        price = 2400.0 + i * 0.10
        frame.iloc[i, frame.columns.get_loc("open")] = price - 0.05
        frame.iloc[i, frame.columns.get_loc("high")] = price + 0.20
        frame.iloc[i, frame.columns.get_loc("low")] = price - 0.20
        frame.iloc[i, frame.columns.get_loc("close")] = price

    assert simple_trend_direction(frame) == "UP"


def test_cross_candle_quality_detects_clean_cross() -> None:
    frame = _frame(30)
    frame.iloc[-1, frame.columns.get_loc("open")] = 2400.0
    frame.iloc[-1, frame.columns.get_loc("low")] = 2399.9
    frame.iloc[-1, frame.columns.get_loc("high")] = 2402.2
    frame.iloc[-1, frame.columns.get_loc("close")] = 2402.0

    result = cross_candle_quality(
        frame,
        reference_value=2400.5,
        direction="bullish",
    )

    assert result["quality"] in {"STRONG", "NORMAL"}
    assert result["clean_close"] is True
