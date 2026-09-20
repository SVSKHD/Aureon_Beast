"""Volatility context: ATR and session range against history (9B, §19).

Context, never a signal. What these pin is that the numbers mean what their names say: ATR is
Wilder's with a warm-up, the session ratio is against a median rather than a mean, and the
regime label carries the version of the bands that produced it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.config.symbol_tuning import VOLATILITY_BANDS_VERSION, tuning_for
from aureon.engine.volatility import (
    ATR_PERIOD,
    MEDIAN_SESSION_DAYS,
    atr,
    build_context,
    regime_for,
    session_range_median,
    true_range,
)
from aureon.models.base import MarketTime
from aureon.models.enums import Timeframe
from aureon.models.market import Candle

TZ = "Europe/Athens"
T0 = datetime(2026, 9, 16, 7, 0, tzinfo=UTC)
GOLD = tuning_for("XAUUSD")
SILVER = tuning_for("XAGUSD")


def candle(*, minutes: int, low: float, high: float, close: float | None = None) -> Candle:
    moment = T0 + timedelta(minutes=minutes)
    return Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(moment, TZ),
        open=low,
        high=high,
        low=low,
        close=close if close is not None else high,
        tick_volume=100,
    )


def flat_series(count: int, *, span: float = 1.0) -> list[Candle]:
    """``count`` candles each with the same range and no gaps, so ATR is exactly ``span``."""
    return [
        candle(minutes=i * 5, low=2400.0, high=2400.0 + span, close=2400.0)
        for i in range(count)
    ]


# ── ATR ───────────────────────────────────────────────────────────────────────


def test_atr_is_none_before_warm_up() -> None:
    """Thirteen candles have no 14-period ATR, and a partial average would be read as one.

    The property is enforced twice: by ``atr``'s own length guard and by the shared Wilder
    smoother, which returns all-NaN below its warm-up. Reverting either leaves this green,
    which is the honest reading of it -- it pins the behaviour rather than one guard.
    """
    assert atr(flat_series(ATR_PERIOD), ATR_PERIOD) is None
    assert atr([], ATR_PERIOD) is None
    assert atr(flat_series(ATR_PERIOD + 1), ATR_PERIOD) is not None


def test_atr_of_an_unvarying_series_is_that_range() -> None:
    """The arithmetic, checkable by hand: every true range is 1.0, so the average is 1.0."""
    assert atr(flat_series(40, span=1.0), ATR_PERIOD) == pytest.approx(1.0)
    assert atr(flat_series(40, span=2.5), ATR_PERIOD) == pytest.approx(2.5)


def test_true_range_sees_a_gap_the_bar_itself_cannot() -> None:
    """A Monday open is mostly gap, and high-low alone would report a quiet bar."""
    candles = [
        candle(minutes=0, low=2400.0, high=2401.0, close=2400.5),
        candle(minutes=5, low=2410.0, high=2411.0, close=2410.5),
    ]
    ranges = true_range(candles)
    assert ranges.iloc[1] == pytest.approx(10.5)  # 2411.0 - 2400.5, not 1.0


def test_a_nonsense_period_is_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        atr(flat_series(20), 0)


def test_atr_in_points_is_the_price_figure_over_the_tick() -> None:
    """Both are reported: an operator thinks in points, a comparison needs price."""
    context = build_context(flat_series(40, span=1.0), point=0.01, tuning=GOLD)
    assert context.atr_14 == pytest.approx(1.0)
    assert context.atr_points == pytest.approx(100.0)


def test_the_candle_range_is_reported_as_a_fraction_of_atr() -> None:
    """Comparable across instruments and regimes, where a points figure is neither."""
    candles = flat_series(40, span=1.0)
    candles.append(candle(minutes=1000, low=2400.0, high=2402.0, close=2401.0))
    context = build_context(candles, point=0.01, tuning=GOLD)
    assert context.candle_range_pct_of_atr > 1.0


# ── The session ratio and the regime ──────────────────────────────────────────


def test_the_median_ignores_one_news_day_where_a_mean_would_not() -> None:
    """A mean would make every session after a spike look small by comparison."""
    ordinary = [10.0] * 19
    assert session_range_median([*ordinary, 400.0]) == pytest.approx(10.0)


def test_the_median_uses_at_most_twenty_days() -> None:
    long_history = [1.0] * 50 + [10.0] * MEDIAN_SESSION_DAYS
    assert session_range_median(long_history) == pytest.approx(10.0)


def test_no_history_means_no_ratio_rather_than_a_comparison_to_zero() -> None:
    assert session_range_median([]) is None
    assert session_range_median([0.0, 0.0]) is None


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [(0.5, "low"), (0.69, "low"), (0.71, "normal"), (1.0, "normal"), (1.5, "high")],
)
def test_the_regime_reads_off_the_bands(ratio: float, expected: str) -> None:
    assert regime_for(ratio, GOLD) == expected


def test_the_bands_are_dimensionless_so_both_symbols_share_them() -> None:
    """A session at half its usual size means the same thing on gold and on silver."""
    assert (GOLD.low_volatility_ratio, GOLD.high_volatility_ratio) == (
        SILVER.low_volatility_ratio,
        SILVER.high_volatility_ratio,
    )
    assert regime_for(0.5, GOLD) == regime_for(0.5, SILVER)


def test_a_stored_regime_carries_the_version_of_the_bands_that_produced_it() -> None:
    """A label whose definition moved silently makes every comparison across the change
    wrong without appearing to be."""
    session = flat_series(4, span=1.0)
    context = build_context(
        flat_series(40, span=1.0),
        point=0.01,
        tuning=GOLD,
        session_candles=session,
        previous_session_ranges=[10.0] * 20,
    )
    assert context.regime == "low"  # a 1.0 session range against a 10.0 median
    assert context.bands_version == VOLATILITY_BANDS_VERSION


def test_an_absent_regime_carries_no_version() -> None:
    """A version on a label nobody assigned would suggest a judgement was made."""
    context = build_context(flat_series(40), point=0.01, tuning=GOLD)
    assert context.regime is None
    assert context.bands_version is None


def test_the_session_range_comes_from_the_session_not_the_window() -> None:
    """Without the session's own candles the figure is absent, not the 24-hour range."""
    window = flat_series(40, span=1.0)
    session = [
        candle(minutes=0, low=2400.0, high=2405.0, close=2402.0),
        candle(minutes=5, low=2399.0, high=2401.0, close=2400.0),
    ]
    with_session = build_context(
        window, point=0.01, tuning=GOLD, session_candles=session,
        previous_session_ranges=[6.0] * 20,
    )
    assert with_session.session_range == pytest.approx(6.0)  # 2405.0 - 2399.0
    assert with_session.session_range_vs_median == pytest.approx(1.0)
    assert with_session.regime == "normal"

    without = build_context(window, point=0.01, tuning=GOLD)
    assert without.session_range is None
    assert without.session_range_vs_median is None


def test_a_nonsense_point_is_refused_rather_than_dividing_by_zero() -> None:
    with pytest.raises(ValueError, match="positive"):
        build_context(flat_series(40), point=0.0, tuning=GOLD)


# ── Both symbols over their own fixture weeks ─────────────────────────────────


@pytest.mark.parametrize("symbol", ["XAUUSD", "XAGUSD"])
def test_a_context_is_computable_for_either_symbol(
    request: pytest.FixtureRequest, symbol: str
) -> None:
    """The ATR figures differ by instrument; the ratio and regime are comparable (9B)."""
    week = request.getfixturevalue(
        "candles" if symbol == "XAUUSD" else "silver_candles"
    )[:300]
    tuning = tuning_for(symbol)
    context = build_context(
        week,
        point=tuning.point,
        tuning=tuning,
        session_candles=week[-12:],
        previous_session_ranges=[
            max(c.high for c in week[i : i + 12]) - min(c.low for c in week[i : i + 12])
            for i in range(0, 240, 12)
        ],
    )
    assert context.atr_14 is not None and context.atr_14 > 0
    # In points both instruments land in a readable range rather than off by a decade,
    # which is what a shared tick would do to one of them.
    assert 1.0 < context.atr_points < 100_000.0
    assert context.regime in {"low", "normal", "high"}
    assert context.bands_version == VOLATILITY_BANDS_VERSION
