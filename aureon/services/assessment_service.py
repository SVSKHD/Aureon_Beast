"""What the measured record says about a detection (§62-§64, 9D).

Everything here is arithmetic over things that already happened. There is no model, no
forecast and no preference anywhere in this module: it reads a window of closed candles to
say what the market has been doing, reads `detection_evaluations` rows that were already
COMPLETE to say what followed detections of this shape before, and reports both with the
counts attached.

## Two halves, and why they are separate

``read_trend`` needs candles. ``build_assessment`` needs Firestore rows. They are split
because the two callers are different processes: the **observer** has candles and may not
hold a Discord client, and **Discord** may hold no data provider at all -- a boundary test
asserts that by inspecting ``BotContext``'s own annotations. So the observer calls
``read_trend`` and publishes the result into the symbol's ``system_state`` document, and
``/monitor`` reads it back and hands it to ``build_assessment``. Nothing in this file
imports a provider or a broker, and a boundary test keeps it that way.

## Why n rides with every percentage

"Reached +$5 in 60% of cases" from five detections and from three hundred are different
statements, and rendered as "60%" they are the same words. Every rate here carries its n
and its 95% Wilson interval, and a cohort below ``MIN_COHORT`` publishes **no percentage at
all** -- because an interval on n=12 is so wide that printing the point estimate beside it
invites reading the estimate and ignoring the interval.

## Why the cohort widens rather than guesses

A detection's exact context has usually never occurred thirty times. Rather than silently
relaxing the question or refusing every readout, the cohort gives up one dimension at a
time in a fixed order and **says which** (``CohortFilter.dropped``). A cohort that quietly
stopped matching on volatility regime is answering a different question while wearing the
same words, and a reader has no way to notice.

## Why the estimates are not targets

TP is the cohort's measured MFE at p50 and p25; SL its measured MAE at p75 and p90. They
describe what happened to similar detections, and nothing here or downstream ever prefills
them onto an order (§64). The rendering says "measured from n=... · not advice" every time.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from aureon.evaluation.stats import median, quantile, wilson_interval
from aureon.models.assessment import (
    MIN_COHORT,
    WIDENING_ORDER,
    Assessment,
    CohortFilter,
    Estimate,
    HorizonConfirmation,
    PairedOutcome,
    ThresholdConfirmation,
    TrendRead,
)
from aureon.models.base import to_utc, utc_now
from aureon.models.detection import Detection
from aureon.models.enums import (
    Direction,
    HorizonKind,
    PathClassification,
    TrendBias,
)
from aureon.models.evaluation import DetectionEvaluation, EvaluationRule, threshold_key
from aureon.models.identity import new_assessment_id
from aureon.models.market import Candle
from aureon.models.profile import ProfileSummary, VolatilityContext

log = logging.getLogger(__name__)

#: How many closed candles the trend read looks at. Sixty M5 candles is five hours -- long
#: enough to cover a session's shape, short enough that yesterday's move is not being read
#: as today's.
DEFAULT_TREND_CANDLES = 60

#: How many candles on each side a pivot must clear to count as a swing. Two is the smallest
#: window that is not simply "higher than its neighbour", which on M5 gold marks roughly
#: every third candle and would make a structure count meaningless.
SWING_STRENGTH = 2

#: Quantiles published as target and stop estimates. The pairs are asymmetric on purpose:
#: a target at the median of what similar detections actually reached, and a stop beyond
#: three-quarters of what they actually gave back first.
TP_QUANTILES: tuple[float, ...] = (0.5, 0.25)
SL_QUANTILES: tuple[float, ...] = (0.75, 0.9)


# ── a. The trend read ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SwingCounts:
    """Higher/lower highs and lows over a window, as counts rather than a verdict."""

    higher_highs: int = 0
    lower_highs: int = 0
    higher_lows: int = 0
    lower_lows: int = 0

    @property
    def bullish(self) -> int:
        return self.higher_highs + self.higher_lows

    @property
    def bearish(self) -> int:
        return self.lower_highs + self.lower_lows


def swing_counts(
    candles: Sequence[Candle], *, strength: int = SWING_STRENGTH
) -> SwingCounts:
    """Count the structure over a window of closed candles.

    A pivot is a candle whose high clears ``strength`` candles on each side, or whose low is
    below them. Consecutive pivots are then compared to each other -- "higher high" means
    higher than the previous *swing*, not than the previous candle.

    Deliberately the crude definition. A swing is a human reading of a chart and every
    formalisation of one is arbitrary; this one is at least written down, so the count in a
    stored assessment can be reproduced from the same candles by somebody who disagrees.
    """
    highs: list[float] = []
    lows: list[float] = []
    for i in range(strength, len(candles) - strength):
        window = candles[i - strength : i + strength + 1]
        here = candles[i]
        if all(here.high >= other.high for other in window) and any(
            here.high > other.high for other in window
        ):
            highs.append(here.high)
        if all(here.low <= other.low for other in window) and any(
            here.low < other.low for other in window
        ):
            lows.append(here.low)

    # An EQUAL pivot counts as neither. Two swings at the same price is a level being
    # retested, not a market making a lower high -- and the naive `else "lh"` reads a flat
    # range as a downtrend, because every equal high falls into the bearish bucket.
    counts = dict.fromkeys(("hh", "lh", "hl", "ll"), 0)
    for earlier, later in zip(highs, highs[1:], strict=False):
        if later > earlier:
            counts["hh"] += 1
        elif later < earlier:
            counts["lh"] += 1
    for earlier, later in zip(lows, lows[1:], strict=False):
        if later > earlier:
            counts["hl"] += 1
        elif later < earlier:
            counts["ll"] += 1
    return SwingCounts(
        higher_highs=counts["hh"],
        lower_highs=counts["lh"],
        higher_lows=counts["hl"],
        lower_lows=counts["ll"],
    )


def read_trend(
    candles: Sequence[Candle],
    *,
    ema_fast: float | None = None,
    ema_slow: float | None = None,
    ema_fast_earlier: float | None = None,
    session_trend: str | None = None,
    asia_profile: ProfileSummary | None = None,
    volatility: VolatilityContext | None = None,
    minutes_since_opposite_cross: float | None = None,
    point: float = 0.01,
    flat_points: float = 50.0,
    now: datetime | None = None,
) -> TrendRead:
    """What the last N closed candles did, as facts and then a summary of them (9D).

    Called by the **observer**, which has the candles; the result is published into the
    symbol's state document and read back by Discord (decision 194).

    The bias is a plain vote over five directional facts -- EMA relation, EMA slope, swing
    structure, session trend, and price against Asia's value area. A tie is ``SIDEWAYS``,
    which is a real answer rather than a fallback: a window whose evidence points both ways
    is a market that is not trending, and rounding it to the nearer side would be inventing
    a direction out of a tie.

    ``evidence`` carries the raw facts with no adjectives, so a reader who disagrees with
    the summary can see exactly what it was summarising.
    """
    window = list(candles[-DEFAULT_TREND_CANDLES:])
    evidence: list[str] = []
    bullish = bearish = 0

    if ema_fast is not None and ema_slow is not None:
        if ema_fast >= ema_slow:
            bullish += 1
            evidence.append(f"ema fast {ema_fast:.2f} above slow {ema_slow:.2f}")
        else:
            bearish += 1
            evidence.append(f"ema fast {ema_fast:.2f} below slow {ema_slow:.2f}")
    else:
        evidence.append("ema unknown")

    if ema_fast is not None and ema_fast_earlier is not None and point > 0:
        change = (ema_fast - ema_fast_earlier) / point
        evidence.append(
            f"ema fast moved {change:+.0f} points over {len(window)} candles"
        )
        # Flat is a band, not a knife edge: a slope of one point over five hours is noise,
        # and calling it a direction is how a sideways market reads as a trend.
        if change > flat_points:
            bullish += 1
        elif change < -flat_points:
            bearish += 1
    else:
        evidence.append("ema slope unknown")

    swings = swing_counts(window)
    evidence.append(
        f"swings: {swings.higher_highs} higher highs, {swings.higher_lows} higher lows, "
        f"{swings.lower_highs} lower highs, {swings.lower_lows} lower lows"
    )
    if swings.bullish > swings.bearish:
        bullish += 1
    elif swings.bearish > swings.bullish:
        bearish += 1

    if session_trend:
        evidence.append(f"session trend {session_trend}")
        if session_trend == "up":
            bullish += 1
        elif session_trend == "down":
            bearish += 1

    last_close = window[-1].close if window else None
    if asia_profile is not None and last_close is not None:
        low, high = asia_profile.value_area_low, asia_profile.value_area_high
        if low is not None and high is not None:
            if last_close > high:
                bullish += 1
                evidence.append(f"close {last_close:.2f} above asia VA {low:.2f}-{high:.2f}")
            elif last_close < low:
                bearish += 1
                evidence.append(f"close {last_close:.2f} below asia VA {low:.2f}-{high:.2f}")
            else:
                evidence.append(f"close {last_close:.2f} inside asia VA {low:.2f}-{high:.2f}")

    # Recorded, never voted on. A regime says how big moves are, not which way they go, and
    # "how long since the last opposite cross" is a timing fact rather than a direction.
    if volatility is not None and volatility.regime:
        evidence.append(f"volatility regime {volatility.regime}")
    if minutes_since_opposite_cross is not None:
        evidence.append(f"last opposite cross {minutes_since_opposite_cross:g}m ago")
    else:
        evidence.append("no opposite cross in the window")

    if bullish > bearish:
        bias = TrendBias.BULLISH
    elif bearish > bullish:
        bias = TrendBias.BEARISH
    else:
        bias = TrendBias.SIDEWAYS

    return TrendRead(
        bias=bias,
        evidence=tuple(evidence),
        candles=len(window),
        as_of=to_utc(now or utc_now()),
    )


# ── b. The cohort ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CohortMember:
    """One prior detection and its outcomes, paired for the arithmetic below."""

    detection: Detection
    evaluation: DetectionEvaluation


def wick_tag_of(detection: Detection, evaluation: DetectionEvaluation) -> str | None:
    """Which rejection wick, if any, this detection's context carried (9B's tags)."""
    from aureon.evaluation.context_tags import (
        TAG_LOWER_REJECTION_WICK,
        TAG_UPPER_REJECTION_WICK,
    )

    tags = evaluation.context_tags or {}
    if tags.get(TAG_LOWER_REJECTION_WICK):
        return TAG_LOWER_REJECTION_WICK
    if tags.get(TAG_UPPER_REJECTION_WICK):
        return TAG_UPPER_REJECTION_WICK
    return None


def price_vs_va_of(detection: Detection) -> str | None:
    ref = detection.volume_profile_ref
    return None if ref is None else ref.price_vs_va


def regime_of(detection: Detection) -> str | None:
    context = detection.volatility
    return None if context is None else context.regime


def trend_aligned_of(evaluation: DetectionEvaluation) -> bool | None:
    from aureon.evaluation.context_tags import TAG_SESSION_TREND_ALIGNED

    tags = evaluation.context_tags or {}
    if TAG_SESSION_TREND_ALIGNED not in tags:
        return None
    return bool(tags[TAG_SESSION_TREND_ALIGNED])


def filter_for(
    subject: Detection,
    evaluation: DetectionEvaluation | None,
    *,
    dropped: Sequence[str] = (),
) -> CohortFilter:
    """The cohort this detection belongs to, minus whatever has been given up."""
    given_up = tuple(dropped)
    return CohortFilter(
        symbol=subject.symbol,
        agent_name=subject.agent_name,
        direction=subject.direction or Direction.BUY,
        session=subject.session.session,
        trend_aligned=(
            None if evaluation is None else trend_aligned_of(evaluation)
        ),
        volatility_regime=(
            None if "volatility_regime" in given_up else regime_of(subject)
        ),
        price_vs_va=None if "price_vs_va" in given_up else price_vs_va_of(subject),
        wick_tag=(
            None
            if evaluation is None or "wick_tag" in given_up
            else wick_tag_of(subject, evaluation)
        ),
        dropped=given_up,
    )


def matches(member: CohortMember, wanted: CohortFilter) -> bool:
    """Whether a prior detection belongs to this cohort.

    A dimension set to ``None`` on the filter matches everything -- that is what dropping
    one means. A dimension the *member* cannot answer (no profile stored, no tags) fails the
    match rather than passing it: counting an unknown as agreement is how a cohort fills up
    with detections that were never shown to have the property being asked about.
    """
    detection = member.detection
    if detection.symbol != wanted.symbol:
        return False
    if detection.agent_name != wanted.agent_name:
        return False
    if detection.direction != wanted.direction:
        return False
    if wanted.session is not None and detection.session.session != wanted.session:
        return False
    if wanted.trend_aligned is not None:
        if trend_aligned_of(member.evaluation) is not wanted.trend_aligned:
            return False
    if wanted.volatility_regime is not None:
        if regime_of(detection) != wanted.volatility_regime:
            return False
    if wanted.price_vs_va is not None:
        if price_vs_va_of(detection) != wanted.price_vs_va:
            return False
    if wanted.wick_tag is not None:
        if wick_tag_of(detection, member.evaluation) != wanted.wick_tag:
            return False
    return True


def select_cohort(
    subject: Detection,
    subject_evaluation: DetectionEvaluation | None,
    population: Sequence[CohortMember],
) -> tuple[list[CohortMember], CohortFilter]:
    """The narrowest cohort of at least ``MIN_COHORT``, and what it cost to get there.

    Widens one dimension at a time in ``WIDENING_ORDER`` and stops at the first cohort that
    is large enough. If even the fully-widened cohort is too small, returns it anyway with
    every dimension listed as dropped -- the caller reports "insufficient history (n=...)",
    which is a more useful answer than an empty one.
    """
    dropped: list[str] = []
    while True:
        wanted = filter_for(subject, subject_evaluation, dropped=dropped)
        found = [m for m in population if matches(m, wanted)]
        if len(found) >= MIN_COHORT or len(dropped) == len(WIDENING_ORDER):
            return found, wanted
        dropped.append(WIDENING_ORDER[len(dropped)])


# ── c. Confirmation per horizon ───────────────────────────────────────────────


def confirmations(
    cohort: Sequence[CohortMember], rule: EvaluationRule
) -> tuple[HorizonConfirmation, ...]:
    """How often the cohort reached each threshold, per horizon, with n and intervals."""
    results: list[HorizonConfirmation] = []
    for horizon in rule.horizons:
        complete = [
            result
            for member in cohort
            for result in member.evaluation.complete_horizons
            if result.horizon_id == horizon.id
        ]
        thresholds: list[ThresholdConfirmation] = []
        for threshold in rule.thresholds:
            key = threshold_key(threshold)
            reached = [r for r in complete if r.reached.get(key)]
            times = [
                r.time_to[key]
                for r in reached
                if r.time_to.get(key) is not None
            ]
            interval = wilson_interval(len(reached), len(complete))
            thresholds.append(
                ThresholdConfirmation(
                    threshold=threshold,
                    reached=len(reached),
                    evaluated=len(complete),
                    ci_low=None if interval is None else interval[0],
                    ci_high=None if interval is None else interval[1],
                    median_seconds_to_first=median(times),
                )
            )
        results.append(
            HorizonConfirmation(
                horizon_id=horizon.id,
                evaluated=len(complete),
                thresholds=tuple(thresholds),
                # Ambiguous paths are excluded rather than counted either way: both
                # thresholds were first crossed inside one candle, so the order was never
                # observed and asserting one would be inventing it (§23).
                mae_first=sum(
                    1
                    for r in complete
                    if r.path is PathClassification.MAE_FIRST and not r.path_ambiguous
                ),
                path_ambiguous=sum(1 for r in complete if r.path_ambiguous),
            )
        )
    return tuple(results)


# ── d. Estimates ──────────────────────────────────────────────────────────────


def excursions(
    cohort: Sequence[CohortMember], horizon_id: str
) -> tuple[list[float], list[float]]:
    """The cohort's measured MFE and MAE at one horizon, in points."""
    mfe: list[float] = []
    mae: list[float] = []
    for member in cohort:
        for result in member.evaluation.complete_horizons:
            if result.horizon_id != horizon_id:
                continue
            if result.mfe is not None:
                mfe.append(result.mfe)
            if result.mae is not None:
                mae.append(result.mae)
    return mfe, mae


def estimates(
    values: Sequence[float],
    quantiles: Sequence[float],
    *,
    reference: float | None,
    point: float,
    favourable: bool,
    direction: Direction,
) -> tuple[Estimate, ...]:
    """Quantiles of a measured excursion, in points and as a price on this detection.

    The price is the points applied to the detection's own reference in the direction the
    excursion was measured: a favourable excursion on a BUY is above the reference, an
    adverse one below. Getting that sign wrong would publish a stop above the entry, which
    reads as a plausible number and is the wrong side of the market.
    """
    built: list[Estimate] = []
    for q in quantiles:
        points = quantile(values, q)
        if points is None:
            continue
        price: float | None = None
        if reference is not None and point > 0:
            up = (direction is Direction.BUY) == favourable
            price = round(reference + (points * point if up else -points * point), 5)
        built.append(Estimate(quantile=q, points=round(points, 2), price=price))
    return tuple(built)


def paired_outcome(
    cohort: Sequence[CohortMember],
    horizon_id: str,
    *,
    favourable: float,
    adverse: float,
) -> PairedOutcome:
    """How often the cohort reached +T before -S, for one configured pair.

    The stat a trader actually asks for, and the one a reached-rate alone cannot answer: a
    threshold reached after a drawdown they would have been stopped out of is not an outcome
    they could have had. Ambiguous paths are excluded from the numerator AND the
    denominator, because for those the order was never observed at all.
    """
    key = threshold_key(favourable)
    evaluated = 0
    first = 0
    for member in cohort:
        for result in member.evaluation.complete_horizons:
            if result.horizon_id != horizon_id or result.path_ambiguous:
                continue
            evaluated += 1
            if result.reached.get(key) and result.path is not PathClassification.MAE_FIRST:
                first += 1
    interval = wilson_interval(first, evaluated)
    return PairedOutcome(
        favourable=favourable,
        adverse=adverse,
        favourable_first=first,
        evaluated=evaluated,
        ci_low=None if interval is None else interval[0],
        ci_high=None if interval is None else interval[1],
    )


# ── The readout ───────────────────────────────────────────────────────────────


def build_assessment(
    subject: Detection,
    *,
    trend: TrendRead,
    rule: EvaluationRule,
    population: Sequence[CohortMember],
    subject_evaluation: DetectionEvaluation | None = None,
    estimate_horizon: str | None = None,
    pair: tuple[float, float] | None = None,
    point: float = 0.01,
    now: datetime | None = None,
) -> Assessment:
    """One measured readout, ready to store and to render (9D).

    Takes the trend read rather than deriving it, because deriving it needs candles and the
    caller may be the Discord process (decision 194). Takes the population rather than a
    repository for the same class of reason: this function is arithmetic, and arithmetic is
    testable without a database.
    """
    cohort, wanted = select_cohort(subject, subject_evaluation, population)
    horizon_id = estimate_horizon or default_estimate_horizon(rule)
    reference = subject.price

    mfe, mae = excursions(cohort, horizon_id)
    favourable, adverse = pair or (rule.thresholds[0], rule.thresholds[0])
    direction = subject.direction or Direction.BUY

    disagrees = (
        subject.direction is not None
        and trend.bias is not TrendBias.SIDEWAYS
        and (trend.bias is TrendBias.BULLISH) != (subject.direction is Direction.BUY)
    )

    return Assessment(
        assessment_id=new_assessment_id(),
        detection_id=subject.detection_id,
        symbol=subject.symbol,
        rule_id=rule.rule_id,
        trend_read=trend,
        cohort_filter=wanted,
        n=len(cohort),
        # No percentage at all below the floor. An interval on n=12 is so wide that
        # printing the point estimate beside it invites reading the estimate and ignoring
        # the interval, so nothing is printed rather than something misleading.
        horizons=confirmations(cohort, rule) if len(cohort) >= MIN_COHORT else (),
        tp_estimates=(
            estimates(
                mfe,
                TP_QUANTILES,
                reference=reference,
                point=point,
                favourable=True,
                direction=direction,
            )
            if len(cohort) >= MIN_COHORT
            else ()
        ),
        sl_estimates=(
            estimates(
                mae,
                SL_QUANTILES,
                reference=reference,
                point=point,
                favourable=False,
                direction=direction,
            )
            if len(cohort) >= MIN_COHORT
            else ()
        ),
        paired=(
            paired_outcome(cohort, horizon_id, favourable=favourable, adverse=adverse)
            if len(cohort) >= MIN_COHORT
            else None
        ),
        insufficient=len(cohort) < MIN_COHORT,
        disagrees_with_detection=disagrees,
        created_at=to_utc(now or utc_now()),
    )


def default_estimate_horizon(rule: EvaluationRule) -> str:
    """Which horizon the TP/SL quantiles are measured over, when nobody says.

    The last COUNTED horizon (candles or minutes), not simply the last one. The rule's last
    horizon is ``opposite_cross``, which is event-driven: it stays PENDING until the EMAs
    cross back, which on a trending day is hours away and on a quiet one may be never. A
    default that landed there produced a readout with a cohort of thirty-five and **no
    estimates at all**, every row reading 0/0 -- a screen that looks like a measurement of a
    market rather than a measurement of the wrong column.
    """
    counted = [
        h for h in rule.horizons if h.kind in {HorizonKind.CANDLES, HorizonKind.MINUTES}
    ]
    return (counted or list(rule.horizons))[-1].id


def population_from(
    detections: Sequence[Detection],
    evaluations: dict[str, DetectionEvaluation],
    *,
    before: datetime,
) -> list[CohortMember]:
    """Pair detections with their evaluations, keeping only what was already known.

    ``before`` is the detection being assessed: a cohort containing detections made *after*
    it would be answering the question with information that did not exist when the question
    was asked. That is the same look-ahead §21 keeps out of the evaluations themselves, and
    it is far easier to introduce here, where the rows are simply sitting in a collection.
    """
    cutoff = to_utc(before)
    members: list[CohortMember] = []
    for detection in detections:
        if to_utc(detection.detected_at.utc) >= cutoff:
            continue
        evaluation = evaluations.get(detection.detection_id)
        if evaluation is None:
            continue
        if not evaluation.complete_horizons:
            # A PENDING horizon is excluded from every statistic rather than counted as a
            # miss (§22). A detection with nothing complete contributes nothing at all.
            continue
        members.append(CohortMember(detection=detection, evaluation=evaluation))
    return members


__all__ = [
    "DEFAULT_TREND_CANDLES",
    "SL_QUANTILES",
    "SWING_STRENGTH",
    "TP_QUANTILES",
    "CohortMember",
    "SwingCounts",
    "build_assessment",
    "default_estimate_horizon",
    "confirmations",
    "estimates",
    "excursions",
    "filter_for",
    "matches",
    "paired_outcome",
    "population_from",
    "read_trend",
    "select_cohort",
    "swing_counts",
]
