"""Historical validation of the live exit manager against a fixed take-profit (item 7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aureon.management.exit_manager import ExitPolicy
from aureon.services.exit_policy_backtest import (
    compare_exit_policies,
    render_exit_comparison,
    simulate_exit_manager,
    simulate_fixed_target,
)

T0 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def _bar(index: int, open_: float, high: float, low: float, close: float):
    opened = T0 + timedelta(minutes=5 * index)
    return SimpleNamespace(
        open=open_,
        high=high,
        low=low,
        close=close,
        open_time=SimpleNamespace(utc=opened),
        close_time=SimpleNamespace(utc=opened + timedelta(minutes=5)),
    )


def _row(at: datetime, direction: str = "buy", entry: float = 100.0):
    return SimpleNamespace(at=at.isoformat(), eligible=True, direction=direction, entry_price=entry)


def _runner_candles():
    """A setup that runs to +32 then gives back to +18: a fixed +10 leaves 22 on the table."""
    prices = [100, 102, 104, 106, 109, 113, 118, 124, 130, 132, 128, 124, 120, 118, 118, 118]
    return [_bar(i, p, p + 1.0, p - 1.0, p) for i, p in enumerate(prices)]


def _loser_candles():
    prices = [100, 99, 97, 95, 92, 90, 88]
    return [_bar(i, p, p + 0.5, p - 0.5, p) for i, p in enumerate(prices)]


def test_fixed_target_takes_ten_and_exit_manager_lets_the_runner_run() -> None:
    candles = _runner_candles()
    rows = [_row(T0)]
    fixed = simulate_fixed_target(rows, candles, target_move=10.0, stop_move=7.0, hold_bars=50)
    managed = simulate_exit_manager(rows, candles, policy=ExitPolicy(), stop_move=7.0, hold_bars=50)
    assert fixed.trades[0].realized_move == 10.0 and fixed.trades[0].result == "target"
    trade = managed.trades[0]
    assert trade.mfe == 33.0
    assert trade.realized_move > 10.0
    assert trade.result in {"protective_stop", "timeout_runner"}
    assert trade.reached["reached_30"] is True
    assert managed.summary["runners_captured"] == 1 and fixed.summary["runners_captured"] == 0
    assert managed.summary["given_back"] < 33.0 - 10.0


def test_both_policies_stop_a_loser_at_the_same_risk() -> None:
    candles = _loser_candles()
    rows = [_row(T0)]
    fixed = simulate_fixed_target(rows, candles, target_move=10.0, stop_move=7.0, hold_bars=50)
    managed = simulate_exit_manager(rows, candles, policy=ExitPolicy(), stop_move=7.0, hold_bars=50)
    assert fixed.trades[0].realized_move == -7.0
    assert managed.trades[0].realized_move == -7.0 and managed.trades[0].result == "hard_risk"


def test_comparison_reports_every_policy_with_counts_and_renders() -> None:
    candles = _runner_candles() + [
        _bar(100 + i, p, p + 0.5, p - 0.5, p) for i, p in enumerate([118, 117, 115, 112, 110, 108])
    ]
    rows = [_row(T0), _row(T0 + timedelta(minutes=500))]
    summaries = compare_exit_policies(rows, candles, stop_move=7.0, fixed_target=10.0, hold_bars=40)
    assert summaries[0]["label"].startswith("fixed_tp_10")
    assert len(summaries) == 5
    for s in summaries:
        assert s["samples"] == 2
        assert {
            "net_move",
            "runners_captured",
            "premature_exits",
            "mfe_captured",
            "given_back",
            "max_drawdown",
        } <= set(s)
    text = render_exit_comparison(summaries)
    assert "fixed_tp_10" in text and "exit_manager_act5" in text and "evidence only" in text


def test_sell_setups_are_measured_on_the_mirrored_path() -> None:
    prices = [100, 98, 96, 93, 89, 85, 88, 90]
    candles = [_bar(i, p, p + 0.5, p - 0.5, p) for i, p in enumerate(prices)]
    managed = simulate_exit_manager(
        [_row(T0, "sell")], candles, policy=ExitPolicy(), stop_move=7.0, hold_bars=20
    )
    trade = managed.trades[0]
    assert trade.mfe == 15.5 and trade.realized_move > 5.0
