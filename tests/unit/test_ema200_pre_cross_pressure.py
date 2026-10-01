"""EMA200 pre-cross pressure behavior."""
from __future__ import annotations

import pandas as pd

from aureon.agents.ema200_pre_cross_agent import Ema200PreCrossPressureAgent
from aureon.discord.service import build_notification
from aureon.models.base import MarketTime
from aureon.models.detection import CandleContext, SessionContext
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.settings import NotificationSettings

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


def _bearish_pressure_frame() -> pd.DataFrame:
    bars = 620
    index = pd.date_range("2026-09-30", periods=bars, freq="5min", tz="UTC")
    close = [2400.0] * bars
    # A strong push away from EMA200 followed by a sharp rollover. The last
    # closed candle is still above EMA200, so this is pressure, not the cross.
    path = [2401, 2402, 2403, 2404, 2405, 2406, 2407, 2408, 2409, 2410, 2411, 2405]
    close[-len(path):] = path
    open_values = close.copy()
    for i in range(1, bars):
        open_values[i] = close[i - 1]
    return pd.DataFrame(
        {
            "open": open_values,
            "high": [value + 0.6 for value in close],
            "low": [value - 0.6 for value in close],
            "close": close,
            "tick_volume": [100.0] * bars,
            "real_volume": [0.0] * bars,
        },
        index=index,
    )


def test_bearish_pressure_emits_before_ema200_cross() -> None:
    frame = _bearish_pressure_frame()
    agent = Ema200PreCrossPressureAgent(
        max_distance_atr=10.0,
        min_supporting_conditions=4,
        rearm_bars=1,
    )

    detections = agent.on_closed_candle(frame, _ctx(frame))

    assert len(detections) == 1
    detection = detections[0]
    assert detection.direction is Direction.SELL
    assert detection.event_key == "bearish_pressure"
    assert detection.price > detection.indicators.ema["ema200"]
    assert detection.evidence.flags["distance_closing"] is True
    assert detection.evidence.numeric["supporting_conditions"] >= 4


def test_pressure_card_is_warning_context_not_execution_prefill() -> None:
    frame = _bearish_pressure_frame()
    agent = Ema200PreCrossPressureAgent(
        max_distance_atr=10.0,
        min_supporting_conditions=4,
        rearm_bars=1,
    )
    detection = agent.on_closed_candle(frame, _ctx(frame))[0]

    card = build_notification(detection)
    fields = dict(card.fields)

    assert "PRE-CROSS PRESSURE" in card.title
    assert fields["Status"] == "EMA200 cross NOT confirmed"
    assert fields["Bias"] == "BEARISH"
    assert card.side is None


def test_signal_first_mode_announces_pre_cross_pressure() -> None:
    settings = NotificationSettings()
    assert settings.announces("ema200_pre_cross") is True



def test_pre_cross_agent_stays_silent_on_actual_cross_candle() -> None:
    frame = _bearish_pressure_frame()
    frame.iloc[-1, frame.columns.get_loc("close")] = 2398.0
    frame.iloc[-1, frame.columns.get_loc("low")] = 2397.5
    agent = Ema200PreCrossPressureAgent(
        max_distance_atr=10.0,
        min_supporting_conditions=3,
        rearm_bars=1,
    )

    assert agent.on_closed_candle(frame, _ctx(frame)) == []
