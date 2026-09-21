"""Volatility as context: ATR and session range against recent history (9B, §19).

    context = build_context(candles, point=0.01, tuning=tuning_for("XAUUSD"),
                            session_ranges=previous_20_days)

Context, never a signal. Nothing here decides whether to act; it answers "is this candle,
and this session, big by recent standards" so a review can group outcomes by the answer.

## ATR is Wilder's, seeded where every other indicator here is seeded

``atr(candles, 14)`` reuses ``indicators._wilder``, so ATR and RSI share one smoother and one
warm-up convention. A second implementation of the same recursion is how two numbers that
should agree stop agreeing -- and the warm-up matters more than the formula: the first 14
candles have **no** ATR, and reporting one for them (a simple mean, or zero) would put a
confident number where there is not enough history to have one.

## The regime is a label over a measured ratio, and the bands are versioned

``session_range_vs_median`` is this session's high--low over the median of the same session's
range across the previous 20 broker days. The median rather than the mean, because one news
day would drag a mean and make every subsequent session look small by comparison.

``regime`` then reads `low`, `normal` or `high` by the bands in ``symbol_tuning``, and
``bands_version`` is stored with it. A label whose definition moved silently is worse than no
label: every comparison across the change would be wrong without appearing to be. The bands
are **dimensionless ratios**, so they are deliberately not scaled per instrument -- a session
at half its usual size means the same thing on gold and on silver.

## Why a fraction of ATR rather than points

``candle_range_pct_of_atr`` is the candle's range divided by ATR, which is comparable across
instruments and across regimes; the same range in points is neither. The points figure is kept
too (``atr_points``), because an operator reading `/status` thinks in the instrument's own
units and converting in their head is how a threshold gets misread.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Sequence

import pandas as pd

from aureon.config.symbol_tuning import VOLATILITY_BANDS_VERSION, SymbolTuning
from aureon.engine.indicators import _wilder
from aureon.models.market import Candle
from aureon.models.profile import VolatilityContext

log = logging.getLogger(__name__)

ATR_PERIOD = 14
#: How many previous broker days of the same session the median is taken over.
MEDIAN_SESSION_DAYS = 20


def true_range(candles: Sequence[Candle]) -> pd.Series:
    """Wilder's true range: the largest of the three ways a bar can have moved.

    The previous close matters because a gap is real movement that the bar's own high--low
    cannot see -- which on a Monday open is most of what happened.
    """
    highs = pd.Series([c.high for c in candles], dtype="float64")
    lows = pd.Series([c.low for c in candles], dtype="float64")
    closes = pd.Series([c.close for c in candles], dtype="float64")
    previous = closes.shift(1)
    return pd.concat(
        [highs - lows, (highs - previous).abs(), (lows - previous).abs()], axis=1
    ).max(axis=1)


def atr(candles: Sequence[Candle], period: int = ATR_PERIOD) -> float | None:
    """ATR over the last ``period`` candles, in PRICE, or ``None`` before warm-up.

    ``None`` rather than a partial average: with thirteen candles there is no 14-period ATR,
    and a number computed from what is available would be read as one.
    """
    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")
    if len(candles) <= period:
        return None
    ranges = true_range(candles)
    # The first true range is NaN (no previous close), exactly as RSI's first delta is, so
    # the shared smoother's seeding convention applies unchanged.
    smoothed = _wilder(ranges, period)
    value = smoothed.iloc[-1]
    return None if pd.isna(value) else float(value)


def regime_for(
    ratio: float | None, tuning: SymbolTuning
) -> str | None:
    """`low`, `normal` or `high` by this symbol's bands, or ``None`` without a ratio."""
    if ratio is None:
        return None
    if ratio < tuning.low_volatility_ratio:
        return "low"
    if ratio > tuning.high_volatility_ratio:
        return "high"
    return "normal"


def session_range_median(previous_ranges: Sequence[float]) -> float | None:
    """The median of the same session's range over previous days, or ``None``.

    The median, not the mean: one news day would drag a mean and make every session after it
    look small. ``None`` when there is no history at all, so a first session says "unknown"
    rather than comparing itself to zero.
    """
    usable = [r for r in previous_ranges if r is not None and r > 0]
    if not usable:
        return None
    return float(statistics.median(usable[-MEDIAN_SESSION_DAYS:]))


def build_context(
    candles: Sequence[Candle],
    *,
    point: float,
    tuning: SymbolTuning,
    session_candles: Sequence[Candle] | None = None,
    previous_session_ranges: Sequence[float] = (),
) -> VolatilityContext:
    """The volatility context as of the last closed candle (9B).

    ``session_candles`` are the ones belonging to the session in progress; without them the
    session figures are absent rather than guessed from the whole window, which would report
    a 24-hour range as a session's.
    """
    if point <= 0:
        raise ValueError(f"point must be positive, got {point}")

    atr_price = atr(candles, ATR_PERIOD)
    last = candles[-1] if candles else None
    candle_range = (last.high - last.low) if last else None

    session_range = None
    if session_candles:
        session_range = max(c.high for c in session_candles) - min(
            c.low for c in session_candles
        )

    median = session_range_median(previous_session_ranges)
    ratio = None
    if session_range is not None and median:
        ratio = session_range / median
    regime = regime_for(ratio, tuning)

    return VolatilityContext(
        atr_14=atr_price,
        atr_points=None if atr_price is None else atr_price / point,
        candle_range_pct_of_atr=(
            None
            if atr_price is None or candle_range is None or atr_price <= 0
            else candle_range / atr_price
        ),
        session_range=session_range,
        session_range_vs_median=ratio,
        regime=regime,
        # Stamped only when there is a regime to qualify: a version on an absent label
        # would suggest a judgement was made.
        bands_version=None if regime is None else VOLATILITY_BANDS_VERSION,
    )
