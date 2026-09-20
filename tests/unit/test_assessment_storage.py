"""The 9D documents and the arithmetic under them.

Three things are worth pinning before anything is built on top:

* **the statistics**, because a Wilson interval and a quantile each have several defensible
  definitions and the difference between them is a dollar or two of gold — and the same
  number has to appear in a Discord embed and in a weekly review;
* **a rate of zero is not an empty cohort**, which is the single easiest way for "we have no
  data" to be published as "it never works";
* **a note never touches its trade**, which is the whole reason notes have their own
  collection.
"""

from __future__ import annotations

import statistics as stdlib_statistics
from datetime import UTC, datetime, timedelta

import pytest

from aureon.evaluation.stats import Z_95, median, quantile, wilson_interval
from aureon.models.assessment import (
    MIN_COHORT,
    WIDENING_ORDER,
    Assessment,
    CohortFilter,
    Estimate,
    HorizonConfirmation,
    PairedOutcome,
    ThresholdConfirmation,
    TradeNote,
    TrendRead,
)
from aureon.models.enums import Direction, SessionName, TrendBias
from aureon.storage.assessment_repository import AssessmentRepository
from aureon.storage.note_repository import TradeNoteRepository

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
SYMBOL = "XAUUSD"
USER = "user-1"


# ── Wilson ────────────────────────────────────────────────────────────────────


def test_the_interval_matches_the_closed_form() -> None:
    """Computed by hand from the formula rather than copied from the implementation.

    A test that re-derived the number the same way the code does would pass on any
    consistent mistake, which for an interval published beside every percentage is the
    mistake most worth catching.
    """
    low, high = wilson_interval(10, 30)
    assert low == pytest.approx(0.19232, abs=1e-4)
    assert high == pytest.approx(0.51219, abs=1e-4)


def test_the_extremes_keep_a_width() -> None:
    """The reason Wilson and not the textbook interval.

    ``p ± z·sqrt(p(1-p)/n)`` is exactly zero wide at p=0, so "0 of 30 reached it" would be
    published as a confident 0% — the opposite of what an interval is for.
    """
    low, high = wilson_interval(0, 30)
    assert low == 0.0
    assert high > 0.1

    low, high = wilson_interval(30, 30)
    assert high == 1.0
    assert low < 0.9


def test_it_never_leaves_the_unit_interval() -> None:
    for successes in range(0, 8):
        low, high = wilson_interval(successes, 7)
        assert 0.0 <= low <= high <= 1.0


def test_no_trials_is_not_an_interval() -> None:
    """``None``, not (0, 1): "we measured nothing" and "it could be anything" differ."""
    assert wilson_interval(0, 0) is None


def test_more_successes_than_trials_is_refused() -> None:
    with pytest.raises(ValueError, match="outside"):
        wilson_interval(8, 7)


def test_the_interval_narrows_as_the_cohort_grows() -> None:
    """The property the whole readout rests on, stated as an inequality."""
    small = wilson_interval(5, 10)
    large = wilson_interval(50, 100)
    assert (large[1] - large[0]) < (small[1] - small[0])
    assert Z_95 == pytest.approx(1.96, abs=0.001)


# ── Quantiles ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("size", [2, 5, 40, 41])
@pytest.mark.parametrize("q", [0.25, 0.5, 0.75, 0.9])
def test_the_quantile_is_the_named_definition(size: int, q: float) -> None:
    """Pinned against the stdlib's ``inclusive`` method, which is R type 7 and numpy's
    default — so a trader with the raw cohort and a spreadsheet gets the same number."""
    values = [float(i * 3 % 97) for i in range(size)]
    cuts = stdlib_statistics.quantiles(values, n=1000, method="inclusive")
    assert quantile(values, q) == pytest.approx(cuts[round(q * 1000) - 1])


def test_an_empty_cohort_has_no_quantile() -> None:
    assert quantile([], 0.5) is None
    assert median([]) is None


def test_one_observation_is_its_own_quantile() -> None:
    assert quantile([7.0], 0.9) == 7.0


def test_a_quantile_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ValueError, match="within"):
        quantile([1.0, 2.0], 1.5)


# ── The documents ─────────────────────────────────────────────────────────────


def test_an_empty_cohort_has_no_rate_rather_than_zero() -> None:
    """A rate of zero is a measurement. An empty cohort is the absence of one."""
    assert ThresholdConfirmation(threshold=5.0, reached=0, evaluated=0).rate is None
    assert ThresholdConfirmation(threshold=5.0, reached=0, evaluated=30).rate == 0.0
    assert PairedOutcome(favourable=5.0, adverse=3.0).rate is None


def test_reaching_more_often_than_it_was_measured_is_refused() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        ThresholdConfirmation(threshold=5.0, reached=31, evaluated=30)


def test_the_widening_order_gives_up_the_narrowest_dimension_first() -> None:
    """The order is the claim: a cohort that stopped matching on volatility regime is
    answering a different question, so the regime is the last thing surrendered."""
    assert WIDENING_ORDER == ("wick_tag", "price_vs_va", "volatility_regime")
    assert MIN_COHORT == 30


def test_the_symbol_agent_direction_and_session_are_not_droppable() -> None:
    """Nothing in the widening order touches them, and that is deliberate: a cohort mixing
    them would be about a different instrument, a different signal or a different hour."""
    fixed = {"symbol", "agent_name", "direction", "session"}
    assert fixed.isdisjoint(WIDENING_ORDER)


def test_a_note_finds_its_tags_in_what_was_written() -> None:
    note = TradeNote(
        note_id="nt-1",
        trade_id="t-1",
        author=USER,
        text="Chased it. #London #range — same as the last #london one.",
    )
    assert note.tags == ("london", "range")


def test_an_empty_note_is_refused() -> None:
    """A row that says nothing is worse than no row: the review would print a blank under a
    trade and a reader would wonder what was meant."""
    with pytest.raises(ValueError):
        TradeNote(note_id="nt-1", trade_id="t-1", author=USER, text="")


# ── Round trips ───────────────────────────────────────────────────────────────


def assessment(**overrides) -> Assessment:
    base = dict(
        assessment_id="as-1",
        detection_id="det-1",
        symbol=SYMBOL,
        rule_id="XAU_OUTCOME_V2",
        trend_read=TrendRead(
            bias=TrendBias.BULLISH,
            evidence=("ema20 2401.00 above ema50 2395.00",),
            candles=60,
            as_of=NOW,
        ),
        cohort_filter=CohortFilter(
            symbol=SYMBOL,
            agent_name="ema_cross",
            direction=Direction.BUY,
            session=SessionName.LONDON,
            trend_aligned=True,
            volatility_regime="high",
            dropped=("wick_tag",),
        ),
        n=42,
        horizons=(
            HorizonConfirmation(
                horizon_id="h20",
                evaluated=42,
                thresholds=(
                    ThresholdConfirmation(
                        threshold=5.0, reached=21, evaluated=42, ci_low=0.35, ci_high=0.65
                    ),
                ),
                mae_first=12,
                path_ambiguous=2,
            ),
        ),
        tp_estimates=(Estimate(quantile=0.5, points=480.0, price=2405.30),),
        sl_estimates=(Estimate(quantile=0.75, points=260.0, price=2397.90),),
        paired=PairedOutcome(
            favourable=5.0, adverse=3.0, favourable_first=25, evaluated=42
        ),
        created_at=NOW,
    )
    return Assessment(**(base | overrides))


def test_an_assessment_survives_the_round_trip(firestore) -> None:
    repository = AssessmentRepository(firestore)
    repository.store(assessment())
    assert repository.get("as-1") == assessment()


def test_the_same_detection_can_be_assessed_twice(firestore) -> None:
    """An hour later is an hour more history: two statements, two evidence bases. An id
    derived from the detection would have overwritten the first with the second."""
    repository = AssessmentRepository(firestore)
    repository.store(assessment(assessment_id="as-1", n=30, created_at=NOW))
    repository.store(
        assessment(assessment_id="as-2", n=44, created_at=NOW + timedelta(hours=1))
    )

    both = repository.for_detection("det-1")
    assert [a.assessment_id for a in both] == ["as-1", "as-2"]
    assert repository.latest_for_detection("det-1").n == 44


def test_readouts_are_read_back_by_period(firestore) -> None:
    repository = AssessmentRepository(firestore)
    repository.store(assessment(assessment_id="as-old", created_at=NOW - timedelta(days=9)))
    repository.store(assessment(assessment_id="as-in", created_at=NOW - timedelta(days=1)))

    found = repository.in_period(NOW - timedelta(days=7), NOW)
    assert [a.assessment_id for a in found] == ["as-in"]


def test_a_note_is_stored_beside_its_trade_and_never_in_it(firestore) -> None:
    """The reason the collection exists (§45): a CLOSED trade refuses field writes, and a
    note is not a reconciliation."""
    from aureon.storage import paths

    trade_path = paths.trade_path("t-1")
    firestore.docs[trade_path] = {"trade_id": "t-1", "status": "closed"}
    before = dict(firestore.docs[trade_path])

    repository = TradeNoteRepository(firestore)
    first = repository.add("t-1", author=USER, text="chased it", now=NOW)
    second = repository.add(
        "t-1", author=USER, text="#london", now=NOW + timedelta(minutes=5)
    )

    assert firestore.docs[trade_path] == before, "the trade document was written to"
    assert [n.note_id for n in repository.for_trade("t-1")] == [
        first.note_id,
        second.note_id,
    ]
    assert repository.for_trade("t-other") == []


def test_notes_are_grouped_for_a_periods_trades(firestore) -> None:
    repository = TradeNoteRepository(firestore)
    repository.add("t-1", author=USER, text="a", now=NOW)
    repository.add("t-2", author=USER, text="b", now=NOW)

    grouped = repository.for_trades(["t-1", "t-2", "t-3"])
    assert [n.text for n in grouped["t-1"]] == ["a"]
    assert [n.text for n in grouped["t-2"]] == ["b"]
    assert grouped["t-3"] == [], "a trade with no notes is an empty list, not a missing key"
