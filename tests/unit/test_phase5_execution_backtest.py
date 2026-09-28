"""Phase-5 execution economics and evidence-contract tests."""
from types import SimpleNamespace

import pytest

from aureon.services.phase5_execution_backtest import (
    ExecutionAssumptions, JAN23_MODEL_ID, phase5_report, simulate_trade,
)


def _bar(high, low, close):
    return SimpleNamespace(high=high, low=low, close=close)


def _trade(i=0, *, model_id=None, result="WIN", pnl=10.0, stage="CROSS"):
    return {
        "signal_id": f"x{i}", "sequence_id": f"s{i}",
        "timestamp": f"2023-01-{i + 1:02d}T12:00:00+00:00",
        "stage": stage, "model_id": model_id, "direction": "BUY",
        "result": result, "intrabar_ambiguous": result == "AMBIGUOUS_AS_LOSS",
        "net_move": pnl, "pnl_by_lot": {
            str(lot): pnl * lot * 100 for lot in (0.01, 0.05, 0.10, 0.25, 0.50, 1.00)
        },
    }


def _provenance():
    return {"model_id": JAN23_MODEL_ID, "dataset": "JAN23_RESEARCH", "sha256": "abc123"}


def test_same_bar_tp_sl_is_conservative_loss() -> None:
    a = ExecutionAssumptions(contract_size=100, spread_price=0, slippage_price=0)
    trade = simulate_trade(
        {"snapshot_id": "x", "sequence_id": "s", "direction": "BUY", "entry_price": 100},
        [_bar(111, 84, 100)], a,
    )
    assert trade["result"] == "AMBIGUOUS_AS_LOSS"
    assert trade["net_move"] == -15


def test_invalid_direction_fails_closed() -> None:
    a = ExecutionAssumptions(contract_size=100, spread_price=0, slippage_price=0)
    with pytest.raises(ValueError, match="unsupported trade direction"):
        simulate_trade({"direction": "WAIT", "entry_price": 100}, [_bar(101, 99, 100)], a)


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


def test_report_requires_frozen_model_binding_and_never_promotes() -> None:
    a = ExecutionAssumptions(contract_size=100, spread_price=0, slippage_price=0)
    generic = [_trade(0)]
    ml = [_trade(0, model_id=JAN23_MODEL_ID)]
    report = phase5_report(
        baseline_results={
            "original_cross_immediate": generic,
            "deterministic_aureon": generic,
            "wait_confirmation": generic,
            "sequence_ml": ml,
        },
        assumptions=a,
        unseen_period={"from": "2023-01-01", "to": "2023-01-31"},
        phase4_provenance=_provenance(),
    )
    assert report["live_execution_allowed"] is False
    assert report["champion_promotion_allowed"] is False
    assert report["registry_write_allowed"] is False
    assert report["phase4_provenance"]["model_id"] == JAN23_MODEL_ID


def test_sequence_ml_cannot_masquerade_as_frozen_challenger() -> None:
    a = ExecutionAssumptions(contract_size=100, spread_price=0, slippage_price=0)
    generic = [_trade(0)]
    with pytest.raises(ValueError, match="not bound"):
        phase5_report(
            baseline_results={
                "original_cross_immediate": generic,
                "deterministic_aureon": generic,
                "wait_confirmation": generic,
                "sequence_ml": generic,
            },
            assumptions=a,
            unseen_period={"from": "2023-01-01", "to": "2023-01-31"},
            phase4_provenance=_provenance(),
        )


def test_evidence_must_be_chronological_and_inside_declared_period() -> None:
    a = ExecutionAssumptions(contract_size=100, spread_price=0, slippage_price=0)
    rows = [_trade(1), _trade(0)]
    ml = [_trade(0, model_id=JAN23_MODEL_ID)]
    with pytest.raises(ValueError, match="not chronological"):
        phase5_report(
            baseline_results={
                "original_cross_immediate": rows,
                "deterministic_aureon": [_trade(0)],
                "wait_confirmation": [_trade(0)],
                "sequence_ml": ml,
            },
            assumptions=a,
            unseen_period={"from": "2023-01-01", "to": "2023-01-31"},
            phase4_provenance=_provenance(),
        )


def test_report_includes_equity_drawdown_and_stage_breakdown() -> None:
    a = ExecutionAssumptions(contract_size=100, spread_price=0, slippage_price=0)
    generic = [_trade(0, pnl=10), _trade(1, result="LOSS", pnl=-15, stage="COUNTER_MOVE")]
    ml = [
        _trade(0, model_id=JAN23_MODEL_ID, pnl=10),
        _trade(1, model_id=JAN23_MODEL_ID, result="LOSS", pnl=-15, stage="REENTRY_CONTINUATION"),
    ]
    report = phase5_report(
        baseline_results={
            "original_cross_immediate": generic,
            "deterministic_aureon": generic,
            "wait_confirmation": generic,
            "sequence_ml": ml,
        },
        assumptions=a,
        unseen_period={"from": "2023-01-01", "to": "2023-01-31"},
        phase4_provenance=_provenance(),
    )
    seq = report["baselines"]["sequence_ml"]
    assert seq["max_drawdown_by_lot"]["1.0"] == 1500.0
    assert seq["by_stage"]["REENTRY_CONTINUATION"]["trades"] == 1
