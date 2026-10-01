"""Signal-first Discord notification policy tests."""
from __future__ import annotations

import pandas as pd

from aureon.agents.ema200_cross_agent import Ema200CrossAgent
from aureon.discord.service import build_notification, should_notify
from aureon.models.base import MarketTime
from aureon.models.detection import AgentEvidence, CandleContext, SessionContext
from aureon.models.enums import SessionName, Timeframe
from aureon.models.settings import NotificationSettings

MARKET_TZ = "Europe/Athens"


def _frame() -> pd.DataFrame:
    base = 2400.0
    index = pd.date_range("2026-01-01", periods=610, freq="5min", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": [base] * len(index),
            "high": [base + 0.5] * len(index),
            "low": [base - 0.5] * len(index),
            "close": [base] * len(index),
            "tick_volume": [100.0] * len(index),
            "real_volume": [0.0] * len(index),
        },
        index=index,
    )
    frame.iloc[-2, frame.columns.get_loc("close")] = 2399.0
    frame.iloc[-1, frame.columns.get_loc("open")] = 2399.2
    frame.iloc[-1, frame.columns.get_loc("high")] = 2401.5
    frame.iloc[-1, frame.columns.get_loc("low")] = 2399.0
    frame.iloc[-1, frame.columns.get_loc("close")] = 2401.0
    return frame


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


def _ema_detection():
    frame = _frame()
    return Ema200CrossAgent().on_closed_candle(frame, _ctx(frame))[0]


def test_signal_first_mode_announces_only_ema_and_session_summary() -> None:
    settings = NotificationSettings(
        enabled_kinds=(
            "ema_cross",
            "ema200_cross",
            "wick",
            "liquidity",
            "breakout",
            "session_trend",
        )
    )

    assert settings.announces("ema_cross") is True
    assert settings.announces("ema200_cross") is True
    assert settings.announces("session_trend") is True
    assert settings.announces("wick") is False
    assert settings.announces("liquidity") is False
    assert settings.announces("breakout") is False


def test_signal_first_mode_suppresses_setup_cards() -> None:
    settings = NotificationSettings()
    assert settings.announces_setup("confirmed") is False
    assert settings.announces_setup("breakout_pressure") is False


def test_legacy_mode_still_respects_configured_detection_kinds() -> None:
    settings = NotificationSettings(
        signal_first_mode=False,
        enabled_kinds=("wick",),
    )
    detection = _ema_detection().model_copy(update={"agent_name": "wick"})

    assert should_notify(detection, settings) is True


def test_completed_session_card_is_compact_and_contains_ema_context() -> None:
    trigger = _ema_detection()
    session_detection = trigger.model_copy(
        update={
            "agent_name": "session_trend",
            "direction": None,
            "event_key": "london|up",
            "evidence": AgentEvidence(
                numeric={
                    "open": 4170.0,
                    "high": 4192.0,
                    "low": 4166.0,
                    "close": 4188.0,
                    "change": 18.0,
                    "change_points": 1800.0,
                    "range": 26.0,
                    "range_points": 2600.0,
                    "candle_count": 96.0,
                },
                categorical={
                    "session_event": "close",
                    "completed_session": "london",
                    "next_session": "new_york",
                    "trend": "up",
                },
            ),
        }
    )

    card = build_notification(
        session_detection,
        session_ema_context=(
            "BULLISH · UP · STRONG · CONSOLIDATION · 15:25",
            "BULLISH · UP · NORMAL · TRENDING · 14:40",
        ),
    )
    fields = dict(card.fields)

    assert card.title == "XAUUSD · LONDON SESSION CLOSED"
    assert fields["Trend / Next"] == "UP · NEW_YORK"
    assert "BULLISH" in fields["Latest EMA20 / EMA50"]
    assert "BULLISH" in fields["Latest Price / EMA200"]
    assert len(card.fields) <= 9


def test_session_open_card_is_compact_and_contains_active_ema_context() -> None:
    trigger = _ema_detection()
    session_detection = trigger.model_copy(
        update={
            "agent_name": "session_trend",
            "direction": None,
            "event_key": "open|london",
            "evidence": AgentEvidence(
                numeric={
                    "open": 4188.0,
                    "previous_close": 4187.5,
                    "previous_change": 12.0,
                    "previous_range": 31.0,
                },
                categorical={
                    "session_event": "open",
                    "opened_session": "london",
                    "previous_session": "asia",
                    "previous_trend": "up",
                },
            ),
        }
    )

    card = build_notification(
        session_detection,
        session_ema_context=(
            "BULLISH · UP · STRONG · CONSOLIDATION · 09:55",
            "BULLISH · UP · NORMAL · TRENDING · 09:40",
        ),
    )
    fields = dict(card.fields)

    assert card.title == "XAUUSD · LONDON SESSION OPENED"
    assert fields["Open"] == "4188.00"
    assert "ASIA · UP" in fields["Previous session"]
    assert "BULLISH" in fields["Active EMA20 / EMA50"]
    assert "BULLISH" in fields["Active Price / EMA200"]
    assert len(card.fields) <= 7



def test_session_cards_can_add_confidence_rows_without_redesign() -> None:
    trigger = _ema_detection()
    session_detection = trigger.model_copy(
        update={
            "agent_name": "session_trend",
            "direction": None,
            "event_key": "open|london",
            "evidence": AgentEvidence(
                numeric={"open": 4188.0, "previous_close": 4187.5},
                categorical={
                    "session_event": "open",
                    "opened_session": "london",
                    "previous_session": "asia",
                    "previous_trend": "up",
                },
            ),
        }
    )

    card = build_notification(
        session_detection,
        session_ema_context=("BULLISH · UP · STRONG · 09:55", "—"),
        session_agent_confidence="HIGH · 4 supportive",
        session_model_confidence="P(+10) 64% (n=212)",
    )
    fields = dict(card.fields)

    assert fields["Agent confidence"] == "HIGH · 4 supportive"
    assert fields["Model confidence"] == "P(+10) 64% (n=212)"

