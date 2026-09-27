from types import SimpleNamespace

import pytest

import aureon.services.saved_model_backtest as subject


class Models:
    def __init__(self, model):
        self.model = model

    def get_model(self, model_id):
        return self.model if model_id == self.model.model_id else None


class Memory:
    def canonical_between(self, symbol, start, end):
        return [object(), object(), object()]


def _model(**updates):
    values = dict(
        model_id="rejected_1",
        symbol="XAUUSD",
        status="rejected",
        algorithm="boosted_stumps_v1",
        trained_from="2023-01-01",
        trained_through="2026-01-31",
    )
    values.update(updates)
    return SimpleNamespace(**values)


def _rows():
    return [
        {
            "setup_id": "a",
            "timestamp": "2026-02-03T10:00:00+00:00",
            "probabilities": {"clean_10": 0.80},
            "actual": {
                "clean_10": True,
                "reach_5": True,
                "reach_10": True,
                "reach_20": False,
                "reach_30": False,
                "reach_40": False,
            },
            "mae_before_10": 2.0,
            "max_adverse_move": 20.0,
            "max_favourable_move": 12.0,
        },
        {
            "setup_id": "b",
            "timestamp": "2026-02-04T10:00:00+00:00",
            "probabilities": {"clean_10": 0.70},
            "actual": {
                "clean_10": False,
                "reach_5": True,
                "reach_10": False,
                "reach_20": False,
                "reach_30": False,
                "reach_40": False,
            },
            "mae_before_10": None,
            "max_adverse_move": 8.0,
            "max_favourable_move": 6.0,
        },
        {
            "setup_id": "c",
            "timestamp": "2026-02-05T10:00:00+00:00",
            "probabilities": {"clean_10": 0.20},
            "actual": {
                "clean_10": True,
                "reach_5": True,
                "reach_10": True,
                "reach_20": True,
                "reach_30": False,
                "reach_40": False,
            },
            "mae_before_10": 1.0,
            "max_adverse_move": 30.0,
            "max_favourable_move": 22.0,
        },
    ]


def _patch_eval(monkeypatch, rows=None):
    rows = rows or _rows()
    monkeypatch.setattr(
        subject,
        "evaluate_v1_artifact",
        lambda model, examples: {
            "samples": len(rows),
            "scored_rows": rows,
            "metrics": {"clean_10": {"precision": 0.5}},
        },
    )


def test_rejected_model_is_backtestable_and_not_mutated(monkeypatch):
    model = _model()
    _patch_eval(monkeypatch)
    report = subject.backtest_saved_model(
        models=Models(model),
        training_memory=Memory(),
        symbol="XAUUSD",
        model_id="rejected_1",
        start_market_date="2026-02-01",
        end_market_date="2026-02-28",
        threshold=0.55,
        usd_per_move=25,
    )
    assert report["model_status"] == "rejected"
    assert model.status == "rejected"
    assert report["trades"] == 2
    assert report["wins"] == 1 and report["losses"] == 1
    assert report["net_move"] == pytest.approx(3)
    assert report["net_pnl_usd"] == pytest.approx(75)
    assert report["max_drawdown_move"] == pytest.approx(7)
    assert report["period_classification"] == "OUT_OF_SAMPLE"


def test_diagnostics_expose_baseline_distribution_and_threshold_lift(monkeypatch):
    model = _model()
    _patch_eval(monkeypatch)
    report = subject.backtest_saved_model(
        models=Models(model),
        training_memory=Memory(),
        symbol="XAUUSD",
        model_id=model.model_id,
        start_market_date="2026-02-01",
        end_market_date="2026-02-28",
        threshold=0.55,
    )
    assert report["baseline_clean_10"] == 2
    assert report["baseline_clean_10_rate"] == pytest.approx(2 / 3)
    assert report["probability_summary"] == {
        "min": pytest.approx(0.20),
        "mean": pytest.approx((0.8 + 0.7 + 0.2) / 3),
        "median": pytest.approx(0.70),
        "max": pytest.approx(0.80),
    }
    cut_20 = next(row for row in report["threshold_sweep"] if row["threshold"] == 0.20)
    cut_55 = next(row for row in report["threshold_sweep"] if row["threshold"] == 0.55)
    assert cut_20["trades"] == 3 and cut_20["wins"] == 2
    assert cut_20["lift_vs_baseline"] == pytest.approx(0.0)
    assert cut_55["trades"] == 2 and cut_55["wins"] == 1
    assert cut_55["lift_vs_baseline"] == pytest.approx(0.5 - (2 / 3))


def test_mae_before_10_is_not_confused_with_full_horizon_mae(monkeypatch):
    model = _model()
    _patch_eval(monkeypatch)
    report = subject.backtest_saved_model(
        models=Models(model),
        training_memory=Memory(),
        symbol="XAUUSD",
        model_id=model.model_id,
        start_market_date="2026-02-01",
        end_market_date="2026-02-28",
        threshold=0.20,
    )
    # Only rows that actually reached +10 have mae_before_10. The full-horizon
    # excursion is intentionally separate and can be much larger after target.
    assert report["average_mae_before_10"] == pytest.approx(1.5)
    assert report["average_mae"] == pytest.approx(1.5)
    assert report["average_full_horizon_mae"] == pytest.approx((20 + 8 + 30) / 3)


def test_period_classification_detects_training_overlap(monkeypatch):
    model = _model()
    _patch_eval(monkeypatch)
    in_sample = subject.backtest_saved_model(
        models=Models(model),
        training_memory=Memory(),
        symbol="XAUUSD",
        model_id=model.model_id,
        start_market_date="2025-01-01",
        end_market_date="2025-12-31",
    )
    overlap = subject.backtest_saved_model(
        models=Models(model),
        training_memory=Memory(),
        symbol="XAUUSD",
        model_id=model.model_id,
        start_market_date="2026-01-15",
        end_market_date="2026-02-15",
    )
    assert in_sample["period_classification"] == "IN_SAMPLE"
    assert overlap["period_classification"] == "OVERLAPS_TRAINING"


def test_unknown_model_fails_clearly():
    model = _model(model_id="known", status="champion", algorithm="logistic_regression_v1")
    with pytest.raises(ValueError, match="model not found"):
        subject.backtest_saved_model(
            models=Models(model),
            training_memory=Memory(),
            symbol="XAUUSD",
            model_id="missing",
            start_market_date="2026-01-01",
            end_market_date="2026-01-31",
        )
