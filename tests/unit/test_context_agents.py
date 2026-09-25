"""Agents 9-11: journey, regime and participation context."""

from __future__ import annotations

import pandas as pd

from aureon.agents.market_journey_agent import MarketJourneyAgent, market_journey_snapshot
from aureon.agents.market_regime_agent import MarketRegimeAgent, classify_market_regime
from aureon.agents.volume_participation_agent import (
    VolumeParticipationAgent,
    participation_snapshot,
)
from aureon.models.base import MarketTime
from aureon.models.detection import CandleContext, SessionContext
from aureon.models.enums import SessionName, Timeframe
from main_observer import default_agents
from aureon.config import AureonConfig
from aureon.discord.service import build_live_panel
from aureon.models.system import SymbolState
from aureon.services.market_snapshot import MarketSnapshot

MARKET_TZ = "Europe/Athens"


def _ctx(frame: pd.DataFrame) -> CandleContext:
    opened = frame.index[-1].to_pydatetime()
    stamp = MarketTime.from_utc(opened, MARKET_TZ)
    return CandleContext(
        account_scope="test",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        market_tz=MARKET_TZ,
        closed_at=MarketTime.from_utc(opened + pd.Timedelta(minutes=5), MARKET_TZ),
        candle_open_time=stamp,
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
    )


def _frame(
    bars: int,
    *,
    start: str = "2026-09-20 00:00:00+00:00",
    base: float = 2400.0,
) -> pd.DataFrame:
    index = pd.date_range(start, periods=bars, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [base] * bars,
            "high": [base + 1.0] * bars,
            "low": [base - 1.0] * bars,
            "close": [base] * bars,
            "tick_volume": [100.0] * bars,
            "real_volume": [0.0] * bars,
        },
        index=index,
    )


def test_market_journey_joins_asia_previous_day_and_recent_days() -> None:
    frame = _frame(1200)
    frame.iloc[-1, frame.columns.get_loc("high")] = 2411.0
    frame.iloc[-1, frame.columns.get_loc("close")] = 2410.0

    snapshot = market_journey_snapshot(
        frame, _ctx(frame), point=0.01, recent_days=3, near_level_points=300.0
    )

    assert snapshot is not None
    assert snapshot["asia_open"] is not None
    assert snapshot["previous_day_high"] is not None
    assert snapshot["previous_day_close"] is not None
    assert snapshot["recent_high"] is not None
    assert snapshot["previous_day_location"] == "above_previous_day_high"
    assert snapshot["above_previous_close"] is True


def test_market_journey_detection_is_context_only_and_training_ready() -> None:
    frame = _frame(1200)
    frame.iloc[-1, frame.columns.get_loc("high")] = 2411.0
    frame.iloc[-1, frame.columns.get_loc("close")] = 2410.0

    agent = MarketJourneyAgent(point=0.01, lookback_bars=1200)
    detections = agent.on_closed_candle(frame, _ctx(frame))

    assert len(detections) == 1
    detection = detections[0]
    assert detection.direction is None
    assert detection.evidence.numeric
    assert detection.evidence.categorical["journey_state"]
    assert "previous_day_high" in detection.levels


def test_regime_classifier_distinguishes_trend_from_chop() -> None:
    trend = _frame(130)
    for i in range(len(trend)):
        close = 2400.0 + i * 0.20
        trend.iloc[i, trend.columns.get_loc("open")] = close - 0.10
        trend.iloc[i, trend.columns.get_loc("high")] = close + 0.30
        trend.iloc[i, trend.columns.get_loc("low")] = close - 0.30
        trend.iloc[i, trend.columns.get_loc("close")] = close

    result = classify_market_regime(
        trend,
        point=0.01,
        baseline_bars=96,
        short_bars=24,
        trend_bars=48,
        compression_ratio=0.75,
        expansion_ratio=1.25,
        trend_efficiency=0.50,
        range_efficiency=0.30,
        messy_reversal_rate=0.55,
    )

    assert result is not None
    assert result["state"] in {"trending", "trend_expansion"}
    assert result["structure_state"] == "trend"
    assert float(result["path_efficiency"]) >= 0.50


def test_market_regime_agent_is_non_directional() -> None:
    agent = MarketRegimeAgent(point=0.01)
    assert agent.agent_name == "market_regime"
    assert agent.params_snapshot()["warmup_bars"] == agent.min_window()


def test_participation_uses_tick_volume_when_real_volume_is_empty() -> None:
    frame = _frame(110)
    frame.iloc[-1, frame.columns.get_loc("tick_volume")] = 400.0
    frame.iloc[-1, frame.columns.get_loc("high")] = 2403.0
    frame.iloc[-1, frame.columns.get_loc("low")] = 2399.0
    frame.iloc[-1, frame.columns.get_loc("close")] = 2402.8

    snapshot = participation_snapshot(
        frame,
        _ctx(frame),
        point=0.01,
        baseline_bars=20,
        expansion_ratio=1.50,
        abnormal_ratio=2.50,
        contraction_ratio=0.65,
        impulse_range_ratio=1.25,
        close_extreme=0.75,
        vwap_neutral_points=10.0,
        real_volume_coverage=0.80,
    )

    assert snapshot is not None
    assert snapshot["volume_source"] == "tick_volume"
    assert snapshot["participation_state"] == "abnormal_expansion"
    assert snapshot["price_impulse"] == "bullish"
    assert snapshot["session_vwap"] is not None


def test_volume_participation_detection_is_context_only() -> None:
    frame = _frame(110)
    frame.iloc[-1, frame.columns.get_loc("tick_volume")] = 400.0
    frame.iloc[-1, frame.columns.get_loc("high")] = 2403.0
    frame.iloc[-1, frame.columns.get_loc("low")] = 2399.0
    frame.iloc[-1, frame.columns.get_loc("close")] = 2402.8

    agent = VolumeParticipationAgent(timeframe=Timeframe.M5, point=0.01)
    detections = agent.on_closed_candle(frame, _ctx(frame))

    assert detections
    detection = detections[0]
    assert detection.direction is None
    assert detection.evidence.categorical["volume_source"] == "tick_volume"
    assert detection.evidence.flags["abnormal_volume"] is True
    assert "session_vwap" in detection.levels


def test_default_roster_contains_agents_9_11() -> None:
    config = AureonConfig(
        symbols=("XAUUSD",),
        timeframes=(Timeframe.M5,),
        ema_fast=20,
        ema_slow=50,
    )
    names = [agent.agent_name for agent in default_agents(config, symbol="XAUUSD")]

    assert names[-3:] == [
        "market_journey",
        "market_regime",
        "volume_participation",
    ]
    assert len(names) == 9


def test_context_agents_publish_into_live_status_panel() -> None:
    journey_frame = _frame(1200)
    journey_frame.iloc[-1, journey_frame.columns.get_loc("high")] = 2411.0
    journey_frame.iloc[-1, journey_frame.columns.get_loc("close")] = 2410.0
    journey = MarketJourneyAgent(point=0.01, lookback_bars=1200).on_closed_candle(
        journey_frame, _ctx(journey_frame)
    )[0]

    volume_frame = _frame(110)
    volume_frame.iloc[-1, volume_frame.columns.get_loc("tick_volume")] = 400.0
    volume_frame.iloc[-1, volume_frame.columns.get_loc("high")] = 2403.0
    volume_frame.iloc[-1, volume_frame.columns.get_loc("low")] = 2399.0
    volume_frame.iloc[-1, volume_frame.columns.get_loc("close")] = 2402.8
    participation = VolumeParticipationAgent(
        timeframe=Timeframe.M5, point=0.01
    ).on_closed_candle(volume_frame, _ctx(volume_frame))[0]

    snapshot = MarketSnapshot(symbol="XAUUSD")
    snapshot.observe(journey)
    snapshot.observe(participation)

    state = SymbolState(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        **snapshot.as_state(),
    )
    panel = build_live_panel(state)

    assert state.market_journey is not None
    assert state.volume_participation is not None
    assert any(line.startswith("journey ") for line in panel.lines)
    assert any(line.startswith("participation ") for line in panel.lines)
