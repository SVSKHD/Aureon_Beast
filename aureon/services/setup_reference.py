"""What happened last time a setup of this shape confirmed (12, T-9).

A setup card without this block invites the reader to supply the missing half from memory, and
memory is the least reliable instrument in the building. With it, the card can say "of the 64
bullish liquidity reversals measured on this symbol, the median one travelled 41 points before it
gave back 18" -- a statement about the record, with its n attached, which a reader can accept or
dismiss on its own terms.

## It reuses ``/monitor``'s arithmetic rather than repeating it

Every number here comes from the functions ``assessment_service`` already publishes: ``quantile``
and ``wilson_interval`` from ``evaluation.stats``, the ``TP_QUANTILES``/``SL_QUANTILES`` pairs,
the ``MIN_COHORT`` floor, the ``MATURE_REAL_DAYS`` maturity rule and ``classify_dates``. A second
implementation would differ from the first on some edge -- an empty cohort, a horizon that never
completed -- and there would be no way to say which of the two screens was right.

What is different is the *population*: ``setup_evaluations`` rather than ``detection_evaluations``,
and the cohort dimensions are a setup's (family, direction context, the four context labels)
rather than a detection's (agent, direction, wick tag).

## The reference is in POINTS and carries no price

``assessment_service.estimates`` can attach a price -- the points applied to a detection's own
reference -- because ``/monitor`` is answering a question about one detection at one moment. A
setup's reference is a statement about a population, and a price on it would render as a level:
a number on a card, beside a chart, at a plausible distance from the current price, which is
indistinguishable from a target. So the block publishes points and nothing else, and the card
says what the points are of.

## It is measured at OPEN and never rewritten

The reference is attached when the setup is created and left alone as the setup advances. Two
reasons. A block that refreshed every candle would make the same document say different things
through the day, so a screenshot could not be reproduced. And the honest question is "what did
the record say when this was first noticed", because that is the moment a reader was deciding
whether to care.

## It never gates anything

Nothing reads ``Setup.reference`` to decide whether to open, advance, confirm or invalidate a
setup, and nothing in the execution path can see it at all. It is research context on a card. A
boundary test asserts this module imports nothing from ``aureon.execution``.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aureon.models.assessment import MIN_COHORT, SL_QUANTILES, TP_QUANTILES
from aureon.models.base import to_utc, utc_now
from aureon.models.evaluation import EvaluationRule, SetupEvaluation
from aureon.models.setup import Setup, SetupReference

log = logging.getLogger(__name__)

#: Cohort dimensions, in the order they are given up when there is not enough history.
#:
#: The mirror of ``assessment.WIDENING_ORDER`` and the same claim: the narrowest and least
#: load-bearing goes first. Value-area position says something about one price relative to one
#: day's profile; MTF alignment says something about four timeframes at one moment; the
#: volatility regime goes last because a high-volatility cohort and a low-volatility one answer
#: genuinely different questions -- forty points of gold is a common excursion in one and a rare
#: one in the other.
#:
#: ``symbol``, ``family``, ``direction_context`` and ``session`` are NEVER dropped. A cohort that
#: mixed families would be answering about a different structure; one that mixed direction
#: contexts would average a bullish population with a bearish one and report the mean of two
#: opposite claims.
SETUP_WIDENING_ORDER: tuple[str, ...] = ("price_vs_va", "mtf_alignment", "volatility_regime")


@dataclass(frozen=True)
class SetupCohort:
    """The population a reference was measured over, and what it cost to get there."""

    members: tuple[SetupEvaluation, ...]
    dropped: tuple[str, ...]

    @property
    def insufficient(self) -> bool:
        return len(self.members) < MIN_COHORT


def belongs(evaluation: SetupEvaluation, subject: Setup, *, dropped: Sequence[str]) -> bool:
    """Whether a past setup's outcome belongs in this setup's cohort.

    A dimension in ``dropped`` matches everything -- that is what dropping one means. A dimension
    the MEMBER cannot answer (no regime recorded, no profile that day) fails the match rather
    than passing it, for the reason ``assessment_service.matches`` gives: counting an unknown as
    agreement is how a cohort fills up with rows that were never shown to have the property being
    asked about.

    ``setup_version`` is deliberately NOT matched. A version bump means a threshold moved, and
    pinning it would empty the cohort on every tuning change -- exactly when a reader most wants
    to compare. The same choice ``filter_for`` makes in matching ``agent_name`` and not
    ``agent_version``, and the version rides on every row for anyone who wants to split by it.
    """
    given_up = set(dropped)
    if evaluation.symbol.upper() != subject.symbol.upper():
        return False
    if evaluation.family is not subject.family:
        return False
    if evaluation.direction_context is not subject.direction_context:
        return False
    wanted = subject.context_summary
    found = evaluation.context_summary
    if wanted.session is not None and found.session is not wanted.session:
        return False
    if "price_vs_va" not in given_up and wanted.price_vs_va is not None:
        if found.price_vs_va != wanted.price_vs_va:
            return False
    if "mtf_alignment" not in given_up:
        # MIXED is a real answer rather than an absent one, so it is matched like any other.
        if found.mtf_alignment is not wanted.mtf_alignment:
            return False
    if "volatility_regime" not in given_up and wanted.volatility_regime is not None:
        if found.volatility_regime != wanted.volatility_regime:
            return False
    return True


def select_setup_cohort(
    subject: Setup, population: Sequence[SetupEvaluation]
) -> SetupCohort:
    """The narrowest cohort of at least ``MIN_COHORT``, and which dimensions it gave up.

    Widens one dimension at a time and stops at the first cohort large enough. If even the fully
    widened cohort is too small it is returned anyway, with every dimension listed as dropped --
    the caller publishes ``insufficient`` and no percentages, which is a more useful answer than
    an empty block.
    """
    dropped: list[str] = []
    while True:
        found = tuple(m for m in population if belongs(m, subject, dropped=dropped))
        if len(found) >= MIN_COHORT or len(dropped) == len(SETUP_WIDENING_ORDER):
            return SetupCohort(members=found, dropped=tuple(dropped))
        dropped.append(SETUP_WIDENING_ORDER[len(dropped)])


def prior_to(
    subject: Setup, population: Sequence[SetupEvaluation]
) -> list[SetupEvaluation]:
    """The rows measured on a STRICTLY EARLIER broker day than the setup being described.

    Same-day rows are excluded, not just the subject's own. A setup opened at 09:35 and one
    opened at 14:00 on the same day share a session, a regime and one day's news, and counting
    the morning's outcome in the afternoon's reference would let a single day's character look
    like a property of the structure. It also keeps the block reproducible: the reference on a
    setup from last Tuesday is the same whenever it is recomputed.
    """
    return [
        row
        for row in population
        if row.market_date < subject.market_date and row.setup_id != subject.setup_id
    ]


def build_setup_reference(
    subject: Setup,
    *,
    population: Sequence[SetupEvaluation],
    rule: EvaluationRule,
    horizon_id: str | None = None,
    real_days: Collection[str] | None = None,
    now: datetime | None = None,
) -> SetupReference:
    """The reference block for one setup. Arithmetic only -- no reads, no writes, no clock."""
    from aureon.services.assessment_service import (
        classify_dates,
        default_estimate_horizon,
        excursions_from,
        paired_outcome_from,
        point_estimates,
    )

    cohort = select_setup_cohort(subject, prior_to(subject, population))
    horizon = horizon_id or default_estimate_horizon(rule)
    source, days = classify_dates([m.market_date for m in cohort.members], real_days)
    mfe, mae = excursions_from(cohort.members, horizon)
    enough = not cohort.insufficient
    favourable = rule.thresholds[0]
    return SetupReference(
        rule_id=rule.rule_id,
        horizon_id=horizon,
        cohort_n=len(cohort.members),
        dropped=cohort.dropped,
        mfe=point_estimates(mfe, TP_QUANTILES) if enough else (),
        mae=point_estimates(mae, SL_QUANTILES) if enough else (),
        paired=(
            paired_outcome_from(
                cohort.members, horizon, favourable=favourable, adverse=favourable
            )
            if enough
            else None
        ),
        insufficient=cohort.insufficient,
        history_source=source,
        real_days=days,
        measured_at=to_utc(now or utc_now()),
    )


class ReferenceBook:
    """One symbol's past setup outcomes, read once per broker day (T-9).

    The engine opens setups inside the observer's candle loop, and a Firestore query per opening
    would put an unbounded read on the hot path for data that changes once a day. So the
    population is loaded on the first setup of each broker day and held; the rows it would have
    picked up since are same-day rows, which ``prior_to`` excludes anyway.

    A failure to load is not a failure to observe: the book logs and serves an EMPTY reference,
    which says "nothing measured" rather than "n=0", and the setup is tracked regardless.
    """

    def __init__(
        self,
        *,
        symbol: str,
        rule: EvaluationRule,
        repository: Any,
        real_days: Collection[str] | None = None,
        horizon_id: str | None = None,
        now: Any = utc_now,
    ) -> None:
        self.symbol = symbol.upper()
        self.rule = rule
        self.repository = repository
        self.real_days = None if real_days is None else frozenset(str(d) for d in real_days)
        self.horizon_id = horizon_id
        self._now = now
        self._population: tuple[SetupEvaluation, ...] = ()
        self._loaded_before: str | None = None

    def population_before(self, market_date: str) -> tuple[SetupEvaluation, ...]:
        if self._loaded_before == market_date:
            return self._population
        try:
            rows = self.repository.before(symbol=self.symbol, market_date=market_date)
        except Exception:  # noqa: BLE001 - a read failure must not stop observation
            log.exception("could not load the setup reference population for %s", self.symbol)
            return ()
        self._population = tuple(rows)
        self._loaded_before = market_date
        log.info(
            "setup reference population for %s before %s: %d rows",
            self.symbol,
            market_date,
            len(self._population),
        )
        return self._population

    def for_setup(self, setup: Setup) -> SetupReference:
        """The block to attach, or an empty one. Never raises."""
        try:
            return build_setup_reference(
                setup,
                population=self.population_before(setup.market_date),
                rule=self.rule,
                horizon_id=self.horizon_id,
                real_days=self.real_days,
                now=to_utc(self._now()),
            )
        except Exception:  # noqa: BLE001 - see the class docstring
            log.exception("could not measure a reference for setup %s", setup.setup_id)
            return SetupReference()


def render_reference(reference: SetupReference) -> list[str]:
    """The block as lines, for a Discord card, a review or a console.

    Here rather than in the embed so every surface says the same thing. The caption comes first
    and is never optional: a reader who sees the numbers without it is reading advice.
    """
    lines = [reference.caption]
    if not reference.measured:
        return lines
    if reference.insufficient:
        lines.append(
            f"no percentages published below n={MIN_COHORT} — the cohort was "
            f"{reference.cohort_n}"
        )
        return lines
    if reference.mfe:
        lines.append(
            "moved in favour (points): "
            + ", ".join(f"p{int(e.quantile * 100)}={e.points:g}" for e in reference.mfe)
        )
    if reference.mae:
        lines.append(
            "gave back (points): "
            + ", ".join(f"p{int(e.quantile * 100)}={e.points:g}" for e in reference.mae)
        )
    paired = reference.paired
    if paired is not None and paired.rate is not None:
        interval = (
            ""
            if paired.ci_low is None
            else f" (95%: {paired.ci_low:.0%}–{paired.ci_high:.0%})"
        )
        lines.append(
            f"reached +{paired.favourable:g} before -{paired.adverse:g} in "
            f"{paired.rate:.0%} of {paired.evaluated}{interval}"
        )
    return lines


__all__ = [
    "SETUP_WIDENING_ORDER",
    "ReferenceBook",
    "SetupCohort",
    "belongs",
    "build_setup_reference",
    "prior_to",
    "render_reference",
    "select_setup_cohort",
]
