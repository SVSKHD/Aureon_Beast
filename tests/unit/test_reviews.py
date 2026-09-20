"""Reviews: machine observation vs human execution (§50, §61-§65).

A synthetic period with counts known by construction, so every expectation is arithmetic
rather than a number copied from a previous run.

The four cases the phase names are here: known counts in → known review out, a detection
with only PENDING horizons contributes zero to reached-N, an inferred link never mutates a
trade, and a re-run is byte-identical.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.evaluation.rules import EMA_OUTCOME_V1, HORIZON_5_CANDLES
from aureon.models.base import MarketTime, utc_now
from aureon.models.detection import Detection, SessionContext
from aureon.models.enums import (
    Direction,
    ExecutionClassification,
    HorizonStatus,
    LinkType,
    PathClassification,
    ReferencePrice,
    SessionName,
    Timeframe,
    TradeStatus,
)
from aureon.models.evaluation import DetectionEvaluation, HorizonResult
from aureon.models.review import InferredLink
from aureon.models.trade import Trade
from aureon.reviews.aggregate import (
    PeriodData,
    aggregate,
    build_daily_review,
    build_weekly_review,
    excluded_summary,
)
from aureon.reviews.linking import classify, classify_period, infer_links
from aureon.reviews.periods import day_period

TZ = "Europe/Athens"
SYMBOL = "XAUUSD"
# A Wednesday inside London, so sessions are unambiguous.
BASE = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
RULE = EMA_OUTCOME_V1


def detection(
    *,
    ident: str,
    minutes: float = 0.0,
    direction: Direction | None = Direction.BUY,
    symbol: str = SYMBOL,
    agent: str = "ema_cross",
    session: SessionName = SessionName.LONDON,
) -> Detection:
    moment = BASE + timedelta(minutes=minutes)
    return Detection(
        detection_id=ident,
        account_scope="primary",
        symbol=symbol,
        timeframe=Timeframe.M5,
        agent_name=agent,
        agent_version="1.0.0",
        event_key="bullish" if direction is Direction.BUY else "bearish",
        direction=direction,
        detected_at=MarketTime.from_utc(moment, TZ),
        candle_open_time=MarketTime.from_utc(moment - timedelta(minutes=5), TZ),
        price=2400.0,
        session=SessionContext(session=session, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
    )


def trade(
    *,
    ident: str,
    minutes: float = 5.0,
    direction: Direction = Direction.BUY,
    symbol: str = SYMBOL,
    pnl: float | None = 100.0,
    status: TradeStatus = TradeStatus.CLOSED,
    detection_id: str | None = None,
) -> Trade:
    opened = BASE + timedelta(minutes=minutes)
    return Trade(
        trade_id=ident,
        mt5_position_id=abs(hash(ident)) % 100_000,
        symbol=symbol,
        direction=direction,
        volume=0.10,
        open_price=2400.0,
        open_time=MarketTime.from_utc(opened, TZ),
        status=status,
        close_time=(
            MarketTime.from_utc(opened + timedelta(minutes=30), TZ)
            if status is TradeStatus.CLOSED
            else None
        ),
        closed_volume=0.10 if status is TradeStatus.CLOSED else 0.0,
        realized_pnl=pnl if status is TradeStatus.CLOSED else None,
        detection_id=detection_id,
        link_type=LinkType.EXPLICIT if detection_id else None,
    )


def complete_evaluation(
    detection_id: str, *, reached: dict[str, bool], horizon_id: str = HORIZON_5_CANDLES
) -> DetectionEvaluation:
    return DetectionEvaluation(
        detection_id=detection_id,
        rule_id=RULE.rule_id,
        reference_price=ReferencePrice.NEXT_OPEN,
        horizons=(
            HorizonResult(
                horizon_id=horizon_id,
                status=HorizonStatus.COMPLETE,
                future_high=2410.0,
                future_low=2395.0,
                mfe=10.0,
                mae=-5.0,
                reached=dict(reached),
                time_to={k: (60.0 if v else None) for k, v in reached.items()},
                path=PathClassification.MFE_FIRST,
                completed_at=utc_now(),
            ),
        ),
    )


def pending_evaluation(detection_id: str, *, horizon_id: str = HORIZON_5_CANDLES):
    return DetectionEvaluation(
        detection_id=detection_id,
        rule_id=RULE.rule_id,
        reference_price=ReferencePrice.NEXT_OPEN,
        horizons=(HorizonResult(horizon_id=horizon_id),),
    )


ALL_REACHED = {"3": True, "5": True, "10": True, "15": False, "20": False}
NONE_REACHED = {"3": False, "5": False, "10": False, "15": False, "20": False}


def counted(data: PeriodData, **kwargs):
    return aggregate(data, RULE, market_tz=TZ, infer_window_minutes=30, **kwargs)


# ── Known counts in, known counts out ─────────────────────────────────────────


def test_a_synthetic_period_produces_the_counts_it_was_built_from() -> None:
    data = PeriodData(
        detections=[
            detection(ident="d1"),
            detection(ident="d2", minutes=60),
            detection(ident="d3", minutes=120, agent="liquidity"),
        ],
        trades=[trade(ident="t1"), trade(ident="t2", minutes=65, pnl=-40.0)],
    )
    totals = counted(data)

    assert totals.detections_total == 3
    assert totals.detections_by_agent == {"ema_cross": 2, "liquidity": 1}
    assert totals.detections_by_session == {SessionName.LONDON: 3}
    assert totals.trades_total == 2
    assert totals.trades_closed == 2
    assert totals.realized_pnl == pytest.approx(60.0)  # 100 - 40


def test_only_realised_pnl_is_counted() -> None:
    """An open trade's floating profit would change every time the review was regenerated."""
    data = PeriodData(
        trades=[
            trade(ident="t1", pnl=100.0),
            trade(ident="t2", minutes=10, status=TradeStatus.OPEN),
        ]
    )
    totals = counted(data)
    assert totals.trades_total == 2
    assert totals.trades_closed == 1
    assert totals.realized_pnl == pytest.approx(100.0)


def test_reached_counts_come_from_complete_horizons() -> None:
    data = PeriodData(
        detections=[detection(ident="d1"), detection(ident="d2", minutes=30)],
        evaluations={
            "d1": complete_evaluation("d1", reached=ALL_REACHED),
            "d2": complete_evaluation("d2", reached=NONE_REACHED),
        },
    )
    totals = counted(data)
    horizon = next(h for h in totals.horizons if h.horizon_id == HORIZON_5_CANDLES)
    by_threshold = {t.threshold: t for t in horizon.thresholds}

    assert by_threshold[3.0].evaluated == 2
    assert by_threshold[3.0].reached == 1
    assert by_threshold[15.0].reached == 0


# ── The central rule: PENDING is not a miss ───────────────────────────────────


def test_a_pending_only_detection_contributes_zero_to_reached_n() -> None:
    """The case the phase names explicitly.

    It must not appear in a reached-N denominator at all. Counting it as a miss would make
    every figure in the review pessimistic, with nothing in the output to reveal it.
    """
    data = PeriodData(
        detections=[detection(ident="d1"), detection(ident="d2", minutes=30)],
        evaluations={
            "d1": complete_evaluation("d1", reached=ALL_REACHED),
            "d2": pending_evaluation("d2"),
        },
    )
    totals = counted(data)
    horizon = next(h for h in totals.horizons if h.horizon_id == HORIZON_5_CANDLES)
    by_threshold = {t.threshold: t for t in horizon.thresholds}

    # One evaluated, not two.
    assert by_threshold[3.0].evaluated == 1
    assert by_threshold[3.0].reached == 1
    # And the excluded work is reported rather than dropped.
    assert totals.pending_excluded == 1


def test_the_excluded_counts_are_reported_alongside_the_totals() -> None:
    data = PeriodData(
        detections=[detection(ident=f"d{i}", minutes=i * 10) for i in range(3)],
        evaluations={
            "d0": complete_evaluation("d0", reached=ALL_REACHED),
            "d1": pending_evaluation("d1"),
            "d2": DetectionEvaluation(
                detection_id="d2",
                rule_id=RULE.rule_id,
                reference_price=ReferencePrice.NEXT_OPEN,
                horizons=(
                    HorizonResult(
                        horizon_id=HORIZON_5_CANDLES,
                        status=HorizonStatus.INVALID,
                        invalid_reason="candle gap",
                    ),
                ),
            ),
        },
    )
    totals = counted(data)
    assert totals.pending_excluded == 1
    assert totals.invalid_excluded == 1


def test_the_excluded_summary_reports_how_much_is_answered() -> None:
    data = PeriodData(
        detections=[detection(ident="d1"), detection(ident="d2", minutes=10)],
        evaluations={
            "d1": complete_evaluation("d1", reached=ALL_REACHED),
            "d2": pending_evaluation("d2"),
        },
    )
    summary = excluded_summary(data)
    assert "1 complete" in summary
    assert "1 pending" in summary
    assert "50% answered" in summary


def test_an_empty_period_reports_no_evaluations() -> None:
    assert excluded_summary(PeriodData()) == "no evaluations in this period"


# ── §50: inferred links ───────────────────────────────────────────────────────


def test_a_trade_shortly_after_a_matching_detection_is_linked() -> None:
    links = infer_links(
        [detection(ident="d1")], [trade(ident="t1", minutes=5)], market_tz=TZ, window_minutes=30
    )
    assert len(links) == 1
    assert links[0].detection_id == "d1"
    assert links[0].trade_id == "t1"
    assert 0.0 < links[0].confidence <= 1.0
    assert links[0].link_type is LinkType.INFERRED


@pytest.mark.parametrize(
    "kwargs,why",
    [
        ({"symbol": "EURUSD"}, "different symbol"),
        ({"direction": Direction.SELL}, "different direction"),
        ({"minutes": 200.0}, "outside the window"),
        ({"minutes": -5.0}, "opened BEFORE the detection"),
    ],
)
def test_a_trade_that_fails_any_criterion_is_not_linked(kwargs: dict, why: str) -> None:
    """Each criterion alone is far too weak; all four must hold (§50)."""
    links = infer_links(
        [detection(ident="d1")],
        [trade(ident="t1", **kwargs)],
        market_tz=TZ,
        window_minutes=30,
    )
    assert links == [], why


def test_a_link_never_spans_a_session_boundary() -> None:
    """The context the detection described has already changed."""
    # Detection late in London; trade just after the New York boundary (18:00 Athens).
    late_london = detection(ident="d1", minutes=4 * 60 + 55)
    ny_trade = trade(ident="t1", minutes=5 * 60 + 5)
    links = infer_links([late_london], [ny_trade], market_tz=TZ, window_minutes=30)
    assert links == []


def test_a_context_only_detection_is_never_linked() -> None:
    """It asserts no direction, so "same direction" is undefined."""
    links = infer_links(
        [detection(ident="ctx", direction=None)],
        [trade(ident="t1", minutes=5)],
        market_tz=TZ,
        window_minutes=30,
    )
    assert links == []


def test_an_explicitly_linked_trade_is_left_alone() -> None:
    """A human already said which detection it was; a competing guess is noise."""
    links = infer_links(
        [detection(ident="d1")],
        [trade(ident="t1", minutes=5, detection_id="d-chosen")],
        market_tz=TZ,
        window_minutes=30,
    )
    assert links == []


def test_a_trade_gets_at_most_one_link() -> None:
    """Two links would double-count the trade in every aggregate downstream."""
    links = infer_links(
        [detection(ident="d1"), detection(ident="d2", minutes=2), detection(ident="d3", minutes=4)],
        [trade(ident="t1", minutes=5)],
        market_tz=TZ,
        window_minutes=30,
    )
    assert len(links) == 1
    # The closest detection wins.
    assert links[0].detection_id == "d3"


def test_a_sooner_trade_ranks_higher() -> None:
    near = infer_links(
        [detection(ident="d1")], [trade(ident="t1", minutes=1)], market_tz=TZ, window_minutes=30
    )[0]
    far = infer_links(
        [detection(ident="d1")], [trade(ident="t2", minutes=29)], market_tz=TZ, window_minutes=30
    )[0]
    assert near.confidence > far.confidence


def test_inferred_links_never_mutate_a_trade() -> None:
    """decision 8 and §50. The whole reason they live on the review.

    Written onto the trade, a guess would become permanent and would eventually be read as
    though a human had stated it.
    """
    original = trade(ident="t1", minutes=5)
    before = original.model_dump(mode="json")

    links = infer_links([detection(ident="d1")], [original], market_tz=TZ, window_minutes=30)

    assert links, "expected a link, so the assertion below means something"
    assert original.model_dump(mode="json") == before
    assert original.detection_id is None
    assert original.link_type is None


def test_a_trade_cannot_carry_an_inferred_link_at_all() -> None:
    """Enforced by the model, not only by convention (decision 8)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="inferred"):
        trade(ident="t1").model_copy(
            update={"detection_id": "d1"}
        ).model_validate(
            {
                **trade(ident="t1").model_dump(),
                "detection_id": "d1",
                "link_type": LinkType.INFERRED,
            }
        )


def test_links_are_ordered_deterministically() -> None:
    """So a re-run is byte-identical."""
    detections = [detection(ident=f"d{i}", minutes=i) for i in range(5)]
    trades = [trade(ident=f"t{i}", minutes=i + 1) for i in range(5)]
    first = infer_links(detections, trades, market_tz=TZ, window_minutes=30)
    second = infer_links(
        list(reversed(detections)),
        list(reversed(trades)),
        market_tz=TZ,
        window_minutes=30,
    )
    assert [(link.detection_id, link.trade_id) for link in first] == [
        (link.detection_id, link.trade_id) for link in second
    ]


# ── §64: classification ───────────────────────────────────────────────────────


def test_a_traded_detection_that_reached_is_classified_as_taken_and_reached() -> None:
    verdict = classify(
        detection(ident="d1"),
        complete_evaluation("d1", reached=ALL_REACHED),
        threshold=3.0,
        horizon_id=HORIZON_5_CANDLES,
        was_traded=True,
    )
    assert verdict is ExecutionClassification.TAKEN_AND_REACHED


def test_an_untaken_detection_that_reached_is_a_missed_opportunity() -> None:
    verdict = classify(
        detection(ident="d1"),
        complete_evaluation("d1", reached=ALL_REACHED),
        threshold=3.0,
        horizon_id=HORIZON_5_CANDLES,
        was_traded=False,
    )
    assert verdict is ExecutionClassification.MISSED_AND_REACHED


def test_an_untaken_detection_that_did_not_reach_was_correctly_skipped() -> None:
    verdict = classify(
        detection(ident="d1"),
        complete_evaluation("d1", reached=NONE_REACHED),
        threshold=3.0,
        horizon_id=HORIZON_5_CANDLES,
        was_traded=False,
    )
    assert verdict is ExecutionClassification.MISSED_AND_NOT_REACHED


@pytest.mark.parametrize("traded", [True, False])
def test_a_pending_outcome_is_unknown_never_a_miss(traded: bool) -> None:
    """The distinction that keeps the comparison honest."""
    verdict = classify(
        detection(ident="d1"),
        pending_evaluation("d1"),
        threshold=3.0,
        horizon_id=HORIZON_5_CANDLES,
        was_traded=traded,
    )
    assert verdict is ExecutionClassification.UNKNOWN
    assert verdict.is_conclusive is False


def test_a_missing_evaluation_is_unknown() -> None:
    assert (
        classify(
            detection(ident="d1"),
            None,
            threshold=3.0,
            horizon_id=HORIZON_5_CANDLES,
            was_traded=False,
        )
        is ExecutionClassification.UNKNOWN
    )


def test_a_trade_with_no_detection_is_discretionary() -> None:
    counts = classify_period(
        [],
        {},
        [trade(ident="t1")],
        [],
        threshold=3.0,
        horizon_id=HORIZON_5_CANDLES,
    )
    assert counts[ExecutionClassification.DISCRETIONARY] == 1


def test_classification_counts_cover_a_whole_period() -> None:
    detections = [
        detection(ident="taken"),
        detection(ident="missed", minutes=90),
        detection(ident="unknown", minutes=150),
    ]
    trades = [trade(ident="t-taken", minutes=5), trade(ident="t-own", minutes=200)]
    links = [InferredLink(detection_id="taken", trade_id="t-taken", confidence=0.9)]
    counts = classify_period(
        detections,
        {
            "taken": complete_evaluation("taken", reached=ALL_REACHED),
            "missed": complete_evaluation("missed", reached=ALL_REACHED),
            "unknown": pending_evaluation("unknown"),
        },
        trades,
        links,
        threshold=3.0,
        horizon_id=HORIZON_5_CANDLES,
    )
    assert counts[ExecutionClassification.TAKEN_AND_REACHED] == 1
    assert counts[ExecutionClassification.MISSED_AND_REACHED] == 1
    assert counts[ExecutionClassification.UNKNOWN] == 1
    assert counts[ExecutionClassification.DISCRETIONARY] == 1


def test_context_only_detections_are_not_missed_opportunities() -> None:
    """They were never actionable, so they cannot have been missed."""
    counts = classify_period(
        [detection(ident="ctx", direction=None)],
        {},
        [],
        [],
        threshold=3.0,
        horizon_id=HORIZON_5_CANDLES,
    )
    assert sum(counts.values()) == 0


# ── Documents, and re-run stability ───────────────────────────────────────────


def sample_period() -> PeriodData:
    return PeriodData(
        detections=[detection(ident="d1"), detection(ident="d2", minutes=45)],
        evaluations={
            "d1": complete_evaluation("d1", reached=ALL_REACHED),
            "d2": pending_evaluation("d2"),
        },
        trades=[trade(ident="t1", minutes=5)],
    )


def daily() -> object:
    period = day_period("2026-09-16", TZ)
    return build_daily_review(
        sample_period(),
        RULE,
        market_date=period.market_date,
        period_start=period.start,
        period_end=period.end,
        market_tz=TZ,
        infer_window_minutes=30,
    )


def test_a_daily_review_carries_the_required_fields() -> None:
    review = daily()
    assert review.market_date == "2026-09-16"
    assert review.evaluation_rule_id == RULE.rule_id
    assert review.detections_total == 2
    assert review.pending_horizons_excluded == 1
    assert review.trades_total == 1
    assert review.horizons
    assert review.notes and "unknown=" in review.notes


def test_a_review_re_run_is_byte_identical() -> None:
    """The case the phase names. A review is a pure function of its data."""
    first, second = daily(), daily()
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_a_review_is_stable_under_input_reordering() -> None:
    """Firestore does not promise an order, so the review must impose one."""
    period = day_period("2026-09-16", TZ)
    forward = sample_period()
    reversed_data = PeriodData(
        detections=list(reversed(forward.detections)),
        evaluations=dict(reversed(list(forward.evaluations.items()))),
        trades=list(reversed(forward.trades)),
    )
    kwargs = dict(
        market_date=period.market_date,
        period_start=period.start,
        period_end=period.end,
        market_tz=TZ,
        infer_window_minutes=30,
    )
    a = build_daily_review(forward, RULE, **kwargs)
    b = build_daily_review(reversed_data, RULE, **kwargs)
    assert a.model_dump(mode="json") == b.model_dump(mode="json")


def test_a_weekly_review_carries_its_iso_identity() -> None:
    from aureon.reviews.periods import week_period

    period = week_period(2026, 38, TZ)
    review = build_weekly_review(
        sample_period(),
        RULE,
        iso_year=2026,
        iso_week=38,
        period_start=period.start,
        period_end=period.end,
        market_tz=TZ,
        infer_window_minutes=30,
        daily_review_ids=("2026-09-16", "2026-09-15"),
    )
    assert review.iso_year == 2026
    assert review.iso_week == 38
    # Sorted, so a re-run is byte-identical.
    assert review.daily_review_ids == ("2026-09-15", "2026-09-16")


def test_the_notes_line_names_the_unknown_count() -> None:
    """A reader who cannot see how much was unresolved cannot judge the rest."""
    review = daily()
    assert "unknown=" in (review.notes or "")
    assert "missed_and_reached=" in (review.notes or "")


def test_an_empty_period_produces_a_valid_empty_review() -> None:
    period = day_period("2026-09-16", TZ)
    review = build_daily_review(
        PeriodData(),
        RULE,
        market_date=period.market_date,
        period_start=period.start,
        period_end=period.end,
        market_tz=TZ,
        infer_window_minutes=30,
    )
    assert review.detections_total == 0
    assert review.trades_total == 0
    assert review.realized_pnl == 0.0
    assert review.inferred_links == ()


# ── Which period a scheduled run picks ────────────────────────────────────────


@pytest.mark.parametrize(
    ("day", "weekday", "expected"),
    [
        (17, "Thu", (2026, 37)),
        (18, "Fri", (2026, 37)),  # Friday has not closed yet
        (19, "Sat", (2026, 38)),  # the week that just traded
        (20, "Sun", (2026, 38)),
        (21, "Mon", (2026, 38)),
        (24, "Thu", (2026, 38)),
    ],
)
def test_the_weekly_default_is_the_week_that_finished_trading(
    day: int, weekday: str, expected: tuple[int, int]
) -> None:
    """§63 asks for the review to be generated after Friday's close.

    So a cron firing on Saturday must name the week that has just traded. "Seven days ago"
    gets this wrong on Friday, Saturday and Sunday -- it names the week *before* the one
    that just ended, and the resulting document carries this week's heading over last week's
    numbers with nothing to reveal the substitution.

    Every weekday is pinned because the rule has to hold for whatever day the operator's
    cron happens to fire on, and because the one that is wrong is the one they would choose.
    """
    from aureon.reviews.periods import previous_iso_week

    now = datetime(2026, 9, day, 12, tzinfo=UTC)
    assert now.strftime("%a") == weekday, "the fixture date is not the weekday it claims"
    assert previous_iso_week(TZ, now=now) == expected


def test_the_weekly_default_never_names_a_week_still_trading() -> None:
    """The invariant behind the table above, checked across a full year.

    A review of a week that is still running is indistinguishable from a final one.
    """
    from datetime import date

    from aureon.reviews.periods import previous_iso_week

    for offset in range(366):
        moment = datetime(2026, 1, 1, 12, tzinfo=UTC) + timedelta(days=offset)
        year, week = previous_iso_week(TZ, now=moment)
        friday = date.fromisocalendar(year, week, 5)
        market_today = moment.date()
        assert friday < market_today, (
            f"on {market_today} the default names {year}-W{week:02d}, whose Friday "
            f"({friday}) has not finished"
        )


def test_the_daily_default_is_yesterday() -> None:
    """Today is still accumulating."""
    from aureon.reviews.periods import previous_market_date

    # 23:30 UTC is already the next day in Athens, so "yesterday" must be computed on the
    # market clock: this is the 19th there, making yesterday the 18th.
    assert previous_market_date(TZ, now=datetime(2026, 9, 18, 23, 30, tzinfo=UTC)) == (
        "2026-09-18"
    )
    assert previous_market_date(TZ, now=datetime(2026, 9, 18, 12, 0, tzinfo=UTC)) == (
        "2026-09-17"
    )


# ── Grouping outcomes by context tag (§23) ────────────────────────────────────


def tagged_evaluation(
    detection_id: str, *, reached: bool, tags: dict[str, bool]
) -> DetectionEvaluation:
    evaluation = complete_evaluation(
        detection_id, reached={k: reached for k in ("3", "5", "10", "15", "20")}
    )
    return evaluation.model_copy(update={"context_tags": tags})


def test_a_tag_splits_the_population_into_with_and_without() -> None:
    """Known counts in, known comparison out.

    Three tagged detections of which two reached, two untagged of which none did.
    """
    from aureon.reviews.aggregate import compare_by_tag

    data = PeriodData(
        detections=[detection(ident=f"d{i}") for i in range(5)],
        evaluations={
            "d0": tagged_evaluation("d0", reached=True, tags={"has_lower_rejection_wick": True}),
            "d1": tagged_evaluation("d1", reached=True, tags={"has_lower_rejection_wick": True}),
            "d2": tagged_evaluation("d2", reached=False, tags={"has_lower_rejection_wick": True}),
            "d3": tagged_evaluation("d3", reached=False, tags={"has_lower_rejection_wick": False}),
            "d4": tagged_evaluation("d4", reached=False, tags={"has_lower_rejection_wick": False}),
        },
    )

    comparisons = compare_by_tag(data, RULE, horizon_id=HORIZON_5_CANDLES, threshold="3")
    wick = next(c for c in comparisons if c.tag == "has_lower_rejection_wick")

    assert (wick.with_tag_reached, wick.with_tag_complete) == (2, 3)
    assert (wick.without_tag_reached, wick.without_tag_complete) == (0, 2)
    assert wick.with_tag_rate == pytest.approx(2 / 3)
    assert wick.without_tag_rate == pytest.approx(0.0)
    assert wick.difference == pytest.approx(2 / 3)


def test_both_sides_are_always_reported() -> None:
    """A "with" figure alone is unreadable without the "without" beside it."""
    from aureon.reviews.aggregate import compare_by_tag

    data = PeriodData(
        detections=[detection(ident="d0")],
        evaluations={
            "d0": tagged_evaluation(
                "d0", reached=True, tags={"has_lower_rejection_wick": True}
            )
        },
    )
    comparison = next(
        c
        for c in compare_by_tag(data, RULE, horizon_id=HORIZON_5_CANDLES)
        if c.tag == "has_lower_rejection_wick"
    )
    assert comparison.with_tag_complete == 1
    assert comparison.without_tag_complete == 0
    assert comparison.without_tag_rate is None
    assert comparison.difference is None, "no comparison is possible with an empty side"


def test_a_pending_horizon_is_on_neither_side() -> None:
    """COMPLETE only, exactly like every other reached count in this package.

    An unknown outcome is unknown for the tagged and untagged populations alike;
    counting it on either side would make the comparison assert something neither
    population supports.
    """
    from aureon.reviews.aggregate import compare_by_tag

    pending = pending_evaluation("d1").model_copy(
        update={"context_tags": {"has_lower_rejection_wick": True}}
    )
    data = PeriodData(
        detections=[detection(ident="d0"), detection(ident="d1", minutes=30)],
        evaluations={
            "d0": tagged_evaluation("d0", reached=True, tags={"has_lower_rejection_wick": True}),
            "d1": pending,
        },
    )
    comparison = next(
        c
        for c in compare_by_tag(data, RULE, horizon_id=HORIZON_5_CANDLES)
        if c.tag == "has_lower_rejection_wick"
    )
    assert comparison.with_tag_complete == 1
    assert comparison.without_tag_complete == 0


def test_an_evaluation_with_no_tags_counts_as_without() -> None:
    """An evaluation written before tags existed must not vanish from the denominator."""
    from aureon.reviews.aggregate import compare_by_tag

    data = PeriodData(
        detections=[detection(ident="d0")],
        evaluations={"d0": complete_evaluation("d0", reached=ALL_REACHED)},
    )
    comparison = next(
        c
        for c in compare_by_tag(data, RULE, horizon_id=HORIZON_5_CANDLES)
        if c.tag == "has_lower_rejection_wick"
    )
    assert comparison.without_tag_complete == 1
    assert comparison.with_tag_complete == 0


def test_a_thin_comparison_says_so_rather_than_printing_a_percentage() -> None:
    """One detection moves a 4-sample rate by 25 points; the label is the honesty."""
    from aureon.reviews.aggregate import compare_by_tag, render_tag_comparisons

    data = PeriodData(
        detections=[detection(ident=f"d{i}") for i in range(4)],
        evaluations={
            f"d{i}": tagged_evaluation(
                f"d{i}", reached=i < 2, tags={"has_lower_rejection_wick": i % 2 == 0}
            )
            for i in range(4)
        },
    )
    comparisons = compare_by_tag(data, RULE, horizon_id=HORIZON_5_CANDLES)
    wick = next(c for c in comparisons if c.tag == "has_lower_rejection_wick")
    assert wick.comparable is False
    assert "too few to read" in render_tag_comparisons(comparisons)


def test_every_tag_appears_in_the_comparison() -> None:
    from aureon.evaluation.context_tags import CONTEXT_TAGS
    from aureon.reviews.aggregate import compare_by_tag

    data = PeriodData(
        detections=[detection(ident="d0")],
        evaluations={"d0": complete_evaluation("d0", reached=ALL_REACHED)},
    )
    comparisons = compare_by_tag(data, RULE, horizon_id=HORIZON_5_CANDLES)
    assert {c.tag for c in comparisons} == set(CONTEXT_TAGS)


def test_the_comparison_is_deterministic() -> None:
    from aureon.reviews.aggregate import compare_by_tag, render_tag_comparisons

    data = PeriodData(
        detections=[detection(ident=f"d{i}") for i in range(3)],
        evaluations={
            f"d{i}": tagged_evaluation(
                f"d{i}", reached=True, tags={"has_upper_rejection_wick": True}
            )
            for i in range(3)
        },
    )
    first = render_tag_comparisons(compare_by_tag(data, RULE, horizon_id=HORIZON_5_CANDLES))
    second = render_tag_comparisons(compare_by_tag(data, RULE, horizon_id=HORIZON_5_CANDLES))
    assert first == second
