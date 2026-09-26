"""Adaptive multi-target learning and advisory profit-plan tests."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aureon.management.adaptive_profit_plan import adaptive_profit_plan
from aureon.ml.boosted_stumps import BoostedStumpModel, fit_boosted_stumps
from aureon.services.adaptive_learning import build_adaptive_examples, select_loss_control_times
from aureon.services.decision_backtest import DecisionReplayRow


def _row(at: datetime) -> DecisionReplayRow:
    return DecisionReplayRow(
        at=at.isoformat(),
        symbol="XAUUSD",
        timeframe="M5",
        event="long_eligible",
        eligible=True,
        direction="buy",
        entry_price=100.0,
        rsi=55.0,
        ema_fast=101.0,
        ema_slow=100.0,
        ema_gap=1.0,
        ema_gap_change=0.2,
        htf_state="bullish",
        director_state="ready",
        director_supporting=7,
        director_opposing=1,
        regime="expanding",
        participation="expanding",
        same_candle_agents=["ema_cross", "ema_rsi_eligibility", "liquidity", "wick"],
        mfe_1=1.0,
        mae_1=0.5,
        mfe_3=5.0,
        mae_3=1.0,
        mfe_6=10.0,
        mae_6=1.5,
        mfe_12=12.0,
        mae_12=2.0,
        reached_10=True,
        bars_to_10=5,
    )


def _candle(opened: datetime, high: float, low: float, close: float):
    return SimpleNamespace(
        open_time=SimpleNamespace(utc=opened),
        high=high,
        low=low,
        close=close,
    )


def test_adaptive_examples_capture_five_targets_and_mae_before_target() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = []
    price = 100.0
    for i in range(20):
        opened = start + timedelta(minutes=5 * i)
        high = price + min(i * 3.0, 45.0)
        low = price - min(i * 0.25, 2.0)
        candles.append(_candle(opened, high, low, price))
    row = _row(start + timedelta(minutes=5))
    examples = build_adaptive_examples([row], candles, horizon_bars=18)
    assert len(examples) == 1
    labels = examples[0]["target_hits"]
    assert labels["5"] is True
    assert labels["10"] is True
    assert labels["20"] is True
    assert labels["30"] is True
    assert labels["40"] is True
    assert examples[0]["mae_before_target"]["10"] is not None


def test_profit_plan_can_secure_five_and_keep_large_runner() -> None:
    plan = adaptive_profit_plan(
        {"5": 0.90, "10": 0.82, "20": 0.74, "30": 0.66, "40": 0.58},
        current_move=12.0,
        peak_move=14.0,
        threshold=0.50,
    )
    assert plan["recommended_target"] == 40
    assert plan["minimum_secure_move"] == 5.0
    assert plan["secure_ready"] is True
    assert plan["long_hold_candidate"] is True
    assert plan["action"] == "PROTECT_AND_HOLD"


def test_boosted_stump_round_trips_and_separates_simple_data() -> None:
    vectors = [[float(i)] for i in range(20)]
    labels = [0 if i < 10 else 1 for i in range(20)]
    model = fit_boosted_stumps(vectors, labels, rounds=20, min_leaf=2)
    restored = BoostedStumpModel.from_dict(model.to_dict())
    assert restored.probability([2.0]) < restored.probability([18.0])


def test_loss_control_selection_filters_risk_and_caps_count() -> None:
    adaptive_test = {
        "scored_rows": [
            {
                "at": f"2026-03-{i + 1:02d}T00:00:00+00:00",
                "probability_clean_10_before_15": clean,
                "probability_stop_15_before_10": stop,
            }
            for i, (clean, stop) in enumerate([
                (0.90, 0.10),
                (0.80, 0.20),
                (0.70, 0.30),
                (0.60, 0.34),
                (0.54, 0.10),
                (0.90, 0.50),
            ])
        ]
    }
    selected = select_loss_control_times(
        adaptive_test,
        min_clean_probability=0.55,
        max_stop_probability=0.35,
        max_trades=3,
    )
    assert len(selected) == 3
    assert "2026-03-01T00:00:00+00:00" in selected
    assert "2026-03-02T00:00:00+00:00" in selected
    assert "2026-03-03T00:00:00+00:00" in selected
    assert "2026-03-05T00:00:00+00:00" not in selected
    assert "2026-03-06T00:00:00+00:00" not in selected
