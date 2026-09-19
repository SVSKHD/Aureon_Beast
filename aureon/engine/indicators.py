"""Pure indicator functions (§14).

Deterministic and stateless: same input series, same output. No clock, no config,
no cached state between calls.

## The window-dependence trap (read before changing anything here)

Both EMA and Wilder's RSI are **recursive**: each value depends on the previous
one, which depends on the one before, all the way back to the first bar in the
series. Their value at the last bar therefore depends on *how much history was
passed in*, not just on the recent bars.

That has a direct consequence for replay/live parity (§82). The live engine holds
a rolling window; the replay engine walks history. If those two hand an agent
windows of different length, the indicators differ, the crossover lands on a
different candle, and the parity test fails -- for a reason that looks like a bug
in the agent and is not.

So **window length is part of the parity contract**, not a performance knob:
``AnalysisEngine`` uses one fixed window size for both paths. The seed's influence
decays as ``(1 - alpha)^n`` but never reaches zero, and at realistic gold prices
it is still worth points after only a couple of periods -- which is why
``min_warmup`` requires ``slow_period * 3`` bars before a cross may be confirmed,
and why the engine's window is sized from it rather than guessed.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

# A cross is only trustworthy once the seed's influence has decayed. Expressed as
# a multiple of the slow period (§14).
WARMUP_PERIODS = 3


def min_warmup(slow_period: int) -> int:
    """Minimum bars before a cross from this slow period may be confirmed."""
    if slow_period <= 0:
        raise ValueError(f"slow_period must be positive, got {slow_period}")
    return slow_period * WARMUP_PERIODS


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average.

    Uses ``adjust=False``: the classic recursive form
    ``ema[i] = alpha * x[i] + (1 - alpha) * ema[i-1]``, seeded with the first
    value, which is what a trading terminal computes. ``adjust=True`` would give a
    different (re-weighted) series and would not match the broker's chart.

    The first ``period - 1`` values are returned as NaN rather than as a
    partially-warmed number, so a caller cannot mistake an unwarmed value for a
    real one.
    """
    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")
    if series.empty:
        return pd.Series(dtype="float64", index=series.index)

    values = pd.to_numeric(series, errors="raise").astype("float64")
    out = values.ewm(span=period, adjust=False, ignore_na=False).mean()
    # Mask the stretch that has not seen `period` observations yet.
    out.iloc[: min(period - 1, len(out))] = float("nan")
    return out


def rsi(series: pd.Series, period: int) -> pd.Series:
    """Relative strength index, Wilder's smoothing.

    Wilder's original definition: seed with the simple mean of the first
    ``period`` gains and losses, then smooth recursively with ``1/period``. This is
    what MT5 and most charting packages show; a plain rolling mean would give
    visibly different values and make stored RSI context incomparable with the
    broker's chart.

    Returns NaN for the first ``period`` bars, and 100 where there are no losses
    in the window (an all-up stretch), which is the conventional limit.
    """
    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")
    if len(series) <= period:
        return pd.Series([float("nan")] * len(series), index=series.index, dtype="float64")

    values = pd.to_numeric(series, errors="raise").astype("float64")
    delta = values.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)

    # Wilder's smoothing is an EMA with alpha = 1/period, seeded on the simple
    # mean of the first `period` deltas. `adjust=False` with that seed reproduces
    # it exactly.
    avg_gain = _wilder(gains, period)
    avg_loss = _wilder(losses, period)

    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    # No losses at all -> RS is infinite -> RSI is 100 by convention. Guarding
    # this explicitly avoids a divide-by-zero warning and a NaN where the answer
    # is well defined.
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    out.iloc[: min(period, len(out))] = float("nan")
    return out


def _wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder-smoothed average of a non-negative series.

    The recursion runs over a NumPy array rather than the Series. It has to be an
    explicit loop -- ``ewm()`` cannot be seeded at an arbitrary offset, and seeding
    it from index 0 would silently change every value -- but per-element ``.iloc``
    access on a Series costs microseconds each, and this is called once per candle
    for every replay of every test. Over a week of M5 that was the single largest
    cost in the parity suite.
    """
    values = series.to_numpy(dtype="float64", copy=False)
    out = np.full(values.shape, np.nan, dtype="float64")
    if len(values) <= period:
        return pd.Series(out, index=series.index, dtype="float64")

    # Seed on the simple mean of the first `period` deltas (index 1..period; index 0
    # is the NaN produced by diff()).
    prev = float(values[1 : period + 1].mean())
    out[period] = prev
    alpha = 1.0 / period
    for i in range(period + 1, len(values)):
        prev += alpha * (values[i] - prev)
        out[i] = prev
    return pd.Series(out, index=series.index, dtype="float64")


def crossed(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """Where ``fast`` crossed ``slow``: +1 bullish, -1 bearish, 0 otherwise.

    A cross is a **change in the sign of ``fast - slow``, ignoring bars where the two
    are exactly equal**. Comparing only against the immediately preceding bar is not
    enough, and both ways of doing it are wrong in a different case:

    * treating "equal" on the previous bar as *the other side* reports a cross for
      ``fast = 1, 2, 2, 1`` against ``slow = 2`` -- fast touched slow from below and
      fell back without ever crossing it;
    * requiring a strict inequality on the previous bar *misses* ``fast = 3, 2, 1``
      against ``slow = 2``, where a genuine cross passes exactly through equality.

    Carrying the last non-zero side forward handles both. Bars where either input is
    NaN yield 0: an indicator that has no value yet cannot have crossed, and treating
    NaN as "below" would manufacture a cross on the first warmed bar of every run.
    """
    diff = fast - slow
    side = pd.Series(0, index=fast.index, dtype="float64")
    side[diff > 0] = 1.0
    side[diff < 0] = -1.0
    side[diff.isna()] = float("nan")

    # Last non-zero, non-NaN side strictly before each bar.
    known = side.replace(0.0, None).ffill().shift(1)

    out = pd.Series(0, index=fast.index, dtype="int64")
    out[(side == 1.0) & (known == -1.0)] = 1
    out[(side == -1.0) & (known == 1.0)] = -1
    return out


def crossed_at_last(fast: pd.Series, slow: pd.Series) -> int:
    """Whether a cross occurred on the FINAL bar: +1 bullish, -1 bearish, 0 none.

    Same rule as ``crossed()``, but it only looks at the last two bars instead of
    building a full boolean Series the caller then discards. An agent is asked about
    one newly-closed candle at a time, so ``crossed()`` was doing ~1800 bars of
    vectorised work per call to answer a question about two of them -- it dominated
    the replay profile.

    ``crossed()`` is kept for batch analysis over a whole history, and
    ``test_indicators.py`` asserts the two agree bar for bar.
    """
    if len(fast) < 2 or len(slow) < 2:
        return 0
    current = float(fast.iloc[-1]) - float(slow.iloc[-1])
    if math.isnan(current) or current == 0.0:
        return 0
    current_side = 1 if current > 0 else -1

    # Walk back to the last bar where the two were NOT equal. Usually that is the
    # immediately preceding bar, so this is O(1) in practice; a long flat stretch is
    # the only case that walks, and it must, or a cross through equality is missed.
    for i in range(len(fast) - 2, -1, -1):
        previous = float(fast.iloc[i]) - float(slow.iloc[i])
        if math.isnan(previous):
            return 0  # unwarmed: no cross can be claimed
        if previous == 0.0:
            continue
        previous_side = 1 if previous > 0 else -1
        return current_side if previous_side != current_side else 0
    return 0
