"""Logical agents 12-16."""

from __future__ import annotations

from datetime import UTC, datetime

from aureon.management.profit_guardian import ProfitGuardianAgent
from aureon.management.trade_manager import TradeManagementAgent
from aureon.models.agent_decision import (
    DirectorState,
    HtfState,
    ManagementAction,
    RiskVerdict,
)
from aureon.models.enums import Direction, Timeframe, TrendBias
from aureon.models.mtf import MtfContext, TimeframeRead
from aureon.risk.risk_agent import RiskAgent, RiskInputs
from aureon.services.higher_timeframe_agent import HigherTimeframeAgent
from aureon.services.market_director import DirectorInputs, MarketDirector


def _mtf(*biases: tuple[Timeframe, TrendBias]) -> MtfContext:
    now = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
    reads = tuple(
        TimeframeRead(
            timeframe=timeframe,
            at=now,
            ema_fast=20.0 if bias is not TrendBias.SIDEWAYS else 20.0,
            ema_slow=10.0 if bias is TrendBias.BULLISH else (
                30.0 if bias is TrendBias.BEARISH else 20.0
            ),
            close=100.0,
            bias=bias,
        )
        for timeframe, bias in biases
    )
    return MtfContext(reads=reads, ema_fast_period=20, ema_slow_period=50)


def test_higher_timeframe_agent_reports_requested_m15_h1_h4() -> None:
    agent = HigherTimeframeAgent()
    result = agent.assess(
        _mtf(
            (Timeframe.M15, TrendBias.BULLISH),
            (Timeframe.H1, TrendBias.BULLISH),
            (Timeframe.H4, TrendBias.BULLISH),
        )
    )

    assert result.state is HtfState.BULLISH
    assert result.dominant_bias is TrendBias.BULLISH
    assert result.bullish_count == 3
    assert result.strongest_available is Timeframe.H4


def test_higher_timeframe_agent_calls_disagreement_mixed() -> None:
    result = HigherTimeframeAgent().assess(
        _mtf(
            (Timeframe.M15, TrendBias.BULLISH),
            (Timeframe.H1, TrendBias.BEARISH),
            (Timeframe.H4, TrendBias.BEARISH),
        )
    )
    assert result.state is HtfState.MIXED
    assert result.dominant_bias is TrendBias.SIDEWAYS


def test_director_can_reach_ready_only_with_structure_trigger() -> None:
    director = MarketDirector(ready_support=6, forming_support=4)
    htf = HigherTimeframeAgent().assess(
        _mtf(
            (Timeframe.M15, TrendBias.BULLISH),
            (Timeframe.H1, TrendBias.BULLISH),
            (Timeframe.H4, TrendBias.BULLISH),
        )
    )
    decision = director.decide(
        DirectorInputs(
            price=2500.0,
            ema_fast=2501.0,
            ema_slow=2499.0,
            rsi=61.0,
            session_trend="up",
            journey={
                "state": "london|above_asia_high|inside_previous_day_range|above",
                "flags": {"above_asia_open": True, "above_previous_close": True},
            },
            regime={"regime": "trend_expansion"},
            participation={
                "price_impulse": "bullish",
                "vwap_relation": "above",
            },
            htf=htf,
            candle_signals=(
                ("breakout", Direction.BUY),
                ("wick", Direction.BUY),
            ),
            as_of=datetime(2026, 9, 25, 9, 5, tzinfo=UTC),
        )
    )

    assert decision.state is DirectorState.READY
    assert decision.direction is Direction.BUY
    assert decision.supporting >= 6
    assert not decision.blockers


def test_director_does_not_call_htf_conflict_ready() -> None:
    director = MarketDirector(ready_support=6, forming_support=4)
    htf = HigherTimeframeAgent().assess(
        _mtf(
            (Timeframe.M15, TrendBias.BEARISH),
            (Timeframe.H1, TrendBias.BEARISH),
            (Timeframe.H4, TrendBias.BEARISH),
        )
    )
    decision = director.decide(
        DirectorInputs(
            price=2500.0,
            ema_fast=2501.0,
            ema_slow=2499.0,
            rsi=61.0,
            session_trend="up",
            journey={
                "state": "london",
                "flags": {"above_asia_open": True, "above_previous_close": True},
            },
            regime={"regime": "trend_expansion"},
            participation={"price_impulse": "bullish", "vwap_relation": "above"},
            htf=htf,
            candle_signals=(("breakout", Direction.BUY), ("wick", Direction.BUY)),
        )
    )
    assert decision.state is not DirectorState.READY
    assert any("higher timeframes" in blocker for blocker in decision.blockers)


def test_risk_agent_vetoes_when_primary_target_has_no_clear_room() -> None:
    result = RiskAgent().assess(
        RiskInputs(
            direction=Direction.BUY,
            entry_price=2500.0,
            stop_price=2492.0,
            nearest_obstacle_price=2504.0,
            primary_target_move=10.0,
            open_positions=0,
            max_open_positions=1,
            daily_realized_pnl=100.0,
            daily_loss_limit=1000.0,
            volatility_regime="trend_expansion",
        )
    )
    assert result.verdict is RiskVerdict.VETO
    assert result.clear_room == 4.0


def test_risk_agent_is_incomplete_without_account_exposure() -> None:
    result = RiskAgent().assess(
        RiskInputs(
            direction=Direction.BUY,
            entry_price=2500.0,
            nearest_obstacle_price=2520.0,
        )
    )
    assert result.verdict is RiskVerdict.INCOMPLETE
    assert "open_positions" in result.missing


def test_trade_manager_hands_off_at_primary_plus_10() -> None:
    result = TradeManagementAgent(primary_target_move=10.0).assess(
        direction=Direction.BUY,
        entry_price=2500.0,
        current_price=2511.0,
        peak_price=2512.0,
    )
    assert result.target_reached is True
    assert result.action is ManagementAction.HANDOFF_RUNNER
    assert result.peak_move == 12.0


def test_guardian_trails_healthy_runner_and_explains_close_conditions() -> None:
    result = ProfitGuardianAgent().assess(
        direction=Direction.BUY,
        entry_price=2500.0,
        current_price=2521.0,
        peak_price=2524.0,
        market_health={
            "ema": True,
            "session": True,
            "htf": True,
            "regime": True,
            "participation": True,
        },
    )
    assert result.active is True
    assert result.action is ManagementAction.TRAIL
    assert result.trail_price is not None
    assert result.trail_price < 2521.0
    assert result.continuation_score == 5
    assert result.close_conditions


def test_guardian_emergency_exit_after_large_giveback_and_breakdown() -> None:
    result = ProfitGuardianAgent(emergency_giveback=8.0).assess(
        direction=Direction.BUY,
        entry_price=2500.0,
        current_price=2514.0,
        peak_price=2524.0,
        market_health={
            "ema": False,
            "session": False,
            "htf": True,
            "regime": False,
            "participation": False,
        },
    )
    assert result.emergency is True
    assert result.action is ManagementAction.EXIT
    assert result.giveback == 10.0
    assert result.protected_move == 14.0
