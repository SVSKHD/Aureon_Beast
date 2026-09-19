"""Indicator correctness and the window-dependence contract (§14)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from aureon.engine.indicators import (
    WARMUP_PERIODS,
    crossed,
    crossed_at_last,
    ema,
    min_warmup,
    rsi,
)


def series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


# ── EMA ───────────────────────────────────────────────────────────────────────


def test_ema_of_a_constant_is_that_constant() -> None:
    assert ema(series([100.0] * 40), 9).iloc[-1] == pytest.approx(100.0)


def test_ema_masks_its_warmup() -> None:
    """An unwarmed value must be NaN, not a partially-warmed number.

    A caller cannot then mistake an early value for a real one.
    """
    result = ema(series([100.0] * 30), 9)
    assert result.iloc[:8].isna().all()
    assert result.iloc[8:].notna().all()


def test_ema_lags_a_step_change() -> None:
    """After a jump, EMA sits between the old and new level."""
    result = ema(series([100.0] * 20 + [110.0] * 5), 9).iloc[-1]
    assert 100.0 < result < 110.0


def test_ema_is_deterministic() -> None:
    values = series(list(100 + np.cumsum(np.random.default_rng(3).normal(0, 1, 120))))
    assert ema(values, 21).equals(ema(values, 21))


def test_ema_depends_on_how_much_history_it_is_given() -> None:
    """The property the fixed-window parity contract exists for.

    Recursive indicators never fully forget their seed. If this ever stops being
    true, ``AnalysisEngine``'s fixed-window requirement can be relaxed -- and until
    then it must not be.
    """
    trend = series([2400.0 + i for i in range(200)])
    short = ema(trend.iloc[-30:].reset_index(drop=True), 21).iloc[-1]
    long = ema(trend, 21).iloc[-1]
    assert short != pytest.approx(long, abs=1e-6)


def test_min_warmup_is_three_slow_periods() -> None:
    assert min_warmup(21) == 21 * WARMUP_PERIODS
    with pytest.raises(ValueError):
        min_warmup(0)


@pytest.mark.parametrize("bad", [0, -1])
def test_ema_rejects_a_nonpositive_period(bad: int) -> None:
    with pytest.raises(ValueError):
        ema(series([1.0, 2.0]), bad)


def test_ema_of_an_empty_series_is_empty() -> None:
    assert ema(series([]), 9).empty


# ── RSI ───────────────────────────────────────────────────────────────────────


def test_rsi_pins_at_the_extremes() -> None:
    rising = series([float(x) for x in range(1, 40)])
    falling = series([float(x) for x in range(40, 1, -1)])
    assert rsi(rising, 14).iloc[-1] == pytest.approx(100.0)
    assert rsi(falling, 14).iloc[-1] == pytest.approx(0.0)


def test_rsi_stays_within_bounds() -> None:
    walk = series(list(100 + np.cumsum(np.random.default_rng(7).normal(0, 1, 300))))
    values = rsi(walk, 14).dropna()
    assert values.min() >= 0.0
    assert values.max() <= 100.0


def test_rsi_masks_its_warmup() -> None:
    result = rsi(series([float(x) for x in range(1, 40)]), 14)
    assert result.iloc[:14].isna().all()
    assert result.iloc[14:].notna().all()


def test_rsi_of_a_flat_series_is_neutral() -> None:
    """No gains and no losses is genuinely undefined; 50 is the convention."""
    assert rsi(series([100.0] * 40), 14).iloc[-1] == pytest.approx(50.0)


def test_rsi_returns_all_nan_when_too_short() -> None:
    assert rsi(series([1.0, 2.0, 3.0]), 14).isna().all()


# ── Crosses ───────────────────────────────────────────────────────────────────


def test_crossed_detects_both_directions_once_each() -> None:
    fast = series([1.0, 1.0, 2.0, 3.0, 2.0, 1.0])
    slow = series([2.0] * 6)
    assert list(crossed(fast, slow)) == [0, 0, 0, 1, 0, -1]


@pytest.mark.parametrize(
    "fast_values,expected,description",
    [
        ([1.0, 2.0, 2.0, 1.0], [0, 0, 0, 0], "touched from below, fell back: never crossed"),
        ([3.0, 2.0, 2.0, 3.0], [0, 0, 0, 0], "touched from above, rose back: never crossed"),
        ([3.0, 2.0, 1.0], [0, 0, -1], "genuine cross passing exactly through equality"),
        ([1.0, 2.0, 2.0, 3.0], [0, 0, 0, 1], "cross completed after a flat stretch"),
        ([1.0, 3.0, 1.0], [0, 1, -1], "plain cross up then down"),
    ],
)
def test_equality_is_handled_without_false_or_missed_crosses(
    fast_values: list[float], expected: list[int], description: str
) -> None:
    """Bars where the two are exactly equal are the awkward case (§14).

    Comparing only against the previous bar gets one of these wrong whichever way
    equality is treated: counting it as the other side invents a cross for a touch
    that fell back, and requiring strict inequality misses a real cross that passes
    through equality. Carrying the last non-zero side forward handles both.
    """
    slow = series([2.0] * len(fast_values))
    fast = series(fast_values)
    assert list(crossed(fast, slow)) == expected, description
    # The scalar path must agree at every bar, not just overall.
    scalar = [
        crossed_at_last(fast.iloc[:i], slow.iloc[:i]) for i in range(1, len(fast_values) + 1)
    ]
    assert scalar == expected, f"scalar disagrees: {description}"


def test_nan_never_manufactures_a_cross() -> None:
    """Otherwise the first warmed bar of every run would look like a signal."""
    fast = series([float("nan"), float("nan"), 2.0, 3.0])
    slow = series([float("nan"), float("nan"), 1.0, 1.0])
    assert list(crossed(fast, slow)) == [0, 0, 0, 0]


def test_the_scalar_and_vectorised_cross_agree_bar_for_bar() -> None:
    """``crossed_at_last`` is an optimisation; it must not change any answer.

    It exists because the vectorised form dominated the replay profile while the
    agent only ever reads the final bar.
    """
    prices = series(list(2400 + np.cumsum(np.random.default_rng(11).normal(0, 1.2, 600))))
    fast, slow = ema(prices, 9), ema(prices, 21)
    vector = crossed(fast, slow)

    mismatches = [
        i
        for i in range(2, len(prices) + 1)
        if crossed_at_last(fast.iloc[:i], slow.iloc[:i]) != int(vector.iloc[i - 1])
    ]
    assert mismatches == []
    assert int((vector != 0).sum()) > 5, "series was too quiet to be a real comparison"


def test_scalar_cross_needs_two_bars() -> None:
    assert crossed_at_last(series([1.0]), series([2.0])) == 0
    assert crossed_at_last(series([]), series([])) == 0
