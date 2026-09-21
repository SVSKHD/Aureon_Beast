"""Building a volume profile from closed candles (9B, §19).

    profile = build_profile(candles, bin_points=10.0, point=0.01, scope="asia")

Context, never a signal. Nothing in this module decides anything; it summarises where
estimated volume sat, and the agents and reviews may record that alongside a detection.

## The estimate, stated plainly

An M5 candle reports one ``tick_volume`` for the bar and says nothing about where inside its
range that volume happened. This spreads it **evenly across the bar's high--low**, which is
the only thing a candle-derived profile can do and is wrong in a knowable direction: a bar
that spent most of its life at one extreme is reported as though it spent it evenly. Real
tick data would answer properly and CLAUDE.md forbids storing ticks, so the honest move is to
name the assumption rather than to hide it behind a plausible-looking number.

Two consequences worth holding on to:

* a **wide** bar contributes a thin layer to many bins, a **narrow** bar a thick layer to
  one. That is the profile's whole shape, and it comes from the assumption rather than from
  the market;
* a doji contributes its entire volume to a single bin, which is the one case where the
  estimate is exactly right.

## Determinism, and why the index is computed in TICKS

Same candles and same ``bin_points`` produce the same profile, bin for bin. Bins are keyed by
an integer index from an absolute origin (price 0), not from the window's own low, so two
scopes over overlapping windows share bin edges and their POCs are comparable.

The index is computed from the price in **ticks**, not from ``price / width`` -- because that
division is wrong at exactly the prices that matter. ``2400.10 / 0.10`` is 24000.999999999996
in binary, so a floor of it puts $2400.10 in the $2400.00 bin: every exact bin edge lands one
bin low, and the shape drifts by a bin in a way no assertion about a POC would reveal.
Rounding the price to whole ticks first makes the arithmetic integral (``240010 // 10``),
which is exact for every price a broker can quote. The first version of this module had the
float bug, and a test asserting that adjacent edges are adjacent bins is what caught it.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Sequence

from aureon.models.base import to_utc
from aureon.models.market import Candle
from aureon.models.profile import (
    MAX_PROFILE_BINS,
    ProfileBin,
    VolumeProfile,
    VolumeProfileRef,
)

log = logging.getLogger(__name__)

#: A node is a bin whose volume stands out against the profile's own scale. Both are
#: fractions of the POC's volume rather than absolute numbers, so they mean the same thing on
#: a quiet session and a busy one -- and both are placeholders, like every other threshold
#: that has not been researched.
HVN_FRACTION_OF_POC = 0.70
LVN_FRACTION_OF_POC = 0.20


def bin_index(price: float, *, point: float, bin_points: float) -> int:
    """Which bin a price falls in, from an absolute origin, computed in ticks.

    ``math.floor`` rather than ``int``: ``int(-0.5)`` is 0, which would put two different
    prices in one bin either side of zero. No traded instrument has a negative price, but a
    function that is wrong there is a function whose reasoning cannot be checked.
    """
    ticks = round(price / point)
    return math.floor(ticks / bin_points)


def bin_price(index: int, *, point: float, bin_points: float) -> float:
    """The bin's lower edge, rounded to the instrument's own precision.

    Rounded because the edge is stored and read by a human: ``24001 * 0.1`` is
    2400.1000000000004, and a document full of those is a document nobody trusts.
    """
    digits = max(0, -math.floor(math.log10(point))) + 1
    return round(index * bin_points * point, digits)


def build_profile(
    candles: Sequence[Candle],
    *,
    bin_points: float,
    point: float,
    scope: str,
    symbol: str | None = None,
    timeframe: object | None = None,
) -> VolumeProfile:
    """Estimated volume by price over these candles (§19).

    ``bin_points`` is in POINTS and ``point`` converts it to price, for the same reason every
    other threshold in Aureon does: 10 points is $0.10 of gold and $0.01 of silver, so a
    shared width would be two different resolutions.

    An empty window is a valid answer, not an error: the Asia profile before Asia opens has
    no candles, and returning an empty profile says that, where raising would make a caller
    invent a placeholder.
    """
    if bin_points <= 0 or point <= 0:
        raise ValueError(f"bin_points and point must be positive, got {bin_points}, {point}")

    first = candles[0] if candles else None
    resolved_symbol = symbol or (first.symbol if first else "")
    resolved_timeframe = timeframe or (first.timeframe if first else None)
    if resolved_timeframe is None:
        raise ValueError("an empty profile needs an explicit timeframe")

    volumes: dict[int, float] = defaultdict(float)
    for candle in candles:
        _spread_candle(candle, point=point, bin_points=bin_points, into=volumes)

    total = sum(volumes.values())
    edge = {
        index: bin_price(index, point=point, bin_points=bin_points) for index in volumes
    }
    bins = tuple(
        ProfileBin(price=edge[index], tick_volume=volume)
        for index, volume in sorted(volumes.items())
    )
    poc_index = max(volumes, key=lambda i: (volumes[i], -i)) if volumes else None
    poc_price = edge[poc_index] if poc_index is not None else None
    va_low, va_high = _value_area(
        volumes, poc_index, total, point=point, bin_points=bin_points
    )

    start = to_utc(candles[0].open_time.utc) if candles else None
    end = to_utc(candles[-1].close_time) if candles else None

    profile = VolumeProfile(
        symbol=resolved_symbol,
        timeframe=resolved_timeframe,
        scope=scope,
        start_utc=start or to_utc(_epoch()),
        end_utc=end or to_utc(_epoch()),
        bin_points=bin_points,
        poc_price=poc_price,
        value_area_low=va_low,
        value_area_high=va_high,
        hvn=_nodes(volumes, above=True, point=point, bin_points=bin_points),
        lvn=_nodes(volumes, above=False, point=point, bin_points=bin_points),
        total_tick_volume=total,
        bins=_capped(bins),
    )
    return profile


def _epoch():
    from datetime import UTC, datetime

    return datetime(1970, 1, 1, tzinfo=UTC)


def _spread_candle(
    candle: Candle, *, point: float, bin_points: float, into: dict[int, float]
) -> None:
    """Spread one candle's tick volume evenly across the bins its range touches.

    Proportional to the **overlap** with each bin, not one equal share per bin: a bar
    covering 1.2 bins would otherwise put as much volume in the sliver as in the bin it
    nearly fills, which would move the POC for a reason that is purely arithmetic.
    """
    volume = float(candle.tick_volume or 0)
    if volume <= 0:
        return
    low, high = candle.low, candle.high
    width = bin_points * point
    if high <= low:
        # A bar with no range at all: its whole volume traded at one price, which is the
        # single case where the even-spreading assumption is exactly right.
        into[bin_index(low, point=point, bin_points=bin_points)] += volume
        return

    span = high - low
    first = bin_index(low, point=point, bin_points=bin_points)
    last = bin_index(high, point=point, bin_points=bin_points)
    for index in range(first, last + 1):
        bin_low = bin_price(index, point=point, bin_points=bin_points)
        overlap = min(high, bin_low + width) - max(low, bin_low)
        if overlap <= 0:
            continue
        into[index] += volume * (overlap / span)


def _value_area(
    volumes: dict[int, float],
    poc_index: int | None,
    total: float,
    *,
    point: float,
    bin_points: float,
) -> tuple[float | None, float | None]:
    """The band around the POC holding ``VALUE_AREA_FRACTION`` of the volume (§19).

    Grown outward from the POC one bin at a time, taking whichever side holds more -- the
    conventional construction. It is deliberately **not** a quantile of the price
    distribution: a value area is a contiguous band by definition, and a quantile would
    happily return a range with a hole in it.
    """
    from aureon.config.symbol_tuning import VALUE_AREA_FRACTION

    if poc_index is None or total <= 0:
        return None, None

    target = total * VALUE_AREA_FRACTION
    low = high = poc_index
    captured = volumes[poc_index]
    while captured < target and (min(volumes) < low or high < max(volumes)):
        below = volumes.get(low - 1, 0.0) if low - 1 >= min(volumes) else None
        above = volumes.get(high + 1, 0.0) if high + 1 <= max(volumes) else None
        if above is None or (below is not None and below >= above):
            low -= 1
            captured += below or 0.0
        else:
            high += 1
            captured += above or 0.0
    # The high edge is the TOP of the highest bin, so the band covers the prices it claims.
    return (
        bin_price(low, point=point, bin_points=bin_points),
        bin_price(high + 1, point=point, bin_points=bin_points),
    )


def _nodes(
    volumes: dict[int, float], *, above: bool, point: float, bin_points: float
) -> tuple[float, ...]:
    """High- or low-volume nodes: bins that stand out against the POC's volume.

    Only bins that are local extremes are reported, so a broad shoulder is one node rather
    than fifteen. Empty bins inside the range count as low-volume nodes -- a gap price
    skipped is the strongest possible version of "little traded here".
    """
    if not volumes:
        return ()
    peak = max(volumes.values())
    if peak <= 0:
        return ()
    threshold = peak * (HVN_FRACTION_OF_POC if above else LVN_FRACTION_OF_POC)
    lo, hi = min(volumes), max(volumes)

    found: list[float] = []
    for index in range(lo, hi + 1):
        value = volumes.get(index, 0.0)
        if (value >= threshold) if above else (value <= threshold):
            left = volumes.get(index - 1, 0.0)
            right = volumes.get(index + 1, 0.0)
            is_extreme = (
                (value >= left and value >= right)
                if above
                else (value <= left and value <= right)
            )
            if is_extreme:
                found.append(bin_price(index, point=point, bin_points=bin_points))
    return tuple(found)


def _capped(bins: tuple[ProfileBin, ...]) -> tuple[ProfileBin, ...]:
    """At most ``MAX_PROFILE_BINS``, keeping the busiest and staying price-ordered.

    A truncation would be worse than a sample: dropping the tail would move the reported
    shape's edges, so the cap keeps the largest bins and says nothing about the rest. The
    totals above are computed before this, so ``total_tick_volume`` still describes the whole
    scope even when the bin list does not.
    """
    if len(bins) <= MAX_PROFILE_BINS:
        return bins
    busiest = sorted(bins, key=lambda b: b.tick_volume, reverse=True)[:MAX_PROFILE_BINS]
    log.info("profile had %d bins; kept the %d busiest", len(bins), MAX_PROFILE_BINS)
    return tuple(sorted(busiest, key=lambda b: b.price))


def reference_for(
    profile: VolumeProfile, price: float
) -> VolumeProfileRef:
    """What a detection at ``price`` records about this profile (9B).

    Computed at the detection's own moment from a profile of already-closed candles, and
    never recomputed: a stored reference must keep saying what was true then, not what a
    fuller profile would say later.
    """
    price_vs_va = None
    if profile.value_area_low is not None and profile.value_area_high is not None:
        if price > profile.value_area_high:
            price_vs_va = "above"
        elif price < profile.value_area_low:
            price_vs_va = "below"
        else:
            price_vs_va = "inside"

    return VolumeProfileRef(
        scope=profile.scope,
        poc_price=profile.poc_price,
        va_high=profile.value_area_high,
        va_low=profile.value_area_low,
        price_vs_va=price_vs_va,
        nearest_lvn=_nearest(profile.lvn, price),
        nearest_hvn=_nearest(profile.hvn, price),
    )


def _nearest(prices: tuple[float, ...], price: float) -> float | None:
    if not prices:
        return None
    return min(prices, key=lambda candidate: (abs(candidate - price), candidate))
