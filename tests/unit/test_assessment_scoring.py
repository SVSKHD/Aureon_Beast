"""Scoring the readouts, and the notes beside them (9D-4, §63).

A readout nobody checks is a readout nobody should trust, so the weekly review asks whether
the target `/monitor` published was reached before the stop it published. The properties
worth pinning are the ones that would quietly flatter or damn the record:

* **both reached is not an answer.** An evaluation records the maximum favourable and maximum
  adverse excursion; when both distances were covered, candle data cannot say which came
  first. ``mfe_at`` and ``mae_at`` are the times of the *extremes*, not of the first touch,
  so ordering by them would be wrong in a way that looks right.
* **neither reached is its own outcome**, not a miss dressed up. "It went against you" and
  "it went nowhere" are different lessons, even though both answer "did the target come
  first?" with no.
* **a rate of zero is not an empty denominator**, the same rule every other rate here follows.
* **a note never becomes a number.** The review prints them; nothing computes with them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.evaluation.rules import XAU_OUTCOME_V2
from aureon.models.assessment import (
    Assessment,
    CohortFilter,
    Estimate,
    HorizonConfirmation,
    ThresholdConfirmation,
    TradeNote,
    TrendRead,
)
from aureon.models.enums import (
    Direction,
    HorizonStatus,
    PathClassification,
    SessionName,
    TrendBias,
)
from aureon.models.evaluation import DetectionEvaluation, HorizonResult, threshold_key
from aureon.reviews.assessment_scoring import (
    HIT,
    MISS,
    NEITHER,
    NOT_SCORED,
    UNRESOLVED,
    cohort_key,
    score_assessments,
    score_one,
)

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
SYMBOL = "XAUUSD"
HORIZON = XAU_OUTCOME_V2.horizons[3].id  # m60, the default estimate horizon

TARGET = 400.0
STOP = 150.0


def assessment(
    ident: str = "as-1",
    *,
    detection_id: str = "det-1",
    target: float | None = TARGET,
    stop: float | None = STOP,
    insufficient: bool = False,
    dropped: tuple[str, ...] = (),
    session: SessionName | None = SessionName.LONDON,
) -> Assessment:
    return Assessment(
        assessment_id=ident,
        detection_id=detection_id,
        symbol=SYMBOL,
        rule_id=XAU_OUTCOME_V2.rule_id,
        trend_read=TrendRead(bias=TrendBias.BULLISH, candles=60, as_of=NOW),
        cohort_filter=CohortFilter(
            symbol=SYMBOL,
            agent_name="ema_cross",
            direction=Direction.BUY,
            session=session,
            dropped=dropped,
        ),
        n=0 if insufficient else 40,
        confirmations=()
        if insufficient
        else (
            HorizonConfirmation(
                horizon_id=HORIZON,
                evaluated=40,
                thresholds=(
                    ThresholdConfirmation(threshold=3.0, reached=20, evaluated=40),
                ),
            ),
        ),
        tp_estimates=()
        if target is None
        else (Estimate(quantile=0.5, points=target, price=2404.0),),
        sl_estimates=()
        if stop is None
        else (Estimate(quantile=0.75, points=stop, price=2398.5),),
        insufficient=insufficient,
        created_at=NOW,
    )


def evaluation(
    detection_id: str = "det-1",
    *,
    mfe: float,
    mae: float,
    horizon_id: str = HORIZON,
    complete: bool = True,
) -> DetectionEvaluation:
    reached = {threshold_key(t): False for t in XAU_OUTCOME_V2.thresholds}
    result = (
        HorizonResult(
            horizon_id=horizon_id,
            status=HorizonStatus.COMPLETE,
            future_high=2410.0,
            future_low=2395.0,
            mfe=mfe,
            mae=mae,
            reached=reached,
            path=PathClassification.NONE,
            candles_seen=12,
            completed_at=NOW,
        )
        if complete
        else HorizonResult(horizon_id=horizon_id, status=HorizonStatus.PENDING)
    )
    return DetectionEvaluation(
        detection_id=detection_id,
        rule_id=XAU_OUTCOME_V2.rule_id,
        reference_price=XAU_OUTCOME_V2.reference_price,
        reference_value=2400.0,
        horizons=(result,),
    )


# ── One readout ───────────────────────────────────────────────────────────────


def test_the_target_reached_and_the_stop_untouched_is_a_hit() -> None:
    assert score_one(assessment(), evaluation(mfe=500.0, mae=100.0)) == HIT


def test_the_stop_reached_and_the_target_untouched_is_a_miss() -> None:
    assert score_one(assessment(), evaluation(mfe=200.0, mae=300.0)) == MISS


def test_both_reached_is_unresolved_rather_than_guessed() -> None:
    """The order was never observed. Ordering by the times of the extremes would be wrong
    in a way that looks right, which is worse than saying nothing."""
    assert score_one(assessment(), evaluation(mfe=500.0, mae=300.0)) == UNRESOLVED


def test_neither_reached_is_its_own_outcome() -> None:
    """Still not a hit — the question is whether the target came first, and it did not —
    but a target nobody got near is a different lesson from one price ran away from."""
    assert score_one(assessment(), evaluation(mfe=100.0, mae=50.0)) == NEITHER


def test_exactly_at_the_quantile_counts_as_reached() -> None:
    """The quantile is "at least this far", so the boundary belongs inside it. Off by one
    here moves every borderline case to the other column."""
    assert score_one(assessment(), evaluation(mfe=TARGET, mae=STOP - 1)) == HIT
    assert score_one(assessment(), evaluation(mfe=TARGET - 1, mae=STOP)) == MISS


def test_a_readout_that_published_no_target_is_not_scored() -> None:
    assert score_one(assessment(insufficient=True), evaluation(mfe=999.0, mae=0.0)) == NOT_SCORED
    assert score_one(assessment(target=None), evaluation(mfe=999.0, mae=0.0)) == NOT_SCORED


def test_an_unevaluated_detection_is_not_scored() -> None:
    assert score_one(assessment(), None) == NOT_SCORED


def test_a_pending_horizon_is_not_scored() -> None:
    """§22 again: unknown is not failure, and a readout whose horizon has not closed has
    not been shown to be wrong."""
    assert score_one(assessment(), evaluation(mfe=500.0, mae=10.0, complete=False)) == NOT_SCORED


def test_it_scores_the_horizon_the_estimates_were_measured_over() -> None:
    """Comparing a sixty-minute quantile against a five-candle outcome would be comparing
    two questions and calling the difference a result."""
    other = evaluation(mfe=500.0, mae=10.0, horizon_id=XAU_OUTCOME_V2.horizons[0].id)
    assert score_one(assessment(), other) == NOT_SCORED


# ── A period ──────────────────────────────────────────────────────────────────


def test_a_period_is_counted_into_its_buckets() -> None:
    assessments = [
        assessment("as-hit", detection_id="d1"),
        assessment("as-miss", detection_id="d2"),
        assessment("as-neither", detection_id="d3"),
        assessment("as-unres", detection_id="d4"),
        assessment("as-none", detection_id="d5", insufficient=True),
    ]
    evaluations = {
        "d1": evaluation("d1", mfe=500.0, mae=10.0),
        "d2": evaluation("d2", mfe=10.0, mae=500.0),
        "d3": evaluation("d3", mfe=10.0, mae=10.0),
        "d4": evaluation("d4", mfe=500.0, mae=500.0),
        "d5": evaluation("d5", mfe=500.0, mae=10.0),
    }

    scores = score_assessments(assessments, evaluations)
    assert (scores.total, scores.hit, scores.miss) == (5, 1, 1)
    assert (scores.neither, scores.unresolved, scores.not_scored) == (1, 1, 1)
    assert scores.resolved == 3
    assert scores.hit_rate == pytest.approx(1 / 3)


def test_nothing_resolved_has_no_rate_rather_than_zero() -> None:
    scores = score_assessments(
        [assessment("as-1", detection_id="d1")],
        {"d1": evaluation("d1", mfe=500.0, mae=500.0)},
    )
    assert scores.unresolved == 1 and scores.resolved == 0
    assert scores.hit_rate is None


def test_an_empty_period_has_no_rate() -> None:
    assert score_assessments([], {}).hit_rate is None


def test_cohorts_are_counted_apart() -> None:
    """"It holds when the regime matched" and "it holds in general" are different claims,
    and the widening is what makes the difference visible."""
    assessments = [
        assessment("as-1", detection_id="d1"),
        assessment("as-2", detection_id="d2", dropped=("wick_tag", "price_vs_va")),
    ]
    evaluations = {
        "d1": evaluation("d1", mfe=500.0, mae=10.0),
        "d2": evaluation("d2", mfe=10.0, mae=500.0),
    }
    scores = score_assessments(assessments, evaluations)
    assert set(scores.by_cohort) == {
        "ema_cross|buy|london|exact",
        "ema_cross|buy|london|dropped:price_vs_va+wick_tag",
    }
    assert scores.by_cohort["ema_cross|buy|london|exact"] == (1, 1)
    assert scores.by_cohort["ema_cross|buy|london|dropped:price_vs_va+wick_tag"] == (0, 1)


def test_the_cohort_key_is_stable_whatever_order_things_were_dropped_in() -> None:
    """A rate keyed by a dict's iteration order would move between runs of the same week."""
    one = cohort_key(assessment(dropped=("wick_tag", "price_vs_va")))
    two = cohort_key(assessment(dropped=("price_vs_va", "wick_tag")))
    assert one == two


def test_a_cohort_with_no_session_says_so_rather_than_reading_as_one() -> None:
    assert "any_session" in cohort_key(assessment(session=None))


# ── Notes ─────────────────────────────────────────────────────────────────────


def test_the_review_groups_trades_by_the_traders_own_tags() -> None:
    """Their vocabulary, not one Aureon invented."""
    from aureon.models.base import MarketTime
    from aureon.models.enums import TradeStatus
    from aureon.models.trade import Trade
    from aureon.reviews.aggregate import PeriodData, _note_fields

    def trade(ident: str, minutes: int) -> Trade:
        return Trade(
            trade_id=ident,
            mt5_position_id=int(ident.split("-")[-1]),
            symbol=SYMBOL,
            direction=Direction.BUY,
            volume=0.1,
            open_price=2400.0,
            open_time=MarketTime.from_utc(NOW + timedelta(minutes=minutes), "Europe/Athens"),
            close_time=MarketTime.from_utc(
                NOW + timedelta(minutes=minutes + 30), "Europe/Athens"
            ),
            close_price=2401.0,
            closed_volume=0.1,
            status=TradeStatus.CLOSED,
        )

    def note(ident: str, trade_id: str, text: str) -> TradeNote:
        return TradeNote(note_id=ident, trade_id=trade_id, author="u", text=text, at=NOW)

    data = PeriodData(
        trades=[trade("t-1", 0), trade("t-2", 5), trade("t-3", 10)],
        notes={
            "t-1": [note("n1", "t-1", "chased it #chased #london")],
            "t-2": [
                note("n2", "t-2", "fine #london"),
                note("n3", "t-2", "size was wrong #size"),
            ],
        },
    )

    fields = _note_fields(data)
    assert fields["trade_notes"] == {
        "t-1": ("chased it #chased #london",),
        "t-2": ("fine #london", "size was wrong #size"),
    }
    assert fields["trades_by_tag"]["london"] == ("t-1", "t-2")
    assert fields["trades_by_tag"]["chased"] == ("t-1",)
    assert fields["trades_by_tag"]["size"] == ("t-2",)
    assert "t-3" not in fields["trade_notes"], "a trade with no note gets no empty row"


def test_a_note_changes_no_counted_number() -> None:
    """The line that must never move: notes sit beside the outcomes, they do not feed them."""
    from aureon.models.base import MarketTime
    from aureon.models.enums import TradeStatus
    from aureon.models.trade import Trade
    from aureon.reviews.aggregate import PeriodData, aggregate

    trade = Trade(
        trade_id="t-1",
        mt5_position_id=1,
        symbol=SYMBOL,
        direction=Direction.BUY,
        volume=0.1,
        open_price=2400.0,
        open_time=MarketTime.from_utc(NOW, "Europe/Athens"),
        close_time=MarketTime.from_utc(NOW + timedelta(minutes=30), "Europe/Athens"),
        close_price=2401.0,
        closed_volume=0.1,
        status=TradeStatus.CLOSED,
    )
    bare = PeriodData(trades=[trade])
    noted = PeriodData(
        trades=[trade],
        notes={
            "t-1": [
                TradeNote(
                    note_id="n1",
                    trade_id="t-1",
                    author="u",
                    text="this was a great trade #win #brilliant",
                    at=NOW,
                )
            ]
        },
    )
    kwargs = {"market_tz": "Europe/Athens", "infer_window_minutes": 10}
    assert aggregate(bare, XAU_OUTCOME_V2, **kwargs) == aggregate(
        noted, XAU_OUTCOME_V2, **kwargs
    )
