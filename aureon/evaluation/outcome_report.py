"""Outcomes grouped by agent and session (§22, §23, P-1).

``build_report`` in ``aureon.evaluation.backfill`` answers "what did this rule find,
per horizon". That was enough while one agent was evaluated; it is not enough now.
With three directional agents and four sessions, a single reached-N table per horizon
averages a London breakout together with an Asia sweep and reports the average as if it
described both.

So this aggregates the same evaluations one level finer -- per **agent** and per
**session** and per horizon -- and reports, for each group:

* how many detections it holds, how many carry an evaluation, and how many reached a
  ``COMPLETE`` horizon at all;
* reached counts for every threshold, as a count **and** a fraction of COMPLETE only;
* median MFE and MAE;
* median time to the headline threshold;
* the MFE_FIRST / MAE_FIRST split.

## Three rules it inherits and does not relax

1. **COMPLETE only.** Every reached count, median and path figure comes from
   ``complete_horizons``. PENDING and INVALID are reported in their own columns and are
   never folded into a denominator: an unknown answer is not a miss.
2. **Units come from the rule.** MFE and MAE are stored in points; a PRICE rule's
   numbers are rendered in price using the symbol's own ``point``. A report that
   printed 700 under a heading of ``$3 / $5 / ...`` would be read as 700 dollars.
3. **``None``, not zero, for no data.** A median of nothing and a fraction of nothing
   are absent, and render as an em dash. A zero is a measurement.

## The time-to-threshold median is over reachers

Only a horizon that actually reached the threshold has a time to it, so the median is
taken over those and the count is reported beside it. The alternative -- treating a
non-reach as an infinite or zero time -- would produce a number that is not a duration
at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import median

from aureon.models.detection import Detection
from aureon.models.enums import HorizonStatus, ThresholdUnit
from aureon.models.evaluation import DetectionEvaluation, EvaluationRule

#: The session label used for a row merged across every session.
ALL_SESSIONS = "all"

#: Session order for rendering, so two runs produce the same document. ``off`` last
#: because it is the residue rather than a trading session.
SESSION_ORDER: tuple[str, ...] = ("asia", "london", "new_york", "off")

#: The threshold whose time-to-reach is reported. $5 by name in the P-1 request; looked
#: up by key so a rule without it reports nothing rather than raising.
HEADLINE_THRESHOLD_KEY = "5"

UNKNOWN = "—"


def _median_or_none(values: Sequence[float]) -> float | None:
    return median(values) if values else None


@dataclass
class HorizonStats:
    """One horizon's outcomes within one (agent, session) group."""

    horizon_id: str
    complete: int = 0
    pending: int = 0
    invalid: int = 0
    #: threshold key -> reached count, from COMPLETE horizons only.
    reached: dict[str, int] = field(default_factory=dict)
    mfe_first: int = 0
    mae_first: int = 0
    path_none: int = 0
    path_ambiguous: int = 0
    #: Excursions in POINTS, COMPLETE only. Kept as samples rather than as a running
    #: mean because a median cannot be accumulated incrementally, and a mean is the
    #: wrong statistic here -- one 40-dollar run would move it and describe nothing.
    mfe_points: list[float] = field(default_factory=list)
    mae_points: list[float] = field(default_factory=list)
    #: threshold key -> seconds to first reach, over the horizons that reached it.
    time_to_seconds: dict[str, list[float]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.complete + self.pending + self.invalid

    def fraction_reached(self, key: str) -> float | None:
        """Reached / COMPLETE, or ``None`` when nothing completed."""
        if not self.complete:
            return None
        return self.reached.get(key, 0) / self.complete

    @property
    def median_mfe_points(self) -> float | None:
        return _median_or_none(self.mfe_points)

    @property
    def median_mae_points(self) -> float | None:
        return _median_or_none(self.mae_points)

    def median_time_to_seconds(self, key: str) -> float | None:
        return _median_or_none(self.time_to_seconds.get(key, []))

    def reachers(self, key: str) -> int:
        return len(self.time_to_seconds.get(key, []))

    def absorb(self, other: HorizonStats) -> None:
        """Fold another group's stats for the same horizon into this one."""
        self.complete += other.complete
        self.pending += other.pending
        self.invalid += other.invalid
        for key, count in other.reached.items():
            self.reached[key] = self.reached.get(key, 0) + count
        self.mfe_first += other.mfe_first
        self.mae_first += other.mae_first
        self.path_none += other.path_none
        self.path_ambiguous += other.path_ambiguous
        self.mfe_points.extend(other.mfe_points)
        self.mae_points.extend(other.mae_points)
        for key, samples in other.time_to_seconds.items():
            self.time_to_seconds.setdefault(key, []).extend(samples)


@dataclass
class GroupStats:
    """Everything known about one agent in one session."""

    agent_name: str
    session: str
    #: Detections in this group, INCLUDING context-only ones with no direction.
    detections: int = 0
    #: Detections that carry an evaluation under this rule.
    evaluated: int = 0
    #: Detections with at least one COMPLETE horizon. Not the same as ``evaluated``:
    #: a detection near the end of the data has an evaluation whose every horizon is
    #: still PENDING, and counting it as evaluated-and-answered would be wrong.
    with_complete: int = 0
    horizons: dict[str, HorizonStats] = field(default_factory=dict)

    @property
    def context_only(self) -> int:
        """Detections with no evaluation -- no direction, so no favourable side."""
        return self.detections - self.evaluated

    def horizon(self, horizon_id: str) -> HorizonStats:
        return self.horizons.setdefault(
            horizon_id, HorizonStats(horizon_id=horizon_id)
        )

    def absorb(self, other: GroupStats) -> None:
        self.detections += other.detections
        self.evaluated += other.evaluated
        self.with_complete += other.with_complete
        for horizon_id, stats in other.horizons.items():
            self.horizon(horizon_id).absorb(stats)


@dataclass
class OutcomeAggregate:
    """The grouped report, plus what is needed to render its numbers honestly."""

    rule: EvaluationRule
    point: float
    groups: dict[tuple[str, str], GroupStats] = field(default_factory=dict)

    # ── Shape ─────────────────────────────────────────────────────────────────

    @property
    def agents(self) -> tuple[str, ...]:
        return tuple(sorted({agent for agent, _ in self.groups}))

    def sessions_for(self, agent: str) -> tuple[str, ...]:
        present = {session for a, session in self.groups if a == agent}
        ordered = [s for s in SESSION_ORDER if s in present]
        # A session name the config grew after this module was written still appears,
        # rather than silently vanishing from the report.
        ordered += sorted(present - set(SESSION_ORDER))
        return tuple(ordered)

    @property
    def horizon_ids(self) -> tuple[str, ...]:
        return tuple(h.id for h in self.rule.horizons)

    def group(self, agent: str, session: str) -> GroupStats:
        """One group, merging across sessions when ``session`` is ``ALL_SESSIONS``."""
        if session != ALL_SESSIONS:
            return self.groups.get(
                (agent, session), GroupStats(agent_name=agent, session=session)
            )
        merged = GroupStats(agent_name=agent, session=ALL_SESSIONS)
        for (a, _), stats in sorted(self.groups.items()):
            if a == agent:
                merged.absorb(stats)
        return merged

    def total(self) -> GroupStats:
        merged = GroupStats(agent_name=ALL_SESSIONS, session=ALL_SESSIONS)
        for _, stats in sorted(self.groups.items()):
            merged.absorb(stats)
        return merged

    # ── Units ─────────────────────────────────────────────────────────────────

    @property
    def unit_scale(self) -> float:
        """Points -> the rule's own threshold unit.

        1.0 for a POINTS rule; the symbol's ``point`` for a PRICE one. Derived, never
        a written-down 100 (§21).
        """
        return 1.0 if self.rule.threshold_unit is ThresholdUnit.POINTS else self.point

    def in_rule_unit(self, points: float | None) -> float | None:
        return None if points is None else points * self.unit_scale

    @property
    def unit_label(self) -> str:
        return "points" if self.rule.threshold_unit is ThresholdUnit.POINTS else "price"


def aggregate(
    detections: Iterable[Detection],
    evaluations: Mapping[str, DetectionEvaluation] | Iterable[DetectionEvaluation],
    rule: EvaluationRule,
    *,
    point: float,
    agents: Iterable[str] | None = None,
) -> OutcomeAggregate:
    """Group detections and their evaluations by agent and session.

    ``evaluations`` may be a mapping keyed by detection id or any iterable of
    evaluations; only those carrying this rule's ``rule_id`` are read, so passing a
    mixed set from a repository that stores several rules is safe.

    ``agents`` restricts the report; the default includes every agent present, so a new
    agent appears without anyone remembering to add it here.
    """
    if point <= 0:
        raise ValueError(f"point must be positive, got {point}")

    by_id: dict[str, DetectionEvaluation] = {}
    if isinstance(evaluations, Mapping):
        candidates: Iterable[DetectionEvaluation] = evaluations.values()
    else:
        candidates = evaluations
    for evaluation in candidates:
        if evaluation.rule_id == rule.rule_id:
            by_id[evaluation.detection_id] = evaluation

    wanted = set(agents) if agents is not None else None
    report = OutcomeAggregate(rule=rule, point=point)
    keys = rule.threshold_keys

    for detection in detections:
        if wanted is not None and detection.agent_name not in wanted:
            continue
        key = (detection.agent_name, detection.session.session.value)
        group = report.groups.setdefault(
            key, GroupStats(agent_name=key[0], session=key[1])
        )
        group.detections += 1

        evaluation = by_id.get(detection.detection_id)
        if evaluation is None:
            continue
        group.evaluated += 1

        # PENDING and INVALID are counted from the raw tuple; they contribute to no
        # reached count and to no median.
        for result in evaluation.horizons:
            stats = group.horizon(result.horizon_id)
            if result.status is HorizonStatus.PENDING:
                stats.pending += 1
            elif result.status is HorizonStatus.INVALID:
                stats.invalid += 1

        complete = evaluation.complete_horizons
        if complete:
            group.with_complete += 1
        for result in complete:
            stats = group.horizon(result.horizon_id)
            stats.complete += 1
            if result.path_ambiguous:
                stats.path_ambiguous += 1
            match result.path.value:
                case "mfe_first":
                    stats.mfe_first += 1
                case "mae_first":
                    stats.mae_first += 1
                case _:
                    stats.path_none += 1
            if result.mfe is not None:
                stats.mfe_points.append(result.mfe)
            if result.mae is not None:
                stats.mae_points.append(result.mae)
            for threshold_key in keys:
                if result.reached.get(threshold_key):
                    stats.reached[threshold_key] = (
                        stats.reached.get(threshold_key, 0) + 1
                    )
                    seconds = result.time_to.get(threshold_key)
                    if seconds is not None:
                        stats.time_to_seconds.setdefault(threshold_key, []).append(
                            seconds
                        )
                else:
                    stats.reached.setdefault(threshold_key, 0)

    return report


# ── Rendering ────────────────────────────────────────────────────────────────


def _reach_cell(stats: HorizonStats, key: str) -> str:
    fraction = stats.fraction_reached(key)
    if fraction is None:
        return UNKNOWN
    return f"{stats.reached.get(key, 0)}/{stats.complete} ({fraction * 100:.0f}%)"


def _signed(value: float | None) -> str:
    return UNKNOWN if value is None else f"{value:+.2f}"


def _minutes(seconds: float | None) -> str:
    return UNKNOWN if seconds is None else f"{seconds / 60:.0f}m"


def _threshold_label(rule: EvaluationRule, key: str) -> str:
    return (
        f"{key}pt" if rule.threshold_unit is ThresholdUnit.POINTS else f"${key}"
    )


def render_markdown(report: OutcomeAggregate) -> list[str]:
    """The per-agent, per-session tables, as markdown lines.

    Shared with the terminal report through the same aggregate, so the document and
    the script cannot disagree about a number.
    """
    rule = report.rule
    keys = list(rule.threshold_keys)
    headline = HEADLINE_THRESHOLD_KEY if HEADLINE_THRESHOLD_KEY in keys else keys[0]
    reach_headers = "".join(f" reach {_threshold_label(rule, k)} |" for k in keys)

    lines: list[str] = []
    for agent in report.agents:
        overall = report.group(agent, ALL_SESSIONS)
        if overall.evaluated == 0:
            # A context-only agent: every detection has direction=None, so there is no
            # favourable side and no table to draw. Named rather than omitted, so its
            # absence reads as a property of the agent instead of a gap in the report.
            lines += [
                "",
                f"### `{agent}` — no outcomes",
                "",
                f"{overall.detections} detections, none evaluable: this agent emits "
                "`direction=None`, so there is no favourable side to measure (§16, "
                "decision 48).",
            ]
            continue
        lines += [
            "",
            f"### `{agent}` — {rule.rule_id}",
            "",
            f"{overall.detections} detections, {overall.evaluated} evaluated, "
            f"{overall.with_complete} with at least one COMPLETE horizon"
            + (
                f" ({overall.context_only} context-only, no direction to measure)"
                if overall.context_only
                else ""
            ),
            "",
            "| session | horizon | complete | pending | invalid |"
            + reach_headers
            + f" median MFE ({report.unit_label}) | median MAE ({report.unit_label}) |"
            f" median t→{_threshold_label(rule, headline)} | MFE_FIRST | MAE_FIRST |"
            " ambiguous |",
            "|---|---|---|---|---|" + "---|" * (len(keys) + 6),
        ]
        for session in (*report.sessions_for(agent), ALL_SESSIONS):
            group = report.group(agent, session)
            label = f"**{session}**" if session == ALL_SESSIONS else f"`{session}`"
            for horizon_id in report.horizon_ids:
                stats = group.horizons.get(horizon_id)
                if stats is None or stats.total == 0:
                    continue
                cells = "".join(f" {_reach_cell(stats, k)} |" for k in keys)
                lines.append(
                    f"| {label} | `{horizon_id}` | {stats.complete} | {stats.pending} |"
                    f" {stats.invalid} |"
                    + cells
                    + f" {_signed(report.in_rule_unit(stats.median_mfe_points))} |"
                    f" {_signed(report.in_rule_unit(stats.median_mae_points))} |"
                    f" {_minutes(stats.median_time_to_seconds(headline))}"
                    f" (n={stats.reachers(headline)}) |"
                    f" {stats.mfe_first} | {stats.mae_first} |"
                    f" {stats.path_ambiguous} |"
                )
    return lines


def render_text(report: OutcomeAggregate) -> str:
    """The same numbers for a terminal, one block per agent."""
    rule = report.rule
    keys = list(rule.threshold_keys)
    headline = HEADLINE_THRESHOLD_KEY if HEADLINE_THRESHOLD_KEY in keys else keys[0]

    lines = [
        f"rule: {rule.rule_id}  (reference={rule.reference_price.value}, "
        f"thresholds in {rule.threshold_unit.value}="
        f"{list(rule.thresholds)}, point={report.point})",
        "",
        "Reached counts, medians and path splits use COMPLETE horizons ONLY. Pending",
        "and invalid are shown separately and are never counted as misses.",
    ]
    if not report.groups:
        lines += ["", "no detections in this period"]
        return "\n".join(lines)

    width = 14
    header = (
        f"{'session':<10}{'horizon':<16}{'compl':>6}{'pend':>6}{'inval':>6}  "
        + "".join(f"{_threshold_label(rule, k):>14}" for k in keys)
        + f"{'med MFE':>10}{'med MAE':>10}"
        + f"{'med t→' + _threshold_label(rule, headline):>14}"
        + f"{'MFE1':>6}{'MAE1':>6}{'ambig':>7}"
    )
    for agent in report.agents:
        overall = report.group(agent, ALL_SESSIONS)
        lines += [
            "",
            f"── {agent} " + "─" * max(0, 60 - len(agent)),
            f"   {overall.detections} detections, {overall.evaluated} evaluated, "
            f"{overall.with_complete} with a COMPLETE horizon, "
            f"{overall.context_only} context-only",
            "",
            header,
            "-" * len(header),
        ]
        for session in (*report.sessions_for(agent), ALL_SESSIONS):
            group = report.group(agent, session)
            for horizon_id in report.horizon_ids:
                stats = group.horizons.get(horizon_id)
                if stats is None or stats.total == 0:
                    continue
                cells = "".join(f"{_reach_cell(stats, k):>14}" for k in keys)
                lines.append(
                    f"{session:<10}{horizon_id:<16}{stats.complete:>6}"
                    f"{stats.pending:>6}{stats.invalid:>6}  "
                    + cells
                    + f"{_signed(report.in_rule_unit(stats.median_mfe_points)):>10}"
                    f"{_signed(report.in_rule_unit(stats.median_mae_points)):>10}"
                    + f"{_minutes(stats.median_time_to_seconds(headline)):>{width}}"
                    + f"{stats.mfe_first:>6}{stats.mae_first:>6}"
                    f"{stats.path_ambiguous:>7}"
                )
    return "\n".join(lines)
