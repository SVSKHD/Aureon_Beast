"""Turning a period's data into a review (§61-§65).

Shared by the session, daily and weekly reviews so all three count the same way. Three
counts differing by which file computed them would be worse than having only one.

## The one rule that governs everything here

**Reached counts come from ``complete_horizons`` only**, and the number of horizons
excluded for being PENDING or INVALID is reported alongside them. A boundary test forbids
this package from touching ``.horizons`` directly, because folding an unknown outcome into
a denominator is the single easiest way to make a strategy look worse than it is -- and
nothing in the resulting numbers would reveal it had happened.

## Determinism

A review is a pure function of the data it aggregates. Re-running it for the same period
must produce a **byte-identical** document, so every collection is sorted before folding
and no wall-clock value enters the result except ``generated_at``, which the caller
supplies.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from aureon.evaluation.context_tags import CONTEXT_TAGS
from aureon.models.assessment import Assessment, TradeNote
from aureon.models.detection import Detection
from aureon.models.enums import (
    ExecutionClassification,
    HorizonStatus,
    LinkType,
    SessionName,
    TradeStatus,
)
from aureon.models.evaluation import DetectionEvaluation, EvaluationRule
from aureon.models.review import (
    DailyReview,
    HorizonOutcome,
    InferredLink,
    ThresholdOutcome,
    WeeklyReview,
)
from aureon.models.session import SessionSummary
from aureon.models.trade import Trade
from aureon.reviews.linking import classify_period, infer_links

log = logging.getLogger(__name__)


@dataclass
class PeriodData:
    """Everything a review aggregates, for one period."""

    detections: list[Detection] = field(default_factory=list)
    evaluations: dict[str, DetectionEvaluation] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    sessions: list[SessionSummary] = field(default_factory=list)
    #: 9D. The readouts produced in this period, and what the trader wrote about its
    #: trades. Both default empty, so a review of a period from before 9D builds exactly
    #: as it did before rather than failing on a missing collection.
    assessments: list[Assessment] = field(default_factory=list)
    notes: dict[str, list[TradeNote]] = field(default_factory=dict)

    def sorted_detections(self) -> list[Detection]:
        return sorted(self.detections, key=lambda d: (d.detected_at.utc, d.detection_id))

    def sorted_trades(self) -> list[Trade]:
        return sorted(self.trades, key=lambda t: (t.open_time.utc, t.trade_id))


@dataclass
class Aggregates:
    """The counted result, before it becomes a document."""

    detections_total: int = 0
    detections_by_agent: dict[str, int] = field(default_factory=dict)
    detections_by_session: dict[SessionName, int] = field(default_factory=dict)
    horizons: list[HorizonOutcome] = field(default_factory=list)
    pending_excluded: int = 0
    invalid_excluded: int = 0
    trades_total: int = 0
    trades_closed: int = 0
    realized_pnl: float = 0.0
    trades_by_session: dict[SessionName, int] = field(default_factory=dict)
    explicit_links: int = 0
    inferred_links: list[InferredLink] = field(default_factory=list)
    classifications: dict[ExecutionClassification, int] = field(default_factory=dict)


def aggregate(
    data: PeriodData,
    rule: EvaluationRule,
    *,
    market_tz: str,
    infer_window_minutes: int,
    classification_threshold: float | None = None,
    classification_horizon: str | None = None,
) -> Aggregates:
    """Count a period (§61, §63, §64)."""
    detections = data.sorted_detections()
    trades = data.sorted_trades()
    result = Aggregates(detections_total=len(detections))

    result.detections_by_agent = dict(
        sorted(Counter(d.agent_name for d in detections).items())
    )
    result.detections_by_session = dict(
        sorted(
            Counter(d.session.session for d in detections).items(),
            key=lambda item: item[0].value,
        )
    )

    result.horizons = _horizon_outcomes(data, rule, result)

    result.trades_total = len(trades)
    result.trades_closed = sum(1 for t in trades if t.status is TradeStatus.CLOSED)
    # Only realised money counts. Including an open trade's floating profit would make the
    # figure change every time the review was regenerated.
    result.realized_pnl = round(
        sum(t.realized_pnl or 0.0 for t in trades if t.status is TradeStatus.CLOSED), 8
    )
    result.trades_by_session = dict(
        sorted(
            Counter(
                _session_for_trade(t, market_tz) for t in trades
            ).items(),
            key=lambda item: item[0].value,
        )
    )

    result.explicit_links = sum(
        1 for t in trades if t.detection_id and t.link_type is LinkType.EXPLICIT
    )
    result.inferred_links = infer_links(
        detections, trades, market_tz=market_tz, window_minutes=infer_window_minutes
    )

    threshold = classification_threshold or rule.thresholds[0]
    horizon_id = classification_horizon or rule.horizons[0].id
    result.classifications = classify_period(
        detections,
        data.evaluations,
        trades,
        result.inferred_links,
        threshold=threshold,
        horizon_id=horizon_id,
    )
    return result


def _session_for_trade(trade: Trade, market_tz: str) -> SessionName:
    from zoneinfo import ZoneInfo

    from aureon.config.sessions import session_for

    return session_for(trade.open_time.utc.astimezone(ZoneInfo(market_tz)))


def _horizon_outcomes(
    data: PeriodData, rule: EvaluationRule, result: Aggregates
) -> list[HorizonOutcome]:
    """Per-horizon reached counts, from COMPLETE horizons only (§22, §61).

    ``pending_excluded`` and ``invalid_excluded`` are accumulated as a side effect, because
    the two must be reported together: a reached-N figure without the count it left out
    invites exactly the misreading Phase 3 exists to prevent.
    """
    outcomes: list[HorizonOutcome] = []

    for horizon in rule.horizons:
        thresholds = {
            key: ThresholdOutcome(threshold=value)
            for key, value in zip(rule.threshold_keys, rule.thresholds, strict=True)
        }
        mfe_values: list[float] = []
        mae_values: list[float] = []
        mfe_first = 0
        mae_first = 0

        for evaluation in _sorted_evaluations(data.evaluations):
            # COMPLETE only. The boundary test forbids reading .horizons here.
            for completed in evaluation.complete_horizons:
                if completed.horizon_id != horizon.id:
                    continue
                for key, entry in thresholds.items():
                    entry.evaluated += 1
                    if completed.reached.get(key):
                        entry.reached += 1
                if completed.mfe is not None:
                    mfe_values.append(completed.mfe)
                if completed.mae is not None:
                    mae_values.append(completed.mae)
                if completed.path.value == "mfe_first":
                    mfe_first += 1
                elif completed.path.value == "mae_first":
                    mae_first += 1

        outcomes.append(
            HorizonOutcome(
                horizon_id=horizon.id,
                thresholds=tuple(thresholds[key] for key in rule.threshold_keys),
                mfe_mean=round(sum(mfe_values) / len(mfe_values), 6) if mfe_values else None,
                mae_mean=round(sum(mae_values) / len(mae_values), 6) if mae_values else None,
                mfe_first=mfe_first,
                mae_first=mae_first,
            )
        )

    # Counted once across all horizons, not per horizon, so the figure means "this much
    # work is still unanswered" rather than something that scales with the rule's shape.
    for evaluation in data.evaluations.values():
        result.pending_excluded += len(evaluation.pending_horizons)
        result.invalid_excluded += len(evaluation.invalid_horizons)
    return outcomes


def _sorted_evaluations(
    evaluations: dict[str, DetectionEvaluation]
) -> list[DetectionEvaluation]:
    return [evaluations[key] for key in sorted(evaluations)]


# ── Building the documents ────────────────────────────────────────────────────


def build_daily_review(
    data: PeriodData,
    rule: EvaluationRule,
    *,
    market_date: str,
    period_start: datetime,
    period_end: datetime,
    market_tz: str,
    infer_window_minutes: int,
    symbol: str | None = None,
    generated_at: datetime | None = None,
) -> DailyReview:
    """One broker trading day (§61)."""
    totals = aggregate(
        data, rule, market_tz=market_tz, infer_window_minutes=infer_window_minutes
    )
    sessions_covered = tuple(
        sorted({s.session for s in data.sessions}, key=lambda s: s.value)
    )
    return DailyReview(
        period_start=period_start,
        period_end=period_end,
        market_tz=market_tz,
        generated_at=generated_at,
        evaluation_rule_id=rule.rule_id,
        symbol=symbol,
        detections_total=totals.detections_total,
        detections_by_agent=totals.detections_by_agent,
        detections_by_session=totals.detections_by_session,
        horizons=tuple(totals.horizons),
        pending_horizons_excluded=totals.pending_excluded,
        invalid_horizons_excluded=totals.invalid_excluded,
        trades_total=totals.trades_total,
        trades_closed=totals.trades_closed,
        realized_pnl=totals.realized_pnl,
        trades_by_session=totals.trades_by_session,
        explicit_links=totals.explicit_links,
        inferred_links=tuple(totals.inferred_links),
        market_date=market_date,
        sessions_covered=sessions_covered,
        notes=_classification_note(totals),
        **_assessment_fields(data),
        **_note_fields(data),
    )


def build_weekly_review(
    data: PeriodData,
    rule: EvaluationRule,
    *,
    iso_year: int,
    iso_week: int,
    period_start: datetime,
    period_end: datetime,
    market_tz: str,
    infer_window_minutes: int,
    daily_review_ids: tuple[str, ...] = (),
    symbol: str | None = None,
    generated_at: datetime | None = None,
) -> WeeklyReview:
    """One trading week, generated after Friday's close (§63)."""
    totals = aggregate(
        data, rule, market_tz=market_tz, infer_window_minutes=infer_window_minutes
    )
    return WeeklyReview(
        period_start=period_start,
        period_end=period_end,
        market_tz=market_tz,
        symbol=symbol,
        generated_at=generated_at,
        evaluation_rule_id=rule.rule_id,
        detections_total=totals.detections_total,
        detections_by_agent=totals.detections_by_agent,
        detections_by_session=totals.detections_by_session,
        horizons=tuple(totals.horizons),
        pending_horizons_excluded=totals.pending_excluded,
        invalid_horizons_excluded=totals.invalid_excluded,
        trades_total=totals.trades_total,
        trades_closed=totals.trades_closed,
        realized_pnl=totals.realized_pnl,
        trades_by_session=totals.trades_by_session,
        explicit_links=totals.explicit_links,
        inferred_links=tuple(totals.inferred_links),
        iso_year=iso_year,
        iso_week=iso_week,
        daily_review_ids=tuple(sorted(daily_review_ids)),
        notes=_classification_note(totals),
        **_assessment_fields(data),
        **_note_fields(data),
    )


def _assessment_fields(data: PeriodData) -> dict[str, object]:
    """How the readouts published in this period fared (§63, 9D).

    Counted onto the review rather than written back onto each assessment: a stored document
    that later said something different from what it said when it was written is exactly the
    failure §21 freezes evaluation rules to avoid (decision 195).
    """
    from aureon.reviews.assessment_scoring import score_assessments

    scores = score_assessments(data.assessments, data.evaluations)
    return {
        "assessments_total": scores.total,
        "assessments_hit": scores.hit,
        "assessments_miss": scores.miss,
        "assessments_neither": scores.neither,
        "assessments_unresolved": scores.unresolved,
        "assessments_not_scored": scores.not_scored,
        "assessment_hit_by_cohort": {
            key: f"{hit}/{resolved}" for key, (hit, resolved) in sorted(scores.by_cohort.items())
        },
    }


def _note_fields(data: PeriodData) -> dict[str, object]:
    """The trader's own words, and the groups they put trades into (9D).

    Copied into the review rather than linked, so a document read next year still says what
    they said at the time. Tags come from the sentence, so the grouping is the trader's
    vocabulary rather than anything Aureon invented -- and nothing here feeds a number: the
    notes sit beside the outcomes, they do not move them.
    """
    by_trade: dict[str, tuple[str, ...]] = {}
    by_tag: dict[str, list[str]] = {}
    for trade in data.sorted_trades():
        found = data.notes.get(trade.trade_id) or []
        if not found:
            continue
        by_trade[trade.trade_id] = tuple(note.text for note in found)
        for note in found:
            for tag in note.tags:
                trades = by_tag.setdefault(tag, [])
                if trade.trade_id not in trades:
                    trades.append(trade.trade_id)
    return {
        "trade_notes": by_trade,
        "trades_by_tag": {tag: tuple(ids) for tag, ids in sorted(by_tag.items())},
    }


def _classification_note(totals: Aggregates) -> str:
    """The §64 comparison, rendered as a stable one-liner.

    ``unknown`` is named explicitly rather than omitted: a reader who cannot see how much
    was unresolved cannot judge the rest.
    """
    parts = [
        f"{name.value}={totals.classifications.get(name, 0)}"
        for name in ExecutionClassification
    ]
    return "execution vs observation: " + ", ".join(parts)


def excluded_summary(data: PeriodData) -> str:
    """How much of the period's evaluation work is still unanswered."""
    pending = sum(len(e.pending_horizons) for e in data.evaluations.values())
    invalid = sum(len(e.invalid_horizons) for e in data.evaluations.values())
    complete = sum(len(e.complete_horizons) for e in data.evaluations.values())
    total = pending + invalid + complete
    if total == 0:
        return "no evaluations in this period"
    return (
        f"{complete} complete, {pending} pending, {invalid} invalid "
        f"({100 * complete / total:.0f}% answered)"
    )


def horizon_status_counts(data: PeriodData) -> dict[HorizonStatus, int]:
    counts = {status: 0 for status in HorizonStatus}
    for evaluation in data.evaluations.values():
        counts[HorizonStatus.COMPLETE] += len(evaluation.complete_horizons)
        counts[HorizonStatus.PENDING] += len(evaluation.pending_horizons)
        counts[HorizonStatus.INVALID] += len(evaluation.invalid_horizons)
    return counts


# ── Grouping outcomes by context tag (§23) ────────────────────────────────────


@dataclass
class TagComparison:
    """One tag's outcomes, split into the detections that carry it and those that do not.

    Both sides are reported, always. A "with" figure alone is unreadable -- 60% reached
    means nothing until you know the "without" number is 30% or 65%.
    """

    tag: str
    horizon_id: str
    threshold_key: str
    with_tag_reached: int = 0
    with_tag_complete: int = 0
    without_tag_reached: int = 0
    without_tag_complete: int = 0

    @property
    def with_tag_rate(self) -> float | None:
        if not self.with_tag_complete:
            return None
        return self.with_tag_reached / self.with_tag_complete

    @property
    def without_tag_rate(self) -> float | None:
        if not self.without_tag_complete:
            return None
        return self.without_tag_reached / self.without_tag_complete

    @property
    def difference(self) -> float | None:
        """``with`` minus ``without``, or None when either side has no data.

        None rather than 0.0: "no difference" is a finding, "nothing to compare" is not.
        """
        left, right = self.with_tag_rate, self.without_tag_rate
        if left is None or right is None:
            return None
        return left - right

    @property
    def comparable(self) -> bool:
        """Whether both sides have enough to be worth reading at all.

        Five is not a statistical threshold -- there is no statistics to be had at these
        counts. It is a floor below which a percentage is actively misleading, because
        one detection moves it by twenty points.
        """
        return self.with_tag_complete >= 5 and self.without_tag_complete >= 5


def compare_by_tag(
    data: PeriodData,
    rule: EvaluationRule,
    *,
    horizon_id: str | None = None,
    threshold: str | None = None,
    tags: Sequence[str] = CONTEXT_TAGS,
) -> list[TagComparison]:
    """Split each tag's population and count reached-N on both sides (§23).

    COMPLETE horizons only, like every other reached count in this package -- a PENDING
    horizon is unknown on both sides of the split, and folding it into either would make
    the comparison say something neither population supports.

    This is a research view and nothing depends on it. With 29 crosses in a week any
    difference here is anecdote; the value is in being able to look at all, rather than
    reading one average over populations that had nothing in common.
    """
    key = threshold or rule.threshold_keys[0]
    target = horizon_id or rule.horizons[0].id

    comparisons = [
        TagComparison(tag=tag, horizon_id=target, threshold_key=key) for tag in tags
    ]
    for evaluation in _sorted_evaluations(data.evaluations):
        completed = next(
            (h for h in evaluation.complete_horizons if h.horizon_id == target), None
        )
        if completed is None:
            continue
        reached = bool(completed.reached.get(key))
        for comparison in comparisons:
            # A tag absent from the dict counts as "without". An evaluation written
            # before tags existed therefore lands on the without side rather than being
            # silently dropped from both -- which would change the denominator without
            # saying so.
            if evaluation.context_tags.get(comparison.tag, False):
                comparison.with_tag_complete += 1
                comparison.with_tag_reached += int(reached)
            else:
                comparison.without_tag_complete += 1
                comparison.without_tag_reached += int(reached)
    return comparisons


def render_tag_comparisons(comparisons: Sequence[TagComparison]) -> str:
    """The §23 comparison as stable text, for a review's notes or a terminal."""
    if not comparisons:
        return "no context comparisons available"
    lines = []
    for comparison in sorted(comparisons, key=lambda c: c.tag):
        with_rate = comparison.with_tag_rate
        without_rate = comparison.without_tag_rate
        left = "—" if with_rate is None else f"{with_rate * 100:.0f}%"
        right = "—" if without_rate is None else f"{without_rate * 100:.0f}%"
        note = "" if comparison.comparable else "  (too few to read)"
        lines.append(
            f"{comparison.tag}: with {left} ({comparison.with_tag_reached}/"
            f"{comparison.with_tag_complete}), without {right} "
            f"({comparison.without_tag_reached}/{comparison.without_tag_complete})"
            f"{note}"
        )
    return "\n".join(lines)
