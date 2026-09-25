"""Reference training for chronological decision replay."""

from aureon.services.decision_backtest import DecisionReplayRow, train_reference


def _row(i: int, reached: bool) -> DecisionReplayRow:
    return DecisionReplayRow(
        at=f"2026-01-{(i % 28) + 1:02d}T00:00:00+00:00",
        symbol="XAUUSD", timeframe="M5",
        event="long_eligible" if i % 2 == 0 else "short_eligible",
        eligible=True,
        direction="buy" if i % 2 == 0 else "sell",
        entry_price=4500.0,
        rsi=55.0 if i % 2 == 0 else 65.0,
        ema_fast=4501.0, ema_slow=4500.0,
        ema_gap=1.0 if i % 2 == 0 else -1.0,
        ema_gap_change=0.2,
        htf_state="bullish" if i % 3 else "mixed",
        director_state="ready" if i % 2 == 0 else "forming",
        director_supporting=6 if i % 2 == 0 else 4,
        director_opposing=1,
        regime="trend_expansion",
        participation="expanding",
        same_candle_agents=["ema_cross", "ema_rsi_eligibility"],
        mfe_1=1.0, mae_1=0.5, mfe_3=5.0, mae_3=1.0,
        mfe_6=10.0 if reached else 4.0, mae_6=1.5,
        mfe_12=12.0 if reached else 5.0, mae_12=2.0,
        reached_10=reached,
        bars_to_10=5 if reached else None,
    )


def test_reference_model_is_chronological_and_marked_reference_only() -> None:
    rows = [_row(i, reached=(i % 3 != 0)) for i in range(60)]
    result = train_reference(rows, train_fraction=0.7)
    assert result["status"] == "reference_only"
    assert result["historical_reference_only"] is True
    assert result["train_samples"] == 42
    assert result["test_samples"] == 18
    assert result["test_metrics"]["samples"] == 18
