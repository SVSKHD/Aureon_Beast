"""Tests for JAN23 execution diagnostics."""
from types import SimpleNamespace

from aureon.services.jan23_execution_diagnostics import diagnose_signal, summarize


def _bar(high, low, close):
    return SimpleNamespace(high=high, low=low, close=close)


def test_buy_target_ladder_mfe_mae_and_time_to_target():
    signal = {"sequence_id": "s", "snapshot_id": "x", "timestamp": "2023-01-26T00:00:00+00:00", "stage": "REENTRY_CONTINUATION", "direction": "BUY", "entry_price": 100}
    row = diagnose_signal(signal, [_bar(105, 98, 104), _bar(111, 99, 110), _bar(121, 100, 120)])
    assert row["mfe"] == 21
    assert row["mae"] == 2
    assert row["targets"]["6"]["bars_to"] == 2
    assert row["targets"]["10"]["bars_to"] == 2
    assert row["targets"]["20"]["bars_to"] == 3
    assert row["targets"]["30"]["reached"] is False


def test_sell_is_directionally_symmetric():
    signal = {"direction": "SELL", "entry_price": 100, "stage": "CROSS"}
    row = diagnose_signal(signal, [_bar(103, 93, 94)])
    assert row["mfe"] == 7
    assert row["mae"] == 3
    assert row["targets"]["6"]["reached"] is True
    assert row["targets"]["10"]["reached"] is False


def test_summary_reports_target_rates_and_stage():
    rows = [
        diagnose_signal({"direction": "BUY", "entry_price": 100, "stage": "CROSS"}, [_bar(111, 99, 110)]),
        diagnose_signal({"direction": "BUY", "entry_price": 100, "stage": "CROSS"}, [_bar(105, 98, 104)]),
    ]
    result = summarize(rows)
    assert result["targets"]["6"]["hits"] == 1
    assert result["targets"]["6"]["hit_rate"] == 0.5
    assert result["by_stage"]["CROSS"]["signals"] == 2
