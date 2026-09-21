"""Did the quantiles a `/monitor` screen published actually hold? (9D, §63)

A readout that is never checked is a readout nobody should trust. `/monitor` publishes a
target and a stop as quantiles of what similar detections measured before; this asks, a week
later, whether price reached that target before that stop — and reports the answer as a rate
with its own denominator, exactly as the readout reported its own.

## Scored from the evaluation, not re-derived

The assessment is read as it was stored, and the outcome comes from the detection's own
``detection_evaluations`` row. Nothing here recomputes the cohort or the quantiles: the
question is whether **what was shown** held, not whether a better readout would have held.
An assessment is never written to (decision 195) — this produces a count that lives on the
review.

## Three answers, and "unresolved" is one of them

An evaluation records the maximum favourable and maximum adverse excursion over a horizon.
That answers two of the three cases outright:

* the target was reached and the stop never was → **hit**;
* the stop was reached and the target never was → **miss**.

When **both** were reached, candle data cannot say which came first. ``mfe_at`` and ``mae_at``
are the times of the *extremes*, not of the first touch of a quantile, so ordering by them
would be wrong in a way that looks right. Those go to **unresolved** and are published as
such, for the reason §22 keeps PENDING out of every statistic: counting an unknown either way
is how a readout comes to look better or worse than the record supports.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from aureon.models.assessment import Assessment
from aureon.models.evaluation import DetectionEvaluation

log = logging.getLogger(__name__)

HIT = "hit"
#: The stop's distance was reached and the target's was not.
MISS = "miss"
#: NEITHER distance was reached inside the horizon. Its own bucket rather than folded into
#: MISS: "it went against you" and "it went nowhere" are different things to have learned,
#: and a rate that merged them would hide a readout whose target was simply too far away.
#: It still counts against the hit rate, because the question is "did the target come
#: first", and the answer here is no.
NEITHER = "neither"
#: Both were reached and candle data cannot order them. Excluded from the rate entirely.
UNRESOLVED = "unresolved"
#: The readout published no target or stop at all, so there is nothing to score. Counted
#: separately rather than dropped: how often `/monitor` had to refuse is itself a finding,
#: and one that should get better as history accumulates.
NOT_SCORED = "not_scored"


@dataclass
class AssessmentScores:
    """How the published quantiles fared, over one period."""

    total: int = 0
    hit: int = 0
    miss: int = 0
    neither: int = 0
    unresolved: int = 0
    not_scored: int = 0
    #: cohort key -> (hit, resolved). Per cohort because "it holds when the regime matched"
    #: and "it holds in general" are different claims, and the widening makes the difference
    #: visible: a cohort that had to drop the volatility regime is a weaker statement.
    by_cohort: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def resolved(self) -> int:
        """Everything whose answer is known. UNRESOLVED is not an answer."""
        return self.hit + self.miss + self.neither

    @property
    def hit_rate(self) -> float | None:
        """``None`` on nothing resolved, never 0.0.

        A rate of zero says the target was never reached first. No resolved assessments says
        nothing at all, and rendering them the same way is how an absence becomes a verdict.
        """
        if self.resolved == 0:
            return None
        return self.hit / self.resolved


def cohort_key(assessment: Assessment) -> str:
    """A stable, readable key for the cohort an assessment was measured over.

    Readable because it appears in a review a human reads, and stable because a rate keyed by
    a dict's iteration order would move between runs of the same week.
    """
    wanted = assessment.cohort_filter
    parts = [wanted.agent_name, wanted.direction.value]
    parts.append(wanted.session.value if wanted.session else "any_session")
    if wanted.dropped:
        parts.append("dropped:" + "+".join(sorted(wanted.dropped)))
    else:
        parts.append("exact")
    return "|".join(parts)


def score_one(
    assessment: Assessment, evaluation: DetectionEvaluation | None
) -> str:
    """Whether this readout's target was reached before its stop.

    Measured over the horizon the estimates were measured over -- comparing a p50 taken from
    sixty-minute excursions against a five-candle outcome would be comparing two different
    questions and calling the difference a result.
    """
    if assessment.insufficient or not assessment.tp_estimates or not assessment.sl_estimates:
        return NOT_SCORED
    if evaluation is None:
        return NOT_SCORED

    target = assessment.tp_estimates[0].points
    stop = assessment.sl_estimates[0].points
    horizon_id = _horizon_of(assessment)

    for result in evaluation.complete_horizons:
        if horizon_id is not None and result.horizon_id != horizon_id:
            continue
        if result.mfe is None or result.mae is None:
            continue
        reached_target = result.mfe >= target
        reached_stop = result.mae >= stop
        if reached_target and not reached_stop:
            return HIT
        if reached_stop and not reached_target:
            return MISS
        if reached_target and reached_stop:
            # Both happened inside the horizon and candle data cannot order them.
            return UNRESOLVED
        # Neither distance was reached inside the horizon. The question asked is "did the
        # target come first", so the answer is no -- but it is recorded as its own outcome,
        # because a target nobody got near is a different lesson from one price ran away
        # from.
        return NEITHER
    return NOT_SCORED


def _horizon_of(assessment: Assessment) -> str | None:
    """Which horizon the estimates were measured over.

    Recovered from the stored confirmation table rather than recomputed from the rule: the
    rule's default could change, and this assessment was measured under whatever was in force
    when it was written.
    """
    # The estimates carry no horizon of their own, so the last horizon in the stored table is
    # used -- it is the one `build_assessment` measures over by default, and an assessment
    # stored with an explicit horizon carries that table too.
    if not assessment.confirmations:
        return None
    return assessment.confirmations[-1].horizon_id


def score_assessments(
    assessments: Sequence[Assessment],
    evaluations: dict[str, DetectionEvaluation],
) -> AssessmentScores:
    """Score every readout produced in a period (§63)."""
    scores = AssessmentScores(total=len(assessments))
    for assessment in assessments:
        verdict = score_one(assessment, evaluations.get(assessment.detection_id))
        if verdict == HIT:
            scores.hit += 1
        elif verdict == MISS:
            scores.miss += 1
        elif verdict == NEITHER:
            scores.neither += 1
        elif verdict == UNRESOLVED:
            scores.unresolved += 1
        else:
            scores.not_scored += 1

        if verdict in {HIT, MISS, NEITHER}:
            key = cohort_key(assessment)
            hit, resolved = scores.by_cohort.get(key, (0, 0))
            scores.by_cohort[key] = (hit + (1 if verdict == HIT else 0), resolved + 1)
    return scores
