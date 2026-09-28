"""Tests for the one-command JAN23 Phase-5 signal construction."""
from scripts.phase5_jan23_pipeline import _build_signals, _verify_phase4
from aureon.services.phase4_jan23_challenger import (
    _classification_metrics, fit_classifier,
)
from aureon.services.phase5_execution_backtest import JAN23_MODEL_ID


def _row(i, stage, decision, direction="BUY", entry=100.0):
    return {
        "snapshot_id": f"snap-{i}-{stage}",
        "sequence_id": f"seq-{i}",
        "timestamp": f"2023-01-{i + 1:02d}T12:00:00+00:00",
        "stage": stage,
        "features": {"entry_price": entry, "session": "london"},
        "labels": {"decision": decision, "direction": direction},
    }


def test_phase4_reproduction_returns_same_classifier() -> None:
    train = [
        _row(0, "CROSS", "ENTER"),
        _row(1, "CROSS", "WAIT"),
        _row(2, "CROSS", "INVALIDATE"),
    ]
    test = [_row(3, "CROSS", "ENTER")]
    model = fit_classifier(train, "decision")
    metrics = _classification_metrics(test, model)
    report = {
        "model_id": JAN23_MODEL_ID,
        "research_only": True,
        "production_eligible": False,
        "test": {"decision": metrics},
    }
    reproduced = _verify_phase4(report, train, test)
    assert reproduced.probabilities(test[0]["features"]) == model.probabilities(test[0]["features"])


def test_sequence_ml_uses_prediction_not_future_decision_label() -> None:
    train = [
        _row(0, "CROSS", "ENTER"),
        _row(1, "CROSS", "ENTER"),
        _row(2, "CROSS", "WAIT"),
    ]
    model = fit_classifier(train, "decision")
    test = [_row(3, "COUNTER_MOVE", "INVALIDATE")]
    crosses = {
        "seq-3": {
            "sequence_id": "seq-3", "timestamp": test[0]["timestamp"],
            "direction": "BUY", "entry_price": 99.0, "stage": "CROSS",
        }
    }
    signals = _build_signals(test, crosses, model)
    if signals["sequence_ml"]:
        assert signals["sequence_ml"][0]["model_id"] == JAN23_MODEL_ID
    # Deterministic baseline is based on observable candidate existence, not its outcome label.
    assert len(signals["deterministic_aureon"]) == 1


def test_cross_price_is_recovered_from_phase2_not_future_label() -> None:
    train = [_row(0, "CROSS", "ENTER"), _row(1, "CROSS", "WAIT")]
    model = fit_classifier(train, "decision")
    row = _row(3, "CROSS", "WAIT", entry=None)
    row["features"]["entry_price"] = None
    crosses = {
        "seq-3": {
            "sequence_id": "seq-3", "timestamp": row["timestamp"],
            "direction": "BUY", "entry_price": 1975.25, "stage": "CROSS",
        }
    }
    signals = _build_signals([row], crosses, model)
    assert signals["original_cross_immediate"][0]["entry_price"] == 1975.25
