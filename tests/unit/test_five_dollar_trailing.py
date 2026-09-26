"""Tests for +5 activation trailing and no-fixed-TP management."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aureon.management.profit_guardian import ProfitGuardianAgent
from aureon.management.trade_manager import TradeManagementAgent
from aureon.models.agent_decision import ManagementAction
from aureon.models.enums import Direction
from aureon.services.decision_backtest import DecisionReplayRow, simulate_trailing_outcomes


def _row(at: datetime) -> DecisionReplayRow:
    return DecisionReplayRow(
        at=at.isoformat(), symbol="XAUUSD", timeframe="M5", event="long_eligible",
        eligible=True, direction="buy", entry_price=100.0, rsi=55.0,
        ema_fast=101.0, ema_slow=100.0, ema_gap=1.0, ema_gap_change=0.2,
        htf_state="bullish", director_state="ready", director_supporting=7,
        director_opposing=1, regime="expanding", participation="expanding",
        same_candle_agents=["ema_cross", "ema_rsi_eligibility"],
        mfe_1=1.0, mae_1=0.0, mfe_3=5.0, mae_3=1.0,
        mfe_6=12.0, mae_6=1.0, mfe_12=30.0, mae_12=1.0,
        reached_10=True, bars_to_10=5,
    )


def _bar(at: datetime, high: float, low: float, close: float):
    return SimpleNamespace(
        open_time=SimpleNamespace(utc=at), high=high, low=low, close=close
    )


def test_trade_manager_hands_off_at_five_dollar_move() -> None:
    manager = TradeManagementAgent()
    decision = manager.assess(
        direction=Direction.BUY, entry_price=100.0, current_price=105.2, peak_price=105.2
    )
    assert decision.target_reached is True
    assert decision.action is ManagementAction.HANDOFF_RUNNER
    assert decision.primary_target_move == 5.0
    assert decision.protected_move == 4.0


def test_profit_guardian_activates_at_five_and_trails() -> None:
    guardian = ProfitGuardianAgent()
    decision = guardian.assess(
        direction=Direction.BUY,
        entry_price=100.0,
        current_price=106.0,
        peak_price=106.0,
        market_health={"ema": True, "htf": True},
    )
    assert decision.active is True
    assert decision.action is ManagementAction.TRAIL
    assert decision.protected_move is not None
    assert decision.protected_move >= 4.0


def test_trailing_backtest_has_no_fixed_ten_dollar_exit() -> None:
    start = datetime(2026, 3, 1, tzinfo=UTC)
    row = _row(start)
    candles = [
        _bar(start, 103.0, 99.0, 102.0),
        _bar(start + timedelta(minutes=5), 106.0, 101.0, 105.0),
        _bar(start + timedelta(minutes=10), 112.0, 105.0, 111.0),
        _bar(start + timedelta(minutes=15), 125.0, 110.0, 124.0),
        _bar(start + timedelta(minutes=20), 128.0, 112.0, 113.0),
    ]
    result = simulate_trailing_outcomes(
        [row], candles, stop_move=15.0, activation_move=5.0,
        minimum_lock_move=4.0, trail_fraction_of_peak=0.55,
        hold_bars=5, lot_size=0.25, contract_size=100.0,
    )
    assert result["trades"] == 1
    trade = result["trade_rows"][0]
    assert trade["trail_activated"] is True
    assert trade["peak_move"] > 10.0
    assert trade["price_move"] > 5.0
    assert trade["result"] == "trailed"
