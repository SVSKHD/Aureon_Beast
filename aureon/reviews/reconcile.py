"""Reconciling a replay's weekly reviews against the recorded baseline (§61, §63).

Phase 7's gate asks that ``docs/PHASE2_BASELINE.md`` reconcile with the weekly review
generated for the same replay week. That is not documentation hygiene. The baseline counts
detections and outcomes one way -- straight ``Counter`` over the engine's output -- and the
review counts them another: split into half-open market-clock week windows, folded through
``aggregate()``, with reached counts taken from COMPLETE horizons only.

Two independent paths to the same number is the whole point. If the week boundaries were
computed in UTC instead of on the broker clock, or if a horizon that is still PENDING were
folded into a denominator, the totals here would stop matching and this would say so. A
review that merely agreed with itself would prove nothing.

This module deliberately imports neither the engine nor the backfill: it takes detections
and evaluations that have already been produced, so it can be pointed at a replay, at a
fixture, or (in principle) at documents loaded from Firestore.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aureon.models.detection import Detection
from aureon.models.evaluation import DetectionEvaluation, EvaluationRule
from aureon.models.review import WeeklyReview
from aureon.reviews.aggregate import PeriodData, build_weekly_review
from aureon.reviews.linking import DEFAULT_INFER_WINDOW_MINUTES
from aureon.reviews.periods import Period, market_date_of, week_period


def weeks_spanned(detections: list[Detection], market_tz: str) -> list[tuple[int, int]]:
    """Every ISO week the detections fall in, by the **market** clock, in order.

    On the market clock rather than UTC: a 23:30 UTC Sunday detection is already Monday in
    Athens and belongs to the week that is starting, not the one that ended.
    """
    from datetime import date

    weeks = set()
    for detection in detections:
        day = date.fromisoformat(market_date_of(detection.detected_at.utc, market_tz))
        iso = day.isocalendar()
        weeks.add((iso.year, iso.week))
    return sorted(weeks)


def week_reviews(
    detections: list[Detection],
    evaluations: dict[str, DetectionEvaluation],
    rule: EvaluationRule,
    *,
    market_tz: str,
    infer_window_minutes: int = DEFAULT_INFER_WINDOW_MINUTES,
) -> list[tuple[Period, WeeklyReview]]:
    """One weekly review per ISO week the detections span.

    ``generated_at`` is left unset, so two runs over the same detections produce identical
    documents -- the determinism the review contract promises.
    """
    built: list[tuple[Period, WeeklyReview]] = []
    for iso_year, iso_week in weeks_spanned(detections, market_tz):
        period = week_period(iso_year, iso_week, market_tz)
        in_period = [d for d in detections if period.contains(d.detected_at.utc)]
        data = PeriodData(
            detections=in_period,
            evaluations={
                d.detection_id: evaluations[d.detection_id]
                for d in in_period
                if d.detection_id in evaluations
            },
        )
        built.append(
            (
                period,
                build_weekly_review(
                    data,
                    rule,
                    iso_year=iso_year,
                    iso_week=iso_week,
                    period_start=period.start,
                    period_end=period.end,
                    market_tz=market_tz,
                    infer_window_minutes=infer_window_minutes,
                ),
            )
        )
    return built


@dataclass
class Reconciliation:
    """What the reviews say, what the baseline says, and where they disagree."""

    weeks: list[tuple[int, int]] = field(default_factory=list)
    per_week_detections: dict[tuple[int, int], int] = field(default_factory=dict)
    per_week_dates: dict[tuple[int, int], list[str]] = field(default_factory=dict)
    detections_total: int = 0
    baseline_detections_total: int = 0
    complete_by_horizon: dict[str, int] = field(default_factory=dict)
    reached_by_horizon: dict[str, int] = field(default_factory=dict)
    pending_excluded: int = 0
    invalid_excluded: int = 0
    mismatches: list[str] = field(default_factory=list)

    @property
    def reconciled(self) -> bool:
        return not self.mismatches


def reconcile(
    detections: list[Detection],
    evaluations: dict[str, DetectionEvaluation],
    rule: EvaluationRule,
    *,
    market_tz: str,
    by_market_date: dict[str, int],
    complete_by_horizon: dict[str, int],
    pending_total: int,
    invalid_total: int,
    reached_by_horizon: dict[str, int] | None = None,
    threshold_key: str | None = None,
    infer_window_minutes: int = DEFAULT_INFER_WINDOW_MINUTES,
) -> Reconciliation:
    """Compare weekly reviews with baseline counts and list every disagreement.

    Returns rather than raises: the caller decides whether a mismatch is a failing test or a
    line in a generated document. Every check is reported, not just the first, because one
    number being wrong usually means several are and seeing them together says which.
    """
    key = threshold_key or rule.threshold_keys[0]
    result = Reconciliation(
        baseline_detections_total=sum(by_market_date.values()),
    )

    reviews = week_reviews(
        detections,
        evaluations,
        rule,
        market_tz=market_tz,
        infer_window_minutes=infer_window_minutes,
    )

    seen_dates: set[str] = set()
    for _period, review in reviews:
        iso = (review.iso_year, review.iso_week)
        result.weeks.append(iso)
        result.per_week_detections[iso] = review.detections_total
        dates = sorted(d for d in by_market_date if _in_week(d, iso))
        result.per_week_dates[iso] = dates
        result.detections_total += review.detections_total
        result.pending_excluded += review.pending_horizons_excluded
        result.invalid_excluded += review.invalid_horizons_excluded

        expected = sum(by_market_date[d] for d in dates)
        if review.detections_total != expected:
            result.mismatches.append(
                f"{iso[0]}-W{iso[1]:02d}: review counts {review.detections_total} "
                f"detections, baseline days {dates} sum to {expected}"
            )
        overlap = seen_dates & set(dates)
        if overlap:
            # Weeks must tile. An overlap means a detection is counted twice, which no
            # total would reveal on its own.
            result.mismatches.append(
                f"{iso[0]}-W{iso[1]:02d}: market dates {sorted(overlap)} already "
                "counted in an earlier week"
            )
        seen_dates |= set(dates)

        for outcome in review.horizons:
            entry = next(
                (t for t in outcome.thresholds if f"{t.threshold:g}" == key), None
            )
            if entry is None:
                continue
            result.complete_by_horizon[outcome.horizon_id] = (
                result.complete_by_horizon.get(outcome.horizon_id, 0) + entry.evaluated
            )
            result.reached_by_horizon[outcome.horizon_id] = (
                result.reached_by_horizon.get(outcome.horizon_id, 0) + entry.reached
            )

    unclaimed = sorted(set(by_market_date) - seen_dates)
    if unclaimed:
        result.mismatches.append(
            f"baseline market dates {unclaimed} fall in no generated week"
        )
    if result.detections_total != result.baseline_detections_total:
        result.mismatches.append(
            f"reviews total {result.detections_total} detections, baseline totals "
            f"{result.baseline_detections_total}"
        )
    for horizon_id, baseline_complete in sorted(complete_by_horizon.items()):
        got = result.complete_by_horizon.get(horizon_id, 0)
        if got != baseline_complete:
            result.mismatches.append(
                f"horizon {horizon_id}: reviews counted {got} COMPLETE, baseline "
                f"records {baseline_complete}"
            )
    for horizon_id, baseline_reached in sorted((reached_by_horizon or {}).items()):
        got = result.reached_by_horizon.get(horizon_id, 0)
        if got != baseline_reached:
            result.mismatches.append(
                f"horizon {horizon_id}: reviews counted {got} reached at "
                f"threshold {key}, baseline records {baseline_reached}"
            )
    if result.pending_excluded != pending_total:
        result.mismatches.append(
            f"reviews excluded {result.pending_excluded} PENDING horizons, baseline "
            f"records {pending_total}"
        )
    if result.invalid_excluded != invalid_total:
        result.mismatches.append(
            f"reviews excluded {result.invalid_excluded} INVALID horizons, baseline "
            f"records {invalid_total}"
        )
    return result


def _in_week(market_date: str, iso: tuple[int, int]) -> bool:
    from datetime import date

    calendar = date.fromisoformat(market_date).isocalendar()
    return (calendar.year, calendar.week) == iso
