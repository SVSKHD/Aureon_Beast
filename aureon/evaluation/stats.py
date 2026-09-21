"""The two pieces of arithmetic every measured readout needs (9D).

Both are here rather than inline because both have several defensible definitions, and the
difference between them is money. Pinning the choice in one module with its reasoning
written down is the only way a number published in a Discord embed and the same number in a
weekly review can be the same number.

## Wilson rather than the textbook interval

The normal approximation (``p ± z·sqrt(p(1-p)/n)``) is what most people write, and it is
wrong in exactly the cases that matter here: it gives intervals that run below 0 or above 1,
and it collapses to zero width when ``p`` is 0 or 1 -- so "0 of 30 reached it" would publish
a confident 0% with no uncertainty at all. Wilson stays inside [0, 1] and keeps a sensible
width at the extremes, which is the whole reason the interval is printed.

## One quantile definition, chosen and named

A p75 of forty numbers has at least nine standard definitions and they disagree by a rank or
two -- a dollar or two of gold at these thresholds. This uses **linear interpolation between
the closest ranks** (R's type 7, numpy's default, ``statistics.quantiles(method=
"inclusive")``), so the estimates can be reproduced by anyone with the raw cohort and a
spreadsheet.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

#: 95%, two-sided. Named rather than written as 1.96 at the call site: the interval's
#: coverage is part of what is being published and must not become a magic number.
Z_95 = 1.959963984540054


def wilson_interval(
    successes: int, trials: int, *, z: float = Z_95
) -> tuple[float, float] | None:
    """The Wilson score interval for a proportion, or ``None`` with no trials.

    ``None`` rather than ``(0.0, 1.0)``: "we measured nothing" and "we measured and it could
    be anything" are different statements, and only the caller knows how to say the first.
    """
    if trials <= 0:
        return None
    if successes < 0 or successes > trials:
        raise ValueError(f"successes {successes} outside 0..{trials}")

    p = successes / trials
    z2 = z * z
    denominator = 1.0 + z2 / trials
    centre = (p + z2 / (2 * trials)) / denominator
    spread = (
        z
        / denominator
        * math.sqrt(p * (1.0 - p) / trials + z2 / (4.0 * trials * trials))
    )
    # Rounded before clamping, and both for the same reason. At p=1 the algebra gives
    # exactly 1, but in floating point it gives 0.9999999999999999 -- so an interval that
    # should read "up to 100%" would read "up to 99.99999999999999%", and clamping alone
    # does not catch it because the value is genuinely below 1. Six places is far more
    # precision than a published percentage can use and enough that no real interval is
    # altered.
    low = round(centre - spread, 6)
    high = round(centre + spread, 6)
    return (max(0.0, low), min(1.0, high))


def quantile(values: Sequence[float], q: float) -> float | None:
    """The ``q`` quantile by linear interpolation between closest ranks.

    ``None`` on an empty sequence, for the reason ``wilson_interval`` returns it.
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"quantile must be within 0..1, got {q}")
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return float(ordered[0])

    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    weight = position - low
    return float(ordered[low] * (1.0 - weight) + ordered[high] * weight)


def median(values: Sequence[float]) -> float | None:
    """The p50, by the same definition as every other quantile here.

    Not ``statistics.median``: it averages the middle pair on an even count, which happens
    to agree with this definition -- but "happens to agree" is how two medians in one
    codebase eventually stop agreeing.
    """
    return quantile(values, 0.5)
