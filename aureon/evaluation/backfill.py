"""Replay detections and evaluate their outcomes (§22, Phase 3).

Shared by ``scripts/backfill_evaluations.py``, ``scripts/report_evaluations.py`` and
the baseline generator, so the three cannot drift apart and quietly disagree about
what the numbers mean.

## Ordering

Per closed candle, in this order:

1. **advance** every open evaluation with the candle;
2. **notify** the tracker of any new detections, closing ``opposite_cross`` horizons
   they end;
3. **track** the new detections, so they start measuring from the *next* candle.

A detection is never measured against its own candle -- that bar is already history
when the detection becomes known -- and the tracker enforces that independently, so
the ordering here is for clarity rather than correctness.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from aureon.agents.base_agent import BaseAgent
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.evaluation.outcome_tracker import OutcomeTracker
from aureon.models.detection import Detection
from aureon.models.enums import HorizonStatus
from aureon.models.evaluation import DetectionEvaluation, EvaluationRule, threshold_key
from aureon.models.market import Candle


@dataclass
class BackfillResult:
    """Everything one backfill produced."""

    detections: list[Detection] = field(default_factory=list)
    evaluations: list[DetectionEvaluation] = field(default_factory=list)

    @property
    def evaluated(self) -> int:
        return len(self.evaluations)

    @property
    def skipped_context_only(self) -> int:
        """Detections with no direction, which have no favourable side to measure."""
        return sum(1 for d in self.detections if d.direction is None)


def run_backfill(
    candles: Sequence[Candle],
    agents: Sequence[BaseAgent],
    rule: EvaluationRule,
    *,
    account_scope: str,
    market_tz: str,
    point: float = 0.01,
) -> BackfillResult:
    """Replay candles, produce detections, and evaluate their outcomes."""
    engine = AnalysisEngine(
        list(agents), account_scope=account_scope, market_tz=market_tz
    )
    tracker = OutcomeTracker(rule, market_tz=market_tz, point=point)

    result = BackfillResult()
    for candle in candles:
        produced = engine.on_closed_candle(candle)
        tracker.on_closed_candle(candle)
        for detection in produced:
            tracker.on_detection(detection)
            tracker.track(detection)
            result.detections.append(detection)

    result.evaluations = tracker.all_evaluations()
    return result


@dataclass
class ThresholdReport:
    """Reached counts for one threshold, from COMPLETE horizons only."""

    threshold: float
    reached: int = 0
    evaluated: int = 0

    @property
    def rate(self) -> float | None:
        """Hit rate, or ``None`` when nothing completed.

        ``None`` rather than 0.0: a rate of zero asserts the threshold was never
        reached, which is a finding. No data is not a finding.
        """
        return self.reached / self.evaluated if self.evaluated else None


@dataclass
class HorizonReport:
    """One horizon's outcome counts, with the excluded work reported alongside."""

    horizon_id: str
    thresholds: dict[str, ThresholdReport] = field(default_factory=dict)
    complete: int = 0
    pending: int = 0
    invalid: int = 0
    mfe_first: int = 0
    mae_first: int = 0
    path_none: int = 0
    path_ambiguous: int = 0

    @property
    def total(self) -> int:
        return self.complete + self.pending + self.invalid


def build_report(
    evaluations: Sequence[DetectionEvaluation], rule: EvaluationRule
) -> dict[str, HorizonReport]:
    """Aggregate evaluations per horizon.

    Reached counts come from ``complete_horizons`` ONLY. ``pending`` and ``invalid``
    are counted separately and never folded into a denominator: a horizon whose answer
    is unknown is not a miss, and counting it as one is the easiest way to make a
    strategy look worse than it is.
    """
    reports: dict[str, HorizonReport] = {
        horizon.id: HorizonReport(
            horizon_id=horizon.id,
            thresholds={
                threshold_key(t): ThresholdReport(threshold=t) for t in rule.thresholds
            },
        )
        for horizon in rule.horizons
    }

    for evaluation in evaluations:
        # Counting the non-complete ones needs the raw tuple; it is deliberately the
        # only place that reads it, and it never contributes to a reached count.
        for result in evaluation.horizons:
            report = reports.get(result.horizon_id)
            if report is None:
                continue
            if result.status is HorizonStatus.PENDING:
                report.pending += 1
            elif result.status is HorizonStatus.INVALID:
                report.invalid += 1

        for result in evaluation.complete_horizons:
            report = reports.get(result.horizon_id)
            if report is None:
                continue
            report.complete += 1
            if result.path_ambiguous:
                report.path_ambiguous += 1
            match result.path.value:
                case "mfe_first":
                    report.mfe_first += 1
                case "mae_first":
                    report.mae_first += 1
                case _:
                    report.path_none += 1
            for key, threshold_report in report.thresholds.items():
                threshold_report.evaluated += 1
                if result.reached.get(key):
                    threshold_report.reached += 1

    return reports


def format_report(reports: dict[str, HorizonReport], rule: EvaluationRule) -> str:
    """Render the report as plain text for a terminal."""
    keys = list(rule.threshold_keys)
    header = (
        f"{'horizon':<16}{'complete':>9}{'pending':>9}{'invalid':>9}  "
        + "".join(f"{'reach ' + k:>12}" for k in keys)
    )
    lines = [
        f"rule: {rule.rule_id}  (reference={rule.reference_price.value}, "
        f"thresholds in points={list(rule.thresholds)})",
        "",
        "Reached counts use COMPLETE horizons ONLY. Pending and invalid are shown",
        "separately and are never counted as misses.",
        "",
        header,
        "-" * len(header),
    ]
    for horizon in rule.horizons:
        report = reports[horizon.id]
        cells = []
        for key in keys:
            threshold_report = report.thresholds[key]
            rate = threshold_report.rate
            cells.append(
                f"{threshold_report.reached:>5}/{threshold_report.evaluated:<4}"
                if rate is None
                else f"{threshold_report.reached:>4}/{threshold_report.evaluated:<3}"
                f"{rate * 100:>4.0f}%"
            )
        lines.append(
            f"{report.horizon_id:<16}{report.complete:>9}{report.pending:>9}"
            f"{report.invalid:>9}  " + "".join(f"{c:>12}" for c in cells)
        )

    lines += ["", "Path classification (COMPLETE only):", ""]
    for horizon in rule.horizons:
        report = reports[horizon.id]
        lines.append(
            f"  {report.horizon_id:<16} MFE_FIRST {report.mfe_first:>4}   "
            f"MAE_FIRST {report.mae_first:>4}   NONE {report.path_none:>4}   "
            f"ambiguous {report.path_ambiguous:>4}"
        )

    warnings = diagnose(reports, rule)
    if warnings:
        lines += ["", "DIAGNOSTICS", ""]
        lines += [f"  ! {w}" for w in warnings]
    return "\n".join(lines)


# A metric that is ~always true measures nothing, so flag it rather than let a wall
# of 100% read as a strong result.
SATURATION_RATE = 0.95
AMBIGUITY_RATE = 0.50


def diagnose(reports: dict[str, HorizonReport], rule: EvaluationRule) -> list[str]:
    """Flag results that are technically correct but carry no information.

    A reached-rate pinned at 100% across every threshold, or a path classification
    that is mostly unobservable, means the thresholds are too small relative to the
    instrument's movement -- not that the strategy is perfect. Surfacing that here is
    the difference between a report that misleads and one that says what it knows.
    """
    warnings: list[str] = []

    saturated = [
        key
        for key in rule.threshold_keys
        if all(
            (report.thresholds[key].rate or 0.0) >= SATURATION_RATE
            for report in reports.values()
            if report.thresholds[key].evaluated
        )
        and any(report.thresholds[key].evaluated for report in reports.values())
    ]
    if saturated:
        warnings.append(
            f"thresholds {saturated} are reached >={SATURATION_RATE:.0%} of the time in "
            "every horizon. They are smaller than the instrument's typical candle range, "
            "so they measure almost nothing. The fix is a NEW rule_id with a larger "
            "scale -- never an edit to this one (§21)."
        )

    total_complete = sum(r.complete for r in reports.values())
    total_ambiguous = sum(r.path_ambiguous for r in reports.values())
    if total_complete and total_ambiguous / total_complete >= AMBIGUITY_RATE:
        warnings.append(
            f"{total_ambiguous}/{total_complete} path classifications are ambiguous: the "
            "favourable and adverse thresholds were first crossed within the SAME candle, "
            "so their order was never observed. MFE_FIRST here is a convention, not a "
            "measurement -- treat these as unknown."
        )
    return warnings
