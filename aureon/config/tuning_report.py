"""What the thresholds would have selected, measured on real bars (11C, F-4).

Every distance threshold in ``symbol_tuning`` is a placeholder. The module docstring there
says so, and says why it matters: the numbers are **not dimensionless**, so gold's
``min_penetration_points = 5`` is a different instrument-relative quantity on silver, and
nothing downstream can tell a sweep that was one from a sweep that was noise.

This module turns that from a warning into a number. For each threshold it computes the
distribution of **exactly the quantity the agent compares against it** -- read off archived
candles -- and tabulates candidate values against how many bars each would admit. Two rules
make it useful rather than dangerous:

**It measures what the agent measures, or it says nothing.** A report on "wick size" that
computed a slightly different ratio from the one ``WickAgent`` compares would be advice
about a different number, and the advice would look identical. So each measure here is
derived from the agent's own expression, and the agent's module is named beside it.

**It never applies anything.** A threshold change is an ``agent_version`` bump (§12): the
parameters live in ``agent_params_snapshot`` and the version is part of the
``detection_id``, so a value changed without a bump leaves the old detections sitting at
ids the new agent would never produce, with nothing to separate the two populations. That
is a commit a human makes, with the version beside it, and no tool should be able to do it
as a side effect of looking.

## Why "how many bars would it admit" rather than "which value is best"

Because nothing here knows what a good detection is. An outcome rule can say whether a
detection was followed by a favourable excursion, and ``report_outcomes.py`` does; what this
answers is the prior question of whether a threshold is *in the range of the data at all*.
A value above every observed measure selects nothing and a value below the first quartile
selects a third of all bars, and both of those are findings before anybody argues about
which side of the distribution to sit on.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from aureon.config.symbol_tuning import SymbolTuning
from aureon.models.market import Candle

#: Quantiles reported for each measure. Deliberately not a mean: these distributions are
#: skewed (most bars have a small wick and a few have a huge one) and a mean sits where no
#: bar is.
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90, 0.99)


@dataclass(frozen=True)
class Measure:
    """One threshold, and the quantity the agent compares against it."""

    #: The ``SymbolTuning`` field this is about.
    field: str
    #: Where the comparison lives, named so a reader can check the derivation.
    agent: str
    #: A human sentence: what the number is.
    means: str
    #: The quantity, per candle. ``None`` for a bar the agent would not look at.
    of: Callable[[Candle, float], float | None]
    #: True when a bar qualifies by being **at or above** the threshold. False for a
    #: threshold a bar has to stay **under** (``max_close_position``).
    admits_above: bool = True
    #: Units, for the report's header. Points are instrument-relative; ratios are not.
    unit: str = "points"


def _range_points(candle: Candle, point: float) -> float | None:
    total = candle.high - candle.low
    return None if total <= 0 else total / point


def _upper_wick_ratio(candle: Candle, point: float) -> float | None:
    total = candle.high - candle.low
    if total <= 0:
        return None
    return (candle.high - max(candle.open, candle.close)) / total


def _lower_wick_ratio(candle: Candle, point: float) -> float | None:
    total = candle.high - candle.low
    if total <= 0:
        return None
    return (min(candle.open, candle.close) - candle.low) / total


def _wick_body_ratio(candle: Candle, point: float) -> float | None:
    """The larger wick over the body, as ``WickAgent.body_ratio`` computes it.

    A zero body is the interesting case rather than an error: a candle that opened and
    closed at the same price has an infinite ratio, and the agent treats it as qualifying.
    Reported as the largest finite value in the sample would understate it, so those bars
    are counted separately by the caller and left out of the quantiles.
    """
    body = abs(candle.close - candle.open)
    wick = max(
        candle.high - max(candle.open, candle.close),
        min(candle.open, candle.close) - candle.low,
    )
    if body <= 0:
        return None
    return wick / body


def _close_position(candle: Candle, point: float) -> float | None:
    """How far up the range the close sits, 0 at the low and 1 at the high.

    ``WickAgent`` compares the distance from the REJECTED end, so a bar with an upper wick
    is judged on ``close_position`` and one with a lower wick on ``1 - close_position``.
    Reported as the distance from the nearer end, which is the quantity the threshold
    actually bounds whichever way the bar points.
    """
    total = candle.high - candle.low
    if total <= 0:
        return None
    position = (candle.close - candle.low) / total
    return min(position, 1.0 - position)


def _body_points(candle: Candle, point: float) -> float | None:
    """The close's distance from the open, in points.

    Stands in for ``min_close_beyond_points``, whose real quantity is a close beyond a
    LEVEL and so depends on which levels existed at the time. This is the achievable
    per-bar proxy and is labelled as one: a threshold larger than most bars' whole body
    cannot be cleared by a break, whatever the levels were.
    """
    return abs(candle.close - candle.open) / point


def _session_move_points(candle: Candle, point: float) -> float | None:
    """One bar's signed move, in points -- the unit ``flat_points`` is measured in."""
    return abs(candle.close - candle.open) / point


MEASURES: tuple[Measure, ...] = (
    Measure(
        field="min_range_points",
        agent="aureon/agents/wick_agent.py",
        means="the candle's whole high-to-low range",
        of=_range_points,
    ),
    Measure(
        field="min_wick_range_ratio",
        agent="aureon/agents/wick_agent.py",
        means="the LARGER wick as a fraction of the range",
        of=lambda candle, point: (
            None
            if (upper := _upper_wick_ratio(candle, point)) is None
            else max(upper, _lower_wick_ratio(candle, point) or 0.0)
        ),
        unit="fraction of range",
    ),
    Measure(
        field="min_wick_body_ratio",
        agent="aureon/agents/wick_agent.py",
        means="the larger wick over the body (bars with no body excluded)",
        of=_wick_body_ratio,
        unit="ratio",
    ),
    Measure(
        field="max_close_position",
        agent="aureon/agents/wick_agent.py",
        means="how close the close sits to the nearer end of the range",
        of=_close_position,
        admits_above=False,
        unit="fraction of range",
    ),
    Measure(
        field="min_penetration_points",
        agent="aureon/agents/liquidity_agent.py",
        means="the candle's range, as the ceiling on any penetration it could show",
        of=_range_points,
    ),
    Measure(
        field="min_close_beyond_points",
        agent="aureon/agents/breakout_agent.py",
        means="the body: open to close, as a proxy for a close beyond a level",
        of=_body_points,
    ),
    Measure(
        field="flat_points",
        agent="aureon/agents/session_trend_agent.py",
        means="one bar's move, in the unit the session's flat band is measured in",
        of=_session_move_points,
    ),
    Measure(
        field="volume_bin_points",
        agent="aureon/engine/volume_profile.py",
        means="the candle's range, which a bin has to divide to resolve anything",
        of=_range_points,
    ),
)


@dataclass(frozen=True)
class Distribution:
    """One measure over one sample of bars."""

    measure: Measure
    #: The value currently configured for this symbol.
    configured: float
    n: int
    #: How many bars the measure could not be computed for, and why it matters: a measure
    #: skipped on most bars is a measure about a subset, and a quantile over that subset is
    #: not a quantile over the session.
    skipped: int
    quantiles: dict[float, float]
    admitted: int
    #: Every computed value, sorted. Kept so a candidate's share is COUNTED rather than
    #: interpolated back out of the quantiles -- the first version did the latter, which is
    #: circular: it derived the share from the quantile it had just derived from the share.
    values: tuple[float, ...] = ()

    @property
    def share_admitted(self) -> float:
        return 0.0 if self.n == 0 else self.admitted / self.n

    @property
    def verdict(self) -> str:
        """One line: is the configured value inside the data at all?

        Three answers, and the two extremes are the findings. A threshold that admits
        nothing produces no detections and looks exactly like a quiet market; one that
        admits most bars produces a detection on most bars and looks exactly like a busy
        one. Neither is visible from the detections alone.
        """
        if self.n == 0:
            return "no bars"
        share = self.share_admitted
        if self.admitted == 0:
            return "ADMITS NOTHING — no bar in this sample clears it"
        if share >= 0.5:
            return f"admits {share:.0%} of bars — almost anything qualifies"
        if share <= 0.01:
            return f"admits {share:.1%} of bars — close to nothing"
        return f"admits {share:.1%} of bars"


def quantiles_of(values: Sequence[float]) -> dict[float, float]:
    """The reported quantiles, by linear interpolation between closest ranks.

    Type 7 -- numpy's default and ``statistics.quantiles(method="inclusive")`` -- chosen for
    the same reason 9D chose it: it is the one a reader who checks in pandas or R will get.
    """
    if not values:
        return {}
    ordered = sorted(values)
    if len(ordered) == 1:
        return dict.fromkeys(QUANTILES, ordered[0])
    cuts = statistics.quantiles(ordered, n=100, method="inclusive")
    return {q: cuts[max(0, min(98, round(q * 100) - 1))] for q in QUANTILES}


def describe(
    measure: Measure, candles: Sequence[Candle], tuning: SymbolTuning
) -> Distribution:
    """One measure's distribution over ``candles``, against the configured value."""
    configured = float(getattr(tuning, measure.field))
    values: list[float] = []
    skipped = 0
    for candle in candles:
        value = measure.of(candle, tuning.point)
        if value is None:
            skipped += 1
            continue
        values.append(value)
    admitted = sum(
        1
        for value in values
        if (value >= configured if measure.admits_above else value <= configured)
    )
    return Distribution(
        measure=measure,
        configured=configured,
        n=len(values),
        skipped=skipped,
        quantiles=quantiles_of(values),
        admitted=admitted,
        values=tuple(sorted(values)),
    )


def admitted_at(distribution: Distribution, value: float) -> int:
    """How many bars in the sample a threshold of ``value`` would admit. Counted."""
    if distribution.measure.admits_above:
        return sum(1 for one in distribution.values if one >= value)
    return sum(1 for one in distribution.values if one <= value)


def candidates(distribution: Distribution) -> list[tuple[float, int, float]]:
    """``(value, bars admitted, share)`` at each reported quantile.

    The quantiles themselves are the candidate values, which is the point: a threshold set at
    the 75th percentile of the measure admits a quarter of bars **by construction**, and
    reading that off the data is more honest than proposing a round number and discovering
    afterwards what it selected. The share is then counted against the sample rather than
    assumed from the quantile, so a tied distribution -- many bars at the same value, which
    happens at a tick boundary -- reports what it really admits.
    """
    rows: list[tuple[float, int, float]] = []
    for _, value in sorted(distribution.quantiles.items()):
        admitted = admitted_at(distribution, value)
        share = 0.0 if distribution.n == 0 else admitted / distribution.n
        rows.append((value, admitted, share))
    return rows
