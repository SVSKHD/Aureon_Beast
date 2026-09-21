"""The measured readout, before anything renders it (9D).

The properties worth proving here are the ones that would produce a *plausible* wrong
number rather than an error:

* a cohort that quietly stopped matching on something and answered a different question;
* a percentage published from eleven detections;
* a stop estimate on the wrong side of the entry, which reads as a perfectly sensible price;
* a cohort containing detections made **after** the one being assessed — look-ahead that is
  trivially easy to introduce here, because the rows are simply sitting in a collection.

Every one of those passes an ordinary smoke test. So the cohort is built from synthetic
detections whose properties are known exactly, and each rule is planted and reverted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.evaluation.context_tags import (
    TAG_LOWER_REJECTION_WICK,
    TAG_SESSION_TREND_ALIGNED,
)
from aureon.evaluation.rules import XAU_OUTCOME_V2
from aureon.models.assessment import MIN_COHORT, WIDENING_ORDER
from aureon.models.base import MarketTime
from aureon.models.detection import Detection, IndicatorSnapshot, SessionContext
from aureon.models.enums import (
    Direction,
    HorizonStatus,
    PathClassification,
    SessionName,
    Timeframe,
    TrendBias,
)
from aureon.models.evaluation import DetectionEvaluation, HorizonResult, threshold_key
from aureon.models.market import Candle
from aureon.models.profile import ProfileSummary, VolatilityContext, VolumeProfileRef
from aureon.services.assessment_service import (
    CohortMember,
    build_assessment,
    confirmations,
    estimates,
    paired_outcome,
    population_from,
    read_trend,
    select_cohort,
    swing_counts,
)

TZ = "Europe/Athens"
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
SYMBOL = "XAUUSD"
HORIZON = XAU_OUTCOME_V2.horizons[2].id  # c20
POINT = 0.01


# ── Candles, for the trend read ───────────────────────────────────────────────


def candle(i: int, *, open_: float, high: float, low: float, close: float) -> Candle:
    moment = NOW - timedelta(minutes=5 * (200 - i))
    return Candle(
        symbol=SYMBOL,
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(moment, TZ),
        open=open_,
        high=high,
        low=low,
        close=close,
    )


def zigzag(
    n: int, *, start: float, drift: float, amplitude: float = 1.6
) -> list[Candle]:
    """A market that swings, with an optional drift per cycle.

    Deliberately NOT a clean staircase. A strictly monotonic series has no interior pivot at
    all -- every candle is exceeded by the next one -- so a staircase fixture would report
    zero higher highs for the most obviously trending data imaginable, and a test written on
    one would prove the opposite of what it claimed. Found by building the fixture and
    looking at the counts rather than by assuming.
    """
    shape = (0.0, 1.0, 2.0, 3.0, 2.0, 1.0)
    out = []
    for i in range(n):
        level = start + drift * (i // len(shape)) + shape[i % len(shape)] * amplitude
        out.append(candle(i, open_=level, high=level + 0.2, low=level - 0.2, close=level))
    return out


def ramp(n: int, *, start: float, step: float) -> list[Candle]:
    """A trend with pullbacks: higher highs and higher lows, or the reverse."""
    return zigzag(n, start=start, drift=step)


def chop(n: int, *, base: float = 2400.0) -> list[Candle]:
    """The same swings with no drift: real pivots, every one equal to the last.

    Neutral by construction rather than by accident -- a fixture with no pivots at all would
    also score zero, and would be neutral for a reason that proves nothing.
    """
    return zigzag(n, start=base, drift=0.0)


# ── The trend read ────────────────────────────────────────────────────────────


def test_a_rising_window_reads_bullish_and_shows_its_working() -> None:
    read = read_trend(
        ramp(60, start=2380.0, step=0.4),
        ema_fast=2401.0,
        ema_slow=2395.0,
        ema_fast_earlier=2380.0,
        session_trend="up",
        point=POINT,
        now=NOW,
    )
    assert read.bias is TrendBias.BULLISH
    assert read.candles == 60
    joined = " | ".join(read.evidence)
    assert "ema fast 2401.00 above slow 2395.00" in joined
    assert "higher highs" in joined
    assert "session trend up" in joined


def test_a_falling_window_reads_bearish() -> None:
    read = read_trend(
        ramp(60, start=2420.0, step=-0.4),
        ema_fast=2395.0,
        ema_slow=2401.0,
        ema_fast_earlier=2420.0,
        session_trend="down",
        point=POINT,
        now=NOW,
    )
    assert read.bias is TrendBias.BEARISH


def test_a_tie_is_sideways_rather_than_the_nearer_side() -> None:
    """SIDEWAYS is a real answer. Rounding a tie to bullish or bearish would be inventing
    a direction out of evidence that points both ways."""
    read = read_trend(
        chop(60),
        ema_fast=2401.0,
        ema_slow=2395.0,  # one bullish vote
        ema_fast_earlier=2401.0,  # flat: no vote
        session_trend="down",  # one bearish vote
        point=POINT,
        now=NOW,
    )
    assert read.bias is TrendBias.SIDEWAYS


def test_a_slope_inside_the_flat_band_casts_no_vote() -> None:
    """One point over five hours is noise, and calling it a direction is how a sideways
    market reads as a trend."""
    rising = read_trend(
        chop(60), ema_fast_earlier=2400.0, ema_fast=2400.10, point=POINT, flat_points=50.0
    )
    assert "ema fast moved +10 points" in " | ".join(rising.evidence)
    assert rising.bias is TrendBias.SIDEWAYS


def test_the_evidence_records_volatility_without_voting_on_it() -> None:
    """A regime says how big moves are, not which way they go."""
    quiet = read_trend(
        chop(60), volatility=VolatilityContext(regime="high", bands_version=1), point=POINT
    )
    assert "volatility regime high" in " | ".join(quiet.evidence)
    assert quiet.bias is TrendBias.SIDEWAYS


def test_price_outside_asias_value_area_votes() -> None:
    profile = ProfileSummary(scope="asia", value_area_low=2390.0, value_area_high=2395.0)
    above = read_trend(chop(60, base=2400.0), asia_profile=profile, point=POINT)
    assert "above asia VA" in " | ".join(above.evidence)
    assert above.bias is TrendBias.BULLISH


def test_a_missing_ema_says_unknown_rather_than_guessing() -> None:
    read = read_trend(chop(60), point=POINT)
    joined = " | ".join(read.evidence)
    assert "ema unknown" in joined and "ema slope unknown" in joined


def test_swings_compare_pivots_to_each_other_not_candles_to_neighbours() -> None:
    counts = swing_counts(ramp(40, start=2400.0, step=0.5))
    assert counts.higher_highs > 0 and counts.lower_highs == 0
    assert counts.higher_lows > 0 and counts.lower_lows == 0

    falling = swing_counts(ramp(40, start=2400.0, step=-0.5))
    assert falling.lower_highs > 0 and falling.higher_highs == 0


def test_an_equal_pivot_counts_as_neither() -> None:
    """Two swings at the same price is a level being retested, not a lower high.

    The naive ``else "lower"`` put every equal pivot in the bearish bucket, which reads a
    flat range as a downtrend — and the flat range is exactly the case the sideways verdict
    exists for.
    """
    flat = swing_counts(chop(60))
    assert (flat.higher_highs, flat.lower_highs) == (0, 0)
    assert (flat.higher_lows, flat.lower_lows) == (0, 0)

    # And the fixture is not degenerate: the same shape with a drift does find its pivots,
    # so the zeros above are equal pivots rather than no pivots at all.
    drifting = swing_counts(zigzag(60, start=2400.0, drift=0.05))
    assert drifting.higher_highs > 0 and drifting.higher_lows > 0


# ── The cohort ────────────────────────────────────────────────────────────────


def detection(
    ident: str,
    *,
    minutes_ago: float = 60.0,
    agent: str = "ema_cross",
    direction: Direction = Direction.BUY,
    session: SessionName = SessionName.LONDON,
    regime: str = "normal",
    price_vs_va: str = "inside",
    symbol: str = SYMBOL,
) -> Detection:
    moment = NOW - timedelta(minutes=minutes_ago)
    return Detection(
        detection_id=ident,
        account_scope="primary",
        symbol=symbol,
        timeframe=Timeframe.M5,
        agent_name=agent,
        agent_version="2.1.0",
        event_key="bullish",
        direction=direction,
        detected_at=MarketTime.from_utc(moment, TZ),
        candle_open_time=MarketTime.from_utc(moment - timedelta(minutes=5), TZ),
        price=2400.00,
        indicators=IndicatorSnapshot(ema={"fast": 2401.0, "slow": 2395.0}, rsi=61.4),
        session=SessionContext(session=session, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
        volume_profile_ref=VolumeProfileRef(scope="asia", price_vs_va=price_vs_va),
        volatility=VolatilityContext(regime=regime, bands_version=1),
    )


def evaluation(
    ident: str,
    *,
    reached: dict[float, bool] | None = None,
    mfe: float = 400.0,
    mae: float = 150.0,
    path: PathClassification = PathClassification.MFE_FIRST,
    ambiguous: bool = False,
    aligned: bool = True,
    wick: bool = False,
    seconds_to: float | None = 600.0,
    complete: bool = True,
) -> DetectionEvaluation:
    hits = reached or {t: t <= 5.0 for t in XAU_OUTCOME_V2.thresholds}
    reached_map = {threshold_key(t): bool(hits.get(t, False)) for t in XAU_OUTCOME_V2.thresholds}
    time_map = {k: (seconds_to if v else None) for k, v in reached_map.items()}
    tags = {TAG_SESSION_TREND_ALIGNED: aligned}
    if wick:
        tags[TAG_LOWER_REJECTION_WICK] = True

    if complete:
        result = HorizonResult(
            horizon_id=HORIZON,
            status=HorizonStatus.COMPLETE,
            future_high=2410.0,
            future_low=2395.0,
            mfe=mfe,
            mae=mae,
            reached=reached_map,
            time_to=time_map,
            path=path,
            path_ambiguous=ambiguous,
            candles_seen=20,
            completed_at=NOW,
        )
    else:
        result = HorizonResult(horizon_id=HORIZON, status=HorizonStatus.PENDING)

    return DetectionEvaluation(
        detection_id=ident,
        rule_id=XAU_OUTCOME_V2.rule_id,
        reference_price=XAU_OUTCOME_V2.reference_price,
        reference_value=2400.0,
        horizons=(result,),
        context_tags=tags,
    )


def member(ident: str, **overrides) -> CohortMember:
    detection_kwargs = {
        k: overrides.pop(k)
        for k in ("agent", "direction", "session", "regime", "price_vs_va", "symbol",
                  "minutes_ago")
        if k in overrides
    }
    return CohortMember(
        detection=detection(ident, **detection_kwargs),
        evaluation=evaluation(ident, **overrides),
    )


def population(n: int, *, prefix: str = "p", **overrides) -> list[CohortMember]:
    return [member(f"{prefix}{i}", **overrides) for i in range(n)]


def test_an_exact_cohort_is_used_when_there_is_enough_of_it() -> None:
    subject = detection("subject", minutes_ago=0)
    cohort, wanted = select_cohort(subject, evaluation("subject"), population(MIN_COHORT))

    assert len(cohort) == MIN_COHORT
    assert wanted.dropped == (), "nothing needed to be given up"
    assert wanted.volatility_regime == "normal"
    assert wanted.price_vs_va == "inside"


def test_the_narrowest_dimension_is_surrendered_first_and_reported() -> None:
    """The order is the claim. A cohort that silently stopped matching on the regime would
    be answering a different question while wearing the same words."""
    subject = detection("subject", minutes_ago=0)
    # Enough detections, but none of them carries the subject's wick tag.
    cohort, wanted = select_cohort(
        subject, evaluation("subject", wick=True), population(MIN_COHORT, wick=False)
    )

    assert len(cohort) == MIN_COHORT
    assert wanted.dropped == ("wick_tag",)
    assert wanted.wick_tag is None
    assert wanted.volatility_regime == "normal", "the regime was not given up too"


def test_dimensions_are_given_up_one_at_a_time_in_order() -> None:
    subject = detection("subject", minutes_ago=0, price_vs_va="above", regime="high")
    # The population differs on the value area AND the regime, so both must go.
    cohort, wanted = select_cohort(
        subject,
        evaluation("subject"),
        population(MIN_COHORT, price_vs_va="inside", regime="normal"),
    )

    assert len(cohort) == MIN_COHORT
    assert wanted.dropped == WIDENING_ORDER
    assert wanted.price_vs_va is None and wanted.volatility_regime is None


def test_the_symbol_agent_direction_and_session_are_never_given_up() -> None:
    """Fully widened is still this instrument, this signal, this direction, this hour."""
    subject = detection("subject", minutes_ago=0)
    others = (
        population(20, prefix="silver", symbol="XAGUSD")
        + population(20, prefix="wick", agent="wick")
        + population(20, prefix="sell", direction=Direction.SELL)
        + population(20, prefix="asia", session=SessionName.ASIA)
    )

    cohort, wanted = select_cohort(subject, evaluation("subject"), others)
    assert cohort == []
    assert wanted.symbol == SYMBOL
    assert wanted.agent_name == "ema_cross"
    assert wanted.direction is Direction.BUY
    assert wanted.session is SessionName.LONDON


# ── The refusal ───────────────────────────────────────────────────────────────


def test_too_little_history_publishes_no_percentage_at_all() -> None:
    """Not a smaller number with a wider interval: nothing. An interval on n=12 is so wide
    that printing the estimate beside it invites reading the estimate and ignoring it."""
    subject = detection("subject", minutes_ago=0)
    readout = build_assessment(
        subject,
        trend=read_trend(chop(60), point=POINT),
        rule=XAU_OUTCOME_V2,
        population=population(MIN_COHORT - 1),
        estimate_horizon=HORIZON,
        point=POINT,
    )

    assert readout.insufficient is True
    assert readout.n == MIN_COHORT - 1
    assert readout.confirmations == ()
    assert readout.tp_estimates == () and readout.sl_estimates == ()
    assert readout.paired is None


def test_exactly_the_floor_is_enough() -> None:
    subject = detection("subject", minutes_ago=0)
    readout = build_assessment(
        subject,
        trend=read_trend(chop(60), point=POINT),
        rule=XAU_OUTCOME_V2,
        population=population(MIN_COHORT),
        estimate_horizon=HORIZON,
        point=POINT,
    )
    assert readout.insufficient is False
    assert readout.n == MIN_COHORT
    assert readout.confirmations


# ── Confirmation ──────────────────────────────────────────────────────────────


def test_every_percentage_carries_its_n_and_its_interval() -> None:
    cohort = population(40, reached={3.0: True, 5.0: True})
    table = {h.horizon_id: h for h in confirmations(cohort, XAU_OUTCOME_V2)}
    row = {t.threshold: t for t in table[HORIZON].thresholds}

    assert row[5.0].reached == 40 and row[5.0].evaluated == 40
    assert row[5.0].rate == 1.0
    assert row[5.0].ci_low is not None and row[5.0].ci_high == 1.0
    assert row[20.0].reached == 0 and row[20.0].rate == 0.0
    assert row[20.0].ci_high > 0.0, "a zero rate still has an interval"
    assert row[5.0].median_seconds_to_first == 600.0


def test_a_pending_horizon_is_excluded_rather_than_counted_as_a_miss() -> None:
    """§22's rule, applied to the cohort: treating unknown as failure is the easiest way to
    make a strategy look worse than it is."""
    cohort = population(20) + population(20, prefix="q", complete=False)
    table = {h.horizon_id: h for h in confirmations(cohort, XAU_OUTCOME_V2)}
    assert table[HORIZON].evaluated == 20


def test_an_ambiguous_path_is_not_counted_as_adversity_first() -> None:
    """Both thresholds were first crossed inside one candle, so the order was never
    observed (§23). Asserting one would be inventing it."""
    cohort = (
        population(20, path=PathClassification.MAE_FIRST)
        + population(10, prefix="amb", path=PathClassification.MAE_FIRST, ambiguous=True)
    )
    table = {h.horizon_id: h for h in confirmations(cohort, XAU_OUTCOME_V2)}
    assert table[HORIZON].mae_first == 20
    assert table[HORIZON].path_ambiguous == 10


# ── Estimates ─────────────────────────────────────────────────────────────────


def test_the_estimates_are_quantiles_of_what_was_measured() -> None:
    cohort = [member(f"m{i}", mfe=100.0 * (i + 1), mae=50.0 * (i + 1)) for i in range(40)]
    subject = detection("subject", minutes_ago=0)
    readout = build_assessment(
        subject,
        trend=read_trend(chop(60), point=POINT),
        rule=XAU_OUTCOME_V2,
        population=cohort,
        estimate_horizon=HORIZON,
        point=POINT,
    )

    tp = {e.quantile: e for e in readout.tp_estimates}
    sl = {e.quantile: e for e in readout.sl_estimates}
    # p50 of 100..4000 step 100 is 2050; p25 is 1075.
    assert tp[0.5].points == pytest.approx(2050.0)
    assert tp[0.25].points == pytest.approx(1075.0)
    # p75 of 50..2000 step 50 is 1512.5; p90 is 1805.
    assert sl[0.75].points == pytest.approx(1512.5)
    assert sl[0.9].points == pytest.approx(1805.0)


def test_a_stop_is_below_the_entry_on_a_buy_and_above_it_on_a_sell() -> None:
    """The sign error that reads as a perfectly sensible price and is the wrong side of
    the market."""
    for direction in (Direction.BUY, Direction.SELL):
        subject = detection("subject", minutes_ago=0, direction=direction)
        readout = build_assessment(
            subject,
            trend=read_trend(chop(60), point=POINT),
            rule=XAU_OUTCOME_V2,
            population=population(40, prefix=f"c{direction.value}"),
            subject_evaluation=evaluation("subject"),
            estimate_horizon=HORIZON,
            point=POINT,
        )
        # The population is all BUY, so a SELL subject widens to nothing; build it directly.
        if readout.insufficient:
            readout = build_assessment(
                subject,
                trend=read_trend(chop(60), point=POINT),
                rule=XAU_OUTCOME_V2,
                population=population(40, prefix="s", direction=direction),
                estimate_horizon=HORIZON,
                point=POINT,
            )

        tp = readout.tp_estimates[0].price
        sl = readout.sl_estimates[0].price
        if direction is Direction.BUY:
            assert tp > subject.price and sl < subject.price
        else:
            assert tp < subject.price and sl > subject.price


def test_an_estimate_in_points_becomes_a_price_at_this_symbols_tick() -> None:
    """400 points is $4.00 of gold and $0.40 of silver. A hard-coded multiplier here would
    publish a silver stop ten times too far away."""
    built = estimates(
        [400.0] * 5,
        (0.5,),
        reference=2400.0,
        point=0.01,
        favourable=True,
        direction=Direction.BUY,
    )
    assert built[0].price == pytest.approx(2404.0)

    silver = estimates(
        [400.0] * 5,
        (0.5,),
        reference=30.0,
        point=0.001,
        favourable=True,
        direction=Direction.BUY,
    )
    assert silver[0].price == pytest.approx(30.4)


# ── The paired stat ───────────────────────────────────────────────────────────


def test_reached_before_adversity_excludes_the_unobservable() -> None:
    cohort = (
        population(20, reached={3.0: True}, path=PathClassification.MFE_FIRST)
        + population(10, prefix="bad", reached={3.0: True}, path=PathClassification.MAE_FIRST)
        + population(
            5, prefix="amb", reached={3.0: True},
            path=PathClassification.MFE_FIRST, ambiguous=True,
        )
    )
    paired = paired_outcome(cohort, HORIZON, favourable=3.0, adverse=3.0)
    assert paired.evaluated == 30, "the five ambiguous paths are out of the denominator"
    assert paired.favourable_first == 20
    assert paired.rate == pytest.approx(20 / 30)
    assert paired.ci_low is not None


# ── Look-ahead ────────────────────────────────────────────────────────────────


def test_the_cohort_cannot_contain_detections_made_after_the_subject() -> None:
    """The look-ahead that is trivially easy to introduce here, because the rows are simply
    sitting in a collection and nothing about reading them says when they happened."""
    subject = detection("subject", minutes_ago=0)
    earlier = [detection(f"old{i}", minutes_ago=600 + i) for i in range(5)]
    later = [detection(f"new{i}", minutes_ago=-60 - i) for i in range(5)]
    evaluations = {d.detection_id: evaluation(d.detection_id) for d in earlier + later}

    members = population_from(
        earlier + later, evaluations, before=subject.detected_at.utc
    )
    assert {m.detection.detection_id for m in members} == {f"old{i}" for i in range(5)}


def test_a_detection_with_nothing_complete_contributes_nothing() -> None:
    earlier = [detection(f"old{i}", minutes_ago=600 + i) for i in range(5)]
    evaluations = {
        d.detection_id: evaluation(d.detection_id, complete=False) for d in earlier
    }
    assert population_from(earlier, evaluations, before=NOW) == []


def test_a_detection_with_no_evaluation_contributes_nothing() -> None:
    earlier = [detection("old0", minutes_ago=600)]
    assert population_from(earlier, {}, before=NOW) == []


# ── Disagreement ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("bias_candles", "direction", "expected"),
    [
        (ramp(60, start=2380.0, step=0.4), Direction.SELL, True),
        (ramp(60, start=2420.0, step=-0.4), Direction.BUY, True),
        (ramp(60, start=2380.0, step=0.4), Direction.BUY, False),
        (chop(60), Direction.BUY, False),
    ],
)
def test_a_trend_read_against_the_detection_is_recorded(
    bias_candles, direction, expected
) -> None:
    """Stored rather than derived at render time, so a review can count how often the two
    parted company — and said out loud in the embed, never quietly averaged away."""
    subject = detection("subject", minutes_ago=0, direction=direction)
    readout = build_assessment(
        subject,
        trend=read_trend(
            bias_candles,
            ema_fast=2401.0 if bias_candles[-1].close > bias_candles[0].close else 2395.0,
            ema_slow=2395.0 if bias_candles[-1].close > bias_candles[0].close else 2401.0,
            ema_fast_earlier=bias_candles[0].close,
            point=POINT,
        ),
        rule=XAU_OUTCOME_V2,
        population=population(MIN_COHORT, prefix=f"d{direction.value}", direction=direction),
        estimate_horizon=HORIZON,
        point=POINT,
    )
    assert readout.disagrees_with_detection is expected


def test_the_rule_id_rides_with_the_readout() -> None:
    """§21: an assessment from before a rule change and one from after would otherwise be
    indistinguishable while meaning different things."""
    subject = detection("subject", minutes_ago=0)
    readout = build_assessment(
        subject,
        trend=read_trend(chop(60), point=POINT),
        rule=XAU_OUTCOME_V2,
        population=population(MIN_COHORT),
        estimate_horizon=HORIZON,
        point=POINT,
    )
    assert readout.rule_id == "XAU_OUTCOME_V2"
    assert readout.symbol == SYMBOL and readout.detection_id == "subject"
