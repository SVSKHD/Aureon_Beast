"""Agent consensus meter tests for EMA cross notifications."""
from __future__ import annotations

import pandas as pd

from aureon.agents.ema200_cross_agent import Ema200CrossAgent
from aureon.discord.service import build_notification
from aureon.models.base import MarketTime
from aureon.models.detection import CandleContext, SessionContext
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.services.agent_consensus import build_agent_consensus

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


def _peer(trigger, *, agent: str, direction: Direction | None, suffix: str):
    return trigger.model_copy(
        update={
            "detection_id": f"{trigger.detection_id}-{suffix}",
            "agent_name": agent,
            "direction": direction,
            "event_key": suffix,
        }
    )


def test_consensus_counts_same_candle_agent_alignment() -> None:
    frame = _frame()
    trigger = Ema200CrossAgent().on_closed_candle(frame, _ctx(frame))[0]
    peers = [
        _peer(trigger, agent="wick", direction=Direction.BUY, suffix="wick"),
        _peer(trigger, agent="breakout", direction=Direction.BUY, suffix="breakout"),
        _peer(trigger, agent="liquidity", direction=Direction.BUY, suffix="liquidity"),
        _peer(trigger, agent="volume_participation", direction=None, suffix="volume"),
        _peer(trigger, agent="session_trend", direction=Direction.SELL, suffix="session"),
    ]

    consensus = build_agent_consensus(trigger, peers)

    assert consensus.supportive == 3
    assert consensus.neutral == 1
    assert consensus.conflicting == 1
    assert consensus.coverage == 5
    assert consensus.label == "HIGH"


def test_consensus_ignores_other_candle_and_other_timeframe() -> None:
    frame = _frame()
    trigger = Ema200CrossAgent().on_closed_candle(frame, _ctx(frame))[0]
    other_timeframe = _peer(
        trigger,
        agent="wick",
        direction=Direction.BUY,
        suffix="wrong-timeframe",
    ).model_copy(update={"timeframe": Timeframe.H1})
    other_candle = _peer(
        trigger,
        agent="breakout",
        direction=Direction.BUY,
        suffix="wrong-candle",
    ).model_copy(
        update={
            "detected_at": MarketTime.from_utc(
                trigger.detected_at.utc + pd.Timedelta(minutes=5),
                MARKET_TZ,
            )
        }
    )

    consensus = build_agent_consensus(trigger, [other_timeframe, other_candle])

    assert consensus.coverage == 0
    assert consensus.label == "INSUFFICIENT"


def test_ema_card_renders_consensus_meter_without_probability() -> None:
    frame = _frame()
    trigger = Ema200CrossAgent().on_closed_candle(frame, _ctx(frame))[0]
    peers = [
        _peer(trigger, agent="wick", direction=Direction.BUY, suffix="wick"),
        _peer(trigger, agent="breakout", direction=Direction.BUY, suffix="breakout"),
        _peer(trigger, agent="liquidity", direction=Direction.BUY, suffix="liquidity"),
    ]
    consensus = build_agent_consensus(trigger, peers)

    fields = dict(build_notification(trigger, consensus=consensus).fields)
    rendered = fields["Agent consensus"]

    assert "HIGH" in rendered
    assert "3 supportive" in rendered
    assert "not a win probability" in rendered
    assert "%" not in rendered
