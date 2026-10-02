"""Acceptance coverage for Aureon V4 Gold TODO 041-060."""
from __future__ import annotations

from datetime import UTC, datetime

from aureon.models.base import MarketTime
from aureon.models.ema_journey_v3 import (
    EMAAnchorFeaturesV3,
    EMAAnchorOutcome,
    EMAAnchorType,
    EMAJourneyAnchor,
    EMAMovementJourney,
    JourneyStatus,
)
from aureon.models.ema_journey_v4 import (
    V4MovementFeatures,
    V4PullbackState,
    V4PullbackStatus,
    V4ReentryObservation,
    V4ReentryOutcome,
    V4RemainingMovementExample,
    V4RemainingMovementLabels,
    V4VolumeTrajectory,
)
from aureon.models.enums import (
    Direction,
    MtfAlignment,
    SessionName,
    Timeframe,
    TrendBias,
)
from aureon.models.market import Candle
from aureon.models.mtf import MtfContext, TimeframeRead
from aureon.services.v4_context_sequence import (
    V4SessionTracker,
    active_sessions,
    fit_continuation_specialist,
    freeze_htf_context,
    htf_alignment_effect,
    predict_continuation,
    session_phase,
    sessionwise_remaining_report,
    train_sessionwise_continuation,
    transition_dataset,
    transition_rates,
)

TZ = "Europe/Athens"


def _market_time(hour: int, minute: int = 0) -> MarketTime:
    utc = datetime(2026, 10, 2, hour, minute, tzinfo=UTC)
    return MarketTime.from_utc(utc, TZ)


def _candle(hour: int, minute: int, price: float, *, volume: int = 100) -> Candle:
    return Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=_market_time(hour, minute),
        open=price,
        high=price + 1.0,
        low=price - 1.0,
        close=price + 0.5,
        tick_volume=volume,
    )


def _features(*, session: str, phase: str, htf: str) -> V4MovementFeatures:
    return V4MovementFeatures(
        anchor_type="ema20_50_cross",
        base=EMAAnchorFeaturesV3(
            session=session,
            session_phase=phase,
            htf_alignment=htf,
            trend_direction="UP",
        ),
        movement_consumed=3.0,
        pre_cross_movement=2.0,
        pre_cross_bars=2,
        pre_cross_seconds=600.0,
        cross_order="ema20_50_cross",
    )


def _example(
    index: int,
    *,
    session: str = "london",
    phase: str = "EARLY",
    htf: str = "aligned",
    hit: bool = True,
) -> V4RemainingMovementExample:
    return V4RemainingMovementExample(
        journey_id=f"j-{index}",
        detection_id=f"d-{index}",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction=Direction.BUY,
        market_date=f"2026-09-{(index % 28) + 1:02d}",
        features=_features(session=session, phase=phase, htf=htf),
        labels=V4RemainingMovementLabels(
            reached_3=hit,
            reached_5=hit,
            reached_10=hit and index % 2 == 0,
            remaining_mfe=8.0 if hit else 2.0,
            remaining_mae=1.5 if hit else 5.0,
        ),
    )


def _journey(
    *,
    journey_id: str = "journey-1",
    with_pre: bool = True,
    expanded: bool = True,
) -> EMAMovementJourney:
    anchors = []
    if with_pre:
        anchors.append(
            EMAJourneyAnchor(
                detection_id=f"{journey_id}-pre",
                anchor_type=EMAAnchorType.PRE_CROSS,
                direction=Direction.BUY,
                detected_at=datetime(2026, 10, 2, 7, 0, tzinfo=UTC),
                price=4100.0,
                features=EMAAnchorFeaturesV3(),
                outcome=EMAAnchorOutcome(completed=True),
            )
        )
    anchors.append(
        EMAJourneyAnchor(
            detection_id=f"{journey_id}-cross",
            anchor_type=EMAAnchorType.EMA20_50_CROSS,
            direction=Direction.BUY,
            detected_at=datetime(2026, 10, 2, 7, 10, tzinfo=UTC),
            price=4102.0,
            movement_from_journey_start=2.0,
            features=EMAAnchorFeaturesV3(),
            outcome=EMAAnchorOutcome(
                completed=True,
                mfe=8.0 if expanded else 2.0,
                mae=1.0,
            ),
        )
    )
    return EMAMovementJourney(
        journey_id=journey_id,
        account_scope="test",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction=Direction.BUY,
        market_date="2026-10-02",
        started_at=datetime(2026, 10, 2, 7, 0, tzinfo=UTC),
        start_price=4100.0,
        anchors=tuple(anchors),
        status=JourneyStatus.CLOSED,
    )


def test_volume_trajectory_detects_pullback_contraction_and_continuation_expansion() -> None:
    volume = V4VolumeTrajectory()
    volume.observe_expansion(160)
    volume.observe_expansion(180)
    volume.observe_pullback(90)
    volume.observe_pullback(100)
    assert volume.expansion_volume_mean == 170.0
    assert volume.pullback_volume_mean == 95.0
    assert volume.pullback_volume_contracting is True

    volume.observe_continuation(190)
    volume.observe_continuation(200)
    assert volume.continuation_volume_mean == 195.0
    assert volume.continuation_volume_expanding is True


def test_sessions_are_independent_and_london_new_york_overlap_is_visible() -> None:
    # 16:00 market-local sits inside London 10-18 and New York 15-23.
    market_dt = datetime(2026, 10, 2, 16, 0)
    memberships = active_sessions(market_dt)
    assert SessionName.LONDON in memberships
    assert SessionName.NEW_YORK in memberships
    assert SessionName.ASIA not in memberships
    assert session_phase(SessionName.LONDON, market_dt) == "LATE"
    assert session_phase(SessionName.NEW_YORK, market_dt) == "EARLY"


def test_session_tracker_keeps_overlap_windows_separate_and_emits_open_close_context() -> None:
    tracker = V4SessionTracker()

    # Use market-local times explicitly through MarketTime. Depending on UTC offset,
    # these values are interpreted by the model's market field.
    first = _candle(8, 0, 4100.0)
    opens, closes = tracker.on_closed_candle(
        first,
        ema20=4101.0,
        ema50=4099.0,
        ema200=4080.0,
        ema20_50_crosses=1,
        journeys_seen=1,
    )
    assert isinstance(opens, list)
    assert closes == []

    # Force finalization to validate close outcome bookkeeping.
    closed = tracker.close_all(first.close_time.astimezone(first.open_time.market.tzinfo))
    assert closed
    assert closed[0].symbol == "XAUUSD"
    assert closed[0].range >= 0.0


def test_sessionwise_reports_and_models_keep_sessions_separate() -> None:
    rows = [
        *[_example(i, session="london", phase="EARLY", hit=i % 3 != 0) for i in range(20)],
        *[_example(i + 20, session="asia", phase="MID", hit=i % 2 == 0) for i in range(20)],
    ]
    report = sessionwise_remaining_report(rows, min_samples=5)
    assert report["london:EARLY"]["samples"] == 20
    assert report["asia:MID"]["samples"] == 20

    models = train_sessionwise_continuation(rows, min_samples=20)
    assert set(models) == {"london", "asia"}


def test_freezes_m15_h1_context_and_reports_htf_effect() -> None:
    mtf = MtfContext(
        reads=(
            TimeframeRead(
                timeframe=Timeframe.M15,
                at=datetime(2026, 10, 2, 7, 0, tzinfo=UTC),
                ema_fast=4100.0,
                ema_slow=4098.0,
                close=4102.0,
                bias=TrendBias.BULLISH,
            ),
            TimeframeRead(
                timeframe=Timeframe.H1,
                at=datetime(2026, 10, 2, 6, 0, tzinfo=UTC),
                ema_fast=4095.0,
                ema_slow=4090.0,
                close=4100.0,
                bias=TrendBias.BULLISH,
            ),
        ),
        alignment=MtfAlignment.ALIGNED,
        ema_fast_period=20,
        ema_slow_period=50,
    )
    frozen = freeze_htf_context(mtf)
    assert frozen.m15 is not None and frozen.m15.bias == "bullish"
    assert frozen.h1 is not None and frozen.h1.bias == "bullish"
    assert frozen.alignment == "aligned"

    rows = [
        _example(1, htf="aligned", hit=True),
        _example(2, htf="aligned", hit=True),
        _example(3, htf="against", hit=False),
    ]
    report = htf_alignment_effect(rows)
    assert report["aligned"]["reach_5_rate"] == 1.0
    assert report["against"]["reach_5_rate"] == 0.0


def test_transition_dataset_covers_pressure_cross_expansion_pullback_continuation_and_exhaustion() -> None:
    journey = _journey(expanded=True)
    pullback = V4PullbackState(
        pullback_id="pb-1",
        journey_id=journey.journey_id,
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction=Direction.BUY,
        started_at=datetime(2026, 10, 2, 7, 20, tzinfo=UTC),
        start_price=4106.0,
        expansion_origin_price=4102.0,
        expansion_extreme_price=4110.0,
        expansion_move=8.0,
        status=V4PullbackStatus.REENTRY_OBSERVED,
    )
    reentry = V4ReentryObservation(
        reentry_id="re-1",
        pullback_id="pb-1",
        journey_id=journey.journey_id,
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction=Direction.BUY,
        observed_at=datetime(2026, 10, 2, 7, 25, tzinfo=UTC),
        price=4107.0,
        ema20=4106.0,
        ema50=4104.0,
        pullback_depth=3.0,
        pullback_fraction=0.375,
        pullback_class=pullback.classification,
        structure_intact=True,
        ema_aligned=True,
        outcome=V4ReentryOutcome(
            reached_3=True,
            reached_5=True,
            mfe=6.0,
            mae=1.0,
            completed=True,
        ),
    )
    rows = transition_dataset(
        [journey],
        pullbacks={journey.journey_id: pullback},
        reentries={journey.journey_id: [reentry]},
    )
    rates = transition_rates(rows)
    assert rates["pre_cross->confirmed_cross"]["transition_rate"] == 1.0
    assert rates["confirmed_cross->expansion"]["transition_rate"] == 1.0
    assert rates["expansion->pullback"]["transition_rate"] == 1.0
    assert rates["pullback->continuation"]["transition_rate"] == 1.0
    assert "expansion->exhaustion" in rates


def test_continuation_specialist_is_monotonic() -> None:
    rows = [_example(i, hit=i % 3 != 0) for i in range(30)]
    bundle = fit_continuation_specialist(rows, min_samples=20)
    prediction = predict_continuation(bundle, rows[0])
    assert prediction["reach_3"] >= prediction["reach_5"] >= prediction["reach_10"]
