"""Item 10: the live intelligence output Discord renders, from stored state only."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from aureon.discord.service import (
    _daily_bias_lines,
    build_live_panel,
    build_status,
    v1_intelligence_lines,
)
from aureon.management.exit_manager import DeterministicExitManager
from aureon.models.enums import Direction, MarketState, SessionName, Timeframe, TradeSource
from aureon.models.learning_v1 import (
    CoverageAssessment,
    CoverageStatus,
    EntryDecision,
    EntryIntelligence,
)
from aureon.models.market_bias import DailyBiasState, DailyMarketBiasSnapshot
from aureon.models.settings import ExecutionSettings
from aureon.models.system import SymbolState, SystemState

AT = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


def _bias() -> DailyMarketBiasSnapshot:
    return DailyMarketBiasSnapshot(
        symbol="XAUUSD",
        timeframe="M5",
        market_date="2026-03-02",
        timestamp=AT,
        daily_bias=DailyBiasState.BULLISH,
        daily_bias_strength=0.62,
        preferred_direction=Direction.BUY,
        current_session=SessionName.LONDON,
        session_bias=DailyBiasState.STRONG_BULLISH,
        session_bias_strength=0.8,
        opportunity_quality=0.7,
        best_session_so_far=SessionName.LONDON,
        session_transition_state="ASIA_RANGE->LONDON_BULLISH_BREAKOUT",
    )


def _intelligence() -> EntryIntelligence:
    return EntryIntelligence(
        model_id="xauusd_entry_logistic_abc",
        generation=2,
        setup_id="setup-123456789",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        predicted_at=AT,
        direction=Direction.BUY,
        confidence=0.71,
        probability_clean_10=0.71,
        probability_reach_5=0.9,
        probability_reach_10=0.7,
        probability_reach_20=0.5,
        probability_reach_30=0.3,
        probability_reach_40=0.2,
        daily_bias="bullish",
        daily_bias_strength=0.62,
        session="london",
        session_bias="strong_bullish",
        training_coverage=CoverageAssessment(
            status=CoverageStatus.NORMAL_COVERAGE, similar_samples=140
        ),
        decision=EntryDecision.ENTER,
        reason="P(clean_10)=0.710 >= 0.550",
    )


def test_live_panel_renders_daily_and_session_bias_with_strengths() -> None:
    state = SymbolState(
        symbol="XAUUSD", timeframe=Timeframe.M5, market_state=MarketState.OPEN, daily_bias=_bias()
    )
    lines = build_live_panel(state).lines
    daily = next(line for line in lines if line.startswith("daily bias"))
    session = next(line for line in lines if line.startswith("session bias"))
    assert "bullish" in daily and "strength 0.62" in daily and "prefers BUY" in daily
    assert "london strong_bullish" in session and "strength 0.80" in session
    assert "ASIA_RANGE->LONDON_BULLISH_BREAKOUT" in session
    assert "probab" not in daily.lower()  # strengths, never probabilities
    empty = SymbolState(symbol="XAUUSD", timeframe=Timeframe.M5, market_state=MarketState.OPEN)
    assert _daily_bias_lines(empty) == ["daily bias —", "session bias —"]


def test_intelligence_lines_show_decision_probabilities_coverage_and_position() -> None:
    manager = DeterministicExitManager()
    state = manager.open(entry_price=2400.0, direction=Direction.BUY, initial_stop=2393.0, now=AT)
    state, _ = manager.assess(state, current_price=2412.0, now=AT)
    trade = SimpleNamespace(
        symbol="XAUUSD",
        direction=Direction.BUY,
        open_price=2400.0,
        source=TradeSource.AUREON,
        aureon_managed=True,
        management_state=state,
    )
    lines = v1_intelligence_lines(_intelligence(), trade)
    assert "ENTER" in lines[0] and "P(clean_10) 0.71" in lines[0] and "gen 2" in lines[0]
    assert "+20:0.50" in lines[1] and "coverage NORMAL_COVERAGE (140 similar)" in lines[1]
    assert "bias bullish(0.62)" in lines[1] and "session london strong_bullish" in lines[1]
    assert lines[2].startswith("  reason:")
    position = lines[3]
    assert "trailing" in position and "stop 2406." in position and "+5 +10" in position
    assert "protection on" in position
    assert v1_intelligence_lines(None, None) == [
        "champion — · no recorded call yet",
        "aureon position —",
    ]


def test_status_screen_carries_the_intelligence_block_per_symbol() -> None:
    symbol_state = SymbolState(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        market_state=MarketState.OPEN,
        daily_bias=_bias(),
        entry_intelligence=_intelligence(),
    )
    system_state = SystemState(symbols=(symbol_state,), updated_at=AT)
    screen = build_status(
        system_state=system_state,
        heartbeats={},
        settings=ExecutionSettings(),
        now=AT,
        aureon_trades=[],
        symbol="XAUUSD",
    )
    assert screen.intelligence and screen.intelligence[0].startswith(
        "champion xauusd_entry_logistic_abc"
    )
    unscoped = build_status(
        system_state=system_state, heartbeats={}, settings=ExecutionSettings(), now=AT
    )
    assert unscoped.intelligence[0].startswith("XAUUSD champion")
