"""The reference block: a measured cohort of past setups, and nothing that could be traded (T-9).

Three things are being pinned here, and they are different kinds of claim.

**The arithmetic is not a second implementation.** Every number in a reference must come out of
the functions ``/monitor`` already uses. The tests compare against those functions directly rather
than against literals wherever the comparison is meaningful, so a future edit that quietly
reimplements a quantile here fails.

**The block cannot be read as an instruction.** No price, no lot size, no target; the caption is
always present; below the cohort floor nothing is published at all. These are asserted as the
absence of things, which is the only way to assert them.

**The population is honest.** Same-day rows are excluded, a member that cannot answer a dimension
fails the match rather than passing it, and the widening order is reported.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aureon.evaluation.rules import XAU_OUTCOME_V2
from aureon.evaluation.stats import median, quantile, wilson_interval
from aureon.models.assessment import (
    MATURE_REAL_DAYS,
    MIN_COHORT,
    SL_QUANTILES,
    TP_QUANTILES,
)
from aureon.models.enums import (
    DirectionContext,
    HistorySource,
    HorizonStatus,
    MtfAlignment,
    PathClassification,
    SessionName,
    SetupAnchorKind,
    SetupFamily,
    Timeframe,
)
from aureon.models.evaluation import HorizonResult, SetupEvaluation, threshold_key
from aureon.models.identity import price_bin, setup_id
from aureon.models.setup import (
    IMMATURE_NOTE,
    REFERENCE_LABEL,
    Setup,
    SetupAnchor,
    SetupContextSummary,
    SetupReference,
)
from aureon.services.assessment_service import default_estimate_horizon
from aureon.services.setup_reference import (
    SETUP_WIDENING_ORDER,
    ReferenceBook,
    belongs,
    build_setup_reference,
    prior_to,
    render_reference,
    select_setup_cohort,
)

NOW = datetime(2026, 9, 16, 9, 35, tzinfo=UTC)
MARKET_DATE = "2026-09-16"
EARLIER = "2026-09-15"
#: The horizon the reference measures over: the rule's last COUNTED one, asked of the same
#: function the service asks rather than pinned to an index. A literal here would pass while
#: measuring a different column from the one the block publishes.
HORIZON = default_estimate_horizon(XAU_OUTCOME_V2)
RULE = XAU_OUTCOME_V2
THRESHOLD = RULE.thresholds[0]


def context(**overrides) -> SetupContextSummary:
    base = {
        "mtf_alignment": MtfAlignment.ALIGNED,
        "volatility_regime": "normal",
        "price_vs_va": "above",
        "session": SessionName.LONDON,
    }
    base.update(overrides)
    return SetupContextSummary(**base)


def a_setup(**overrides) -> Setup:
    ident = {
        "account_scope": "primary",
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "family": SetupFamily.LIQUIDITY_REVERSAL.value,
        "direction_context": DirectionContext.BULLISH.value,
        "market_date": overrides.get("market_date", MARKET_DATE),
        "anchor_kind": SetupAnchorKind.LIQUIDITY_LEVEL.value,
        "anchor_price_bin": price_bin(2412.5, bin_size=0.05),
        "setup_version": "1.0.0",
    }
    base = {
        "setup_id": setup_id(**ident),
        "account_scope": "primary",
        "symbol": "XAUUSD",
        "timeframe": Timeframe.M5,
        "family": SetupFamily.LIQUIDITY_REVERSAL,
        "direction_context": DirectionContext.BULLISH,
        "market_date": MARKET_DATE,
        "anchor": SetupAnchor(
            kind=SetupAnchorKind.LIQUIDITY_LEVEL, price=2412.5, level_type="session_high"
        ),
        "opened_at": NOW,
        "context_summary": context(),
    }
    base.update(overrides)
    return Setup(**base)


def an_evaluation(
    ident: str,
    *,
    family: SetupFamily = SetupFamily.LIQUIDITY_REVERSAL,
    direction: DirectionContext = DirectionContext.BULLISH,
    market_date: str = EARLIER,
    symbol: str = "XAUUSD",
    mfe: float = 400.0,
    mae: float = 150.0,
    reached: bool = True,
    path: PathClassification = PathClassification.MFE_FIRST,
    ambiguous: bool = False,
    complete: bool = True,
    summary: SetupContextSummary | None = None,
) -> SetupEvaluation:
    reached_map = {threshold_key(t): (reached and t <= THRESHOLD) for t in RULE.thresholds}
    if complete:
        horizon = HorizonResult(
            horizon_id=HORIZON,
            status=HorizonStatus.COMPLETE,
            future_high=2450.0,
            future_low=2400.0,
            mfe=mfe,
            mae=mae,
            reached=reached_map,
            time_to={k: (600.0 if v else None) for k, v in reached_map.items()},
            path=path,
            path_ambiguous=ambiguous,
            candles_seen=20,
            completed_at=NOW,
        )
    else:
        horizon = HorizonResult(horizon_id=HORIZON, status=HorizonStatus.PENDING)
    return SetupEvaluation(
        setup_id=ident,
        rule_id=RULE.rule_id,
        family=family,
        direction_context=direction,
        symbol=symbol,
        timeframe=Timeframe.M5,
        market_date=market_date,
        setup_version="1.0.0",
        reference_price=RULE.reference_price,
        reference_value=2412.5,
        horizons=(horizon,),
        context_summary=summary if summary is not None else context(),
    )


def population(count: int, **overrides) -> list[SetupEvaluation]:
    return [an_evaluation(f"past-{i}", **overrides) for i in range(count)]


# ── The population ────────────────────────────────────────────────────────────


def test_same_day_outcomes_are_not_in_this_setups_reference() -> None:
    """The morning's outcome must not feed the afternoon's reference.

    Not merely the subject's own row: a setup opened at 09:35 and one at 14:00 share a session, a
    regime and one day's news, and counting one in the other's reference would let a single day's
    character look like a property of the structure.
    """
    subject = a_setup()
    earlier = an_evaluation("yesterday", market_date=EARLIER)
    today = an_evaluation("this-morning", market_date=MARKET_DATE)
    later = an_evaluation("tomorrow", market_date="2026-09-17")

    kept = prior_to(subject, [earlier, today, later])
    assert [row.setup_id for row in kept] == ["yesterday"]


def test_a_setup_is_never_in_its_own_reference() -> None:
    subject = a_setup()
    own = an_evaluation(subject.setup_id, market_date=EARLIER)
    assert prior_to(subject, [own]) == []


def test_the_families_are_never_mixed() -> None:
    subject = a_setup()
    other = an_evaluation("x", family=SetupFamily.BREAKOUT_ACCEPTANCE)
    assert not belongs(other, subject, dropped=SETUP_WIDENING_ORDER)


def test_the_direction_contexts_are_never_mixed() -> None:
    """Averaging a bullish population with a bearish one reports the mean of two opposite
    claims, and the number looks exactly like a measurement."""
    subject = a_setup()
    other = an_evaluation("x", direction=DirectionContext.BEARISH)
    assert not belongs(other, subject, dropped=SETUP_WIDENING_ORDER)


def test_the_symbols_are_never_mixed() -> None:
    subject = a_setup()
    assert not belongs(
        an_evaluation("x", symbol="XAGUSD"), subject, dropped=SETUP_WIDENING_ORDER
    )


def test_the_session_is_never_dropped() -> None:
    subject = a_setup()
    other = an_evaluation("x", summary=context(session=SessionName.NEW_YORK))
    assert not belongs(other, subject, dropped=SETUP_WIDENING_ORDER)


def test_a_member_that_cannot_answer_a_dimension_fails_the_match() -> None:
    """Counting an unknown as agreement is how a cohort fills with rows never shown to have
    the property being asked about."""
    subject = a_setup()
    blank = an_evaluation("x", summary=context(volatility_regime=None))
    assert not belongs(blank, subject, dropped=())
    # ...and it comes back once that dimension has been given up.
    assert belongs(blank, subject, dropped=("volatility_regime",))


def test_a_subject_that_cannot_answer_a_dimension_asks_nothing_about_it() -> None:
    """The other direction. A setup opened on a day with no volume profile cannot ask about
    the value area, and must not therefore have an empty cohort."""
    subject = a_setup(context_summary=context(price_vs_va=None))
    assert belongs(an_evaluation("x"), subject, dropped=())


def test_the_version_is_not_a_cohort_dimension() -> None:
    """A tuning bump must not empty the cohort -- that is exactly when a reader wants to
    compare. The version rides on every row for anyone who wants to split by it."""
    subject = a_setup()
    other = an_evaluation("x")
    assert other.setup_version == "1.0.0"
    bumped = other.model_copy(update={"setup_version": "2.0.0"})
    assert belongs(bumped, subject, dropped=())


# ── Widening ──────────────────────────────────────────────────────────────────


def test_an_exact_cohort_gives_nothing_up() -> None:
    cohort = select_setup_cohort(a_setup(), population(MIN_COHORT))
    assert len(cohort.members) == MIN_COHORT
    assert cohort.dropped == ()
    assert not cohort.insufficient


def test_widening_gives_up_one_dimension_at_a_time_and_says_which() -> None:
    subject = a_setup()
    # Enough rows, but every one of them disagrees on the value area.
    rows = population(MIN_COHORT, summary=context(price_vs_va="below"))
    cohort = select_setup_cohort(subject, rows)
    assert cohort.dropped == ("price_vs_va",)
    assert len(cohort.members) == MIN_COHORT


def test_the_widening_order_is_the_documented_one() -> None:
    subject = a_setup()
    rows = population(
        MIN_COHORT,
        summary=context(price_vs_va="below", mtf_alignment=MtfAlignment.AGAINST),
    )
    cohort = select_setup_cohort(subject, rows)
    assert cohort.dropped == ("price_vs_va", "mtf_alignment")


def test_the_widening_order_holds_exactly_these_three_dimensions() -> None:
    """Pinned as a literal, because the ORDER IS THE CLAIM.

    A dimension appended to this tuple is a dimension the system is now willing to give up, and
    the four that are absent -- symbol, family, direction context and session -- are absent
    deliberately. ``belongs`` does not consult ``dropped`` for the session at all, so adding
    ``session`` here would be a silent no-op today and a live widening the moment somebody
    "tidied up" the asymmetry. A literal here means that change has to be made on purpose.
    """
    assert SETUP_WIDENING_ORDER == ("price_vs_va", "mtf_alignment", "volatility_regime")
    for never in ("symbol", "family", "direction_context", "session"):
        assert never not in SETUP_WIDENING_ORDER


def test_a_cohort_that_stays_too_small_is_returned_anyway_fully_widened() -> None:
    """"We looked and there was not enough history" is a finding worth counting, and a more
    useful answer than an empty block."""
    cohort = select_setup_cohort(a_setup(), population(3))
    assert len(cohort.members) == 3
    assert cohort.dropped == SETUP_WIDENING_ORDER
    assert cohort.insufficient


# ── The numbers, against the functions /monitor uses ──────────────────────────


def test_the_quantiles_are_the_ones_monitor_publishes() -> None:
    rows = [an_evaluation(f"p{i}", mfe=100.0 + i, mae=40.0 + i) for i in range(MIN_COHORT)]
    reference = build_setup_reference(a_setup(), population=rows, rule=RULE, now=NOW)

    assert [e.quantile for e in reference.mfe] == list(TP_QUANTILES)
    assert [e.quantile for e in reference.mae] == list(SL_QUANTILES)

    mfe_values = [100.0 + i for i in range(MIN_COHORT)]
    mae_values = [40.0 + i for i in range(MIN_COHORT)]
    assert reference.mfe[0].points == round(median(mfe_values), 2)
    assert reference.mfe[1].points == round(quantile(mfe_values, 0.25), 2)
    assert reference.mae[0].points == round(quantile(mae_values, 0.75), 2)
    assert reference.mae[1].points == round(quantile(mae_values, 0.9), 2)


def test_the_paired_rate_carries_its_n_and_its_95_percent_interval() -> None:
    rows = population(MIN_COHORT)
    # Ten of them went against first.
    rows = rows[:10] + [
        row.model_copy(
            update={
                "horizons": (
                    row.horizons[0].model_copy(update={"path": PathClassification.MAE_FIRST}),
                )
            }
        )
        for row in rows[10:]
    ]
    reference = build_setup_reference(a_setup(), population=rows, rule=RULE, now=NOW)
    paired = reference.paired
    assert paired is not None
    assert paired.evaluated == MIN_COHORT
    assert paired.favourable_first == 10
    low, high = wilson_interval(10, MIN_COHORT)
    assert (paired.ci_low, paired.ci_high) == (low, high)


def test_nothing_is_published_below_the_cohort_floor() -> None:
    """The point estimate is what a reader quotes, and an interval on n=4 is so wide that
    printing the estimate beside it invites reading the estimate alone."""
    reference = build_setup_reference(a_setup(), population=population(4), rule=RULE, now=NOW)
    assert reference.insufficient
    assert reference.cohort_n == 4
    assert reference.mfe == ()
    assert reference.mae == ()
    assert reference.paired is None


def test_the_horizon_is_a_counted_one_and_is_recorded() -> None:
    reference = build_setup_reference(
        a_setup(), population=population(MIN_COHORT), rule=RULE, now=NOW
    )
    assert reference.horizon_id == HORIZON
    assert reference.rule_id == RULE.rule_id


def test_pending_horizons_are_not_measured_as_zero() -> None:
    """A horizon that has not finished is not an excursion of nothing."""
    rows = population(MIN_COHORT, complete=False)
    reference = build_setup_reference(a_setup(), population=rows, rule=RULE, now=NOW)
    assert reference.cohort_n == MIN_COHORT
    assert reference.mfe == ()
    assert reference.mae == ()


# ── It cannot be read as an instruction ───────────────────────────────────────


def test_no_estimate_carries_a_price() -> None:
    """A price on this block would render as a level on a card beside a chart, at a plausible
    distance from the market, which is indistinguishable from a target."""
    reference = build_setup_reference(
        a_setup(), population=population(MIN_COHORT), rule=RULE, now=NOW
    )
    assert reference.mfe and reference.mae
    assert all(e.price is None for e in (*reference.mfe, *reference.mae))


def test_the_model_refuses_a_quantile_it_does_not_publish() -> None:
    from aureon.models.assessment import Estimate

    with pytest.raises(ValueError, match="not the published"):
        SetupReference(mfe=(Estimate(quantile=0.9, points=10.0),))


def test_every_rendering_carries_the_three_word_label() -> None:
    reference = build_setup_reference(
        a_setup(), population=population(MIN_COHORT), rule=RULE, now=NOW
    )
    assert reference.caption.startswith(REFERENCE_LABEL)
    assert render_reference(reference)[0] == reference.caption
    for word in ("historical", "measured cohort", "research context"):
        assert word in REFERENCE_LABEL


def test_the_rendering_says_nothing_that_looks_like_an_order() -> None:
    reference = build_setup_reference(
        a_setup(), population=population(MIN_COHORT), rule=RULE, now=NOW
    )
    text = " ".join(render_reference(reference)).lower()
    for forbidden in ("buy", "sell", "entry", "take profit", "stop loss", "lot", "target"):
        assert forbidden not in text, f"the reference rendering said {forbidden!r}"


# ── Maturity and the history source ───────────────────────────────────────────


def test_a_cohort_from_unverified_days_is_synthetic_and_immature() -> None:
    reference = build_setup_reference(
        a_setup(), population=population(MIN_COHORT), rule=RULE, real_days=set(), now=NOW
    )
    assert reference.history_source is HistorySource.SYNTHETIC
    assert reference.real_days == 0
    assert reference.immature
    assert not reference.is_evidence
    assert IMMATURE_NOTE in reference.caption


def test_a_caller_who_did_not_say_gets_unknown_rather_than_synthetic() -> None:
    """A caller not wired up yet and a cohort known to be generated are different facts."""
    reference = build_setup_reference(
        a_setup(), population=population(MIN_COHORT), rule=RULE, real_days=None, now=NOW
    )
    assert reference.history_source is HistorySource.UNKNOWN


def test_enough_verified_days_makes_the_reference_evidence() -> None:
    days = [f"2026-09-{day:02d}" for day in range(1, 1 + MATURE_REAL_DAYS)]
    rows = [
        an_evaluation(f"p{i}", market_date=days[i % len(days)]) for i in range(MIN_COHORT)
    ]
    reference = build_setup_reference(
        a_setup(), population=rows, rule=RULE, real_days=set(days), now=NOW
    )
    assert reference.history_source is HistorySource.REAL
    assert reference.real_days == MATURE_REAL_DAYS
    assert not reference.immature
    assert reference.is_evidence
    assert IMMATURE_NOTE not in reference.caption


def test_an_empty_reference_says_nothing_rather_than_zero() -> None:
    empty = SetupReference()
    assert not empty.measured
    assert empty.cohort_n is None
    assert "nothing measured yet" in empty.caption
    # And an absence is immature: there is nothing mature about it.
    assert empty.immature


def test_a_measured_cohort_of_zero_is_not_an_empty_reference() -> None:
    reference = build_setup_reference(a_setup(), population=[], rule=RULE, now=NOW)
    assert reference.measured
    assert reference.cohort_n == 0
    assert reference.insufficient


# ── The book the observer holds ───────────────────────────────────────────────


class FakeEvaluations:
    def __init__(self, rows: list[SetupEvaluation], *, raises: bool = False) -> None:
        self.rows = rows
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def before(self, *, symbol: str, market_date: str) -> list[SetupEvaluation]:
        self.calls.append((symbol, market_date))
        if self.raises:
            raise RuntimeError("firestore is unavailable")
        return [row for row in self.rows if row.market_date < market_date]


def test_the_book_reads_once_per_broker_day() -> None:
    """A Firestore query per setup opening would put an unbounded read on the candle loop."""
    repository = FakeEvaluations(population(MIN_COHORT))
    book = ReferenceBook(symbol="XAUUSD", rule=RULE, repository=repository, now=lambda: NOW)

    first = book.for_setup(a_setup())
    second = book.for_setup(a_setup(market_date=MARKET_DATE))
    assert repository.calls == [("XAUUSD", MARKET_DATE)]
    assert first.cohort_n == second.cohort_n == MIN_COHORT

    book.for_setup(a_setup(market_date="2026-09-17"))
    assert len(repository.calls) == 2


def test_a_read_failure_costs_the_block_and_not_the_setup() -> None:
    repository = FakeEvaluations([], raises=True)
    book = ReferenceBook(symbol="XAUUSD", rule=RULE, repository=repository, now=lambda: NOW)
    reference = book.for_setup(a_setup())
    # Measured over nothing rather than raising, and the caller opens the setup regardless.
    assert reference.cohort_n == 0


def test_the_book_never_raises_even_when_the_arithmetic_does() -> None:
    class Exploding(FakeEvaluations):
        def before(self, **kwargs):  # type: ignore[override]
            return [object()]  # not a SetupEvaluation; the arithmetic will fail on it

    book = ReferenceBook(
        symbol="XAUUSD", rule=RULE, repository=Exploding([]), now=lambda: NOW
    )
    assert book.for_setup(a_setup()) == SetupReference()
