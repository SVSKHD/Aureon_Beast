"""Phase-5 execution economics tests."""
from types import SimpleNamespace

from aureon.services.phase5_execution_backtest import (
    ExecutionAssumptions, phase5_report, simulate_trade,
)


def _bar(high, low, close):
    return SimpleNamespace(high=high, low=low, close=close)


def test_same_bar_tp_sl_is_conservative_loss() -> None:
    a = ExecutionAssumptions(contract_size=100, spread_price=0, slippage_price=0)
    trade = simulate_trade(
        {"snapshot_id": "x", "sequence_id": "s", "direction": "BUY", "entry_price": 100},
        [_bar(111, 84, 100)], a,
    )
    assert trade["result"] == "AMBIGUOUS_AS_LOSS"
    assert trade["net_move"] == -15


def test_costs_are_adverse_and_money_scales_by_lot() -> None:
    a = ExecutionAssumptions(
        contract_size=100, spread_price=0.2, slippage_price=0.1,
        target_move=10, stop_move=15,
    )
    trade = simulate_trade(
        {"direction": "BUY", "entry_price": 100}, [_bar(111, 100, 110.5)], a,
    )
    assert trade["result"] == "WIN"
    assert trade["net_move"] == 9.9
    assert trade["pnl_by_lot"]["0.01"] == 9.9
    assert trade["pnl_by_lot"]["1.0"] == 990.0


def test_report_requires_core_baselines_and_never_promotes() -> None:
    a = ExecutionAssumptions(contract_size=100, spread_price=0, slippage_price=0)
    trade = simulate_trade({"direction": "SELL", "entry_price": 100}, [_bar(101, 89, 90)], a)
    rows = [trade]
    report = phase5_report(
        baseline_results={
            "original_cross_immediate": rows,
            "deterministic_aureon": rows,
            "wait_confirmation": rows,
            "sequence_ml": rows,
            "counter_move_continuation": rows,
        },
        assumptions=a,
        unseen_period={"from": "2026-03-01", "to": "2026-03-31"},
    )
    assert report["live_execution_allowed"] is False
    assert report["champion_promotion_allowed"] is False
    assert set(report["baselines"]) >= {
        "original_cross_immediate", "deterministic_aureon",
        "wait_confirmation", "sequence_ml",
    }
