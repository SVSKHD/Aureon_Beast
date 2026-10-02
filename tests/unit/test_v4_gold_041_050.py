"""Acceptance coverage for Aureon V4 Gold TODO 041-050."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aureon.models.base import MarketTime
from aureon.models.ema_journey_v4 import (
    V4SessionLearningRow,
    V4SessionPhase,
    V4VolumeTrajectory,
)
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.market import Candle
from aureon.services.v4_session_intelligence import (
    V4SessionTracker,
    active_sessions,
    fit_session_specialists,
    independent_session_context,
    session_phase,
    session_phase_report,
)

TZ = "Europe/Athens"


def _market_dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 2, hour, minute, tzinfo=ZoneInfo(TZ))


def _candle(hour: int, minute: int, open_: float, high: float, low: float, close: float) -> Candle:
    local = _market_dt(hour, minute)
    return Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(local.astimezone(ZoneInfo("UTC")), TZ),
        open=open_,
        high=high,
        low=low,
        close=close,
        tick_volume=100,
    )


def test_041_042_pullback_contracts_and_continuation_volume_are_separate() -> None:
    volume = V4VolumeTrajectory()
    volume.observe_expansion(200)
    volume.observe_expansion(180)
    volume.observe_pullback(100)
    volume.observe_pullback(90)
    assert volume.expansion_volume_mean == 190.0
    assert volume.pullback_volume_mean == 95.0
    assert volume.pullback_volume_contracting is True

    volume.observe_continuation(160)
    volume.observe_continuation(180)
    assert volume.continuation_volume_mean == 170.0
    assert volume.continuation_volume_expanding is True


def test_043_046_sessions_are_independent_and_overlap_is_explicit() -> None:
    assert active_sessions(_market_dt(3)) == (SessionName.ASIA,)
    assert active_sessions(_market_dt(11)) == (SessionName.LONDON,)
    assert active_sessions(_market_dt(16)) == (
        SessionName.LONDON,
        SessionName.NEW_YORK,
    )
    assert active_sessions(_market_dt(20)) == (SessionName.NEW_YORK,)

    context = independent_session_context(_market_dt(16))
    assert context.overlap is True
    assert context.overlap_name == "london_new_york"
    assert context.active_sessions == ("london", "new_york")


def test_047_048_session_open_context_and_close_outcomes_are_kept_independently() -> None:
    tracker = V4SessionTracker()

    opened, closed = tracker.on_closed_candle(
        _candle(2, 0, 4100, 4102, 4099, 4101),
        ema20=4100.5,
        ema50=4099.5,
        ema200=4080.0,
        trend="UP",
    )
    assert len(opened) == 1
    assert opened[0].session == "asia"
    assert opened[0].open_price == 4100
    assert closed == []

    tracker.on_closed_candle(_candle(9, 55, 4101, 4110, 4098, 4108))

    opened, closed = tracker.on_closed_candle(
        _candle(10, 0, 4108, 4111, 4107, 4110),
        ema20=4107.0,
        ema50=4105.0,
        ema200=4085.0,
        trend="UP",
    )
    assert len(closed) == 1
    asia = closed[0]
    assert asia.session == "asia"
    assert asia.open_price == 4100
    assert asia.close_price == 4108
    assert asia.high == 4110
    assert asia.low == 4098
    assert asia.change == 8
    assert asia.range == 12
    assert asia.candle_count == 2

    assert len(opened) == 1
    london = opened[0]
    assert london.session == "london"
    assert london.previous_session == "asia"
    assert london.previous_session_change == 8
    assert london.previous_session_range == 12

    # New York opens while London remains active.
    opened, closed = tracker.on_closed_candle(
        _candle(15, 0, 4120, 4122, 4119, 4121),
        ema20=4118.0,
        ema50=4113.0,
        ema200=4095.0,
        trend="UP",
    )
    assert [row.session for row in opened] == ["new_york"]
    assert closed == []

    # At 18:00 London closes while New York remains independently active.
    tracker.on_closed_candle(_candle(17, 55, 4121, 4128, 4120, 4126))
    opened, closed = tracker.on_closed_candle(_candle(18, 0, 4126, 4127, 4124, 4125))
    assert [row.session for row in closed] == ["london"]
    assert opened == []
    assert independent_session_context(_market_dt(18)).active_sessions == ("new_york",)


def test_050_session_phase_is_derived_per_independent_window() -> None:
    assert session_phase(SessionName.ASIA, _market_dt(2, 30)) is V4SessionPhase.EARLY
    assert session_phase(SessionName.ASIA, _market_dt(6, 0)) is V4SessionPhase.MID
    assert session_phase(SessionName.ASIA, _market_dt(9, 30)) is V4SessionPhase.LATE

    # At the same wall-clock moment the overlapping sessions can be at
    # different phases because each has its own start/end.
    context = independent_session_context(_market_dt(16, 0))
    assert context.phases["london"] is V4SessionPhase.LATE
    assert context.phases["new_york"] is V4SessionPhase.EARLY


def test_049_050_session_specialists_require_samples_and_compare_phases() -> None:
    london_early = [
        V4SessionLearningRow(
            session="london",
            phase=V4SessionPhase.EARLY,
            direction=Direction.BUY,
            anchor_type="ema20_50_cross",
            reached_3=True,
            reached_5=index < 3,
            reached_10=index == 0,
            mfe=8.0 + index,
            mae=1.0,
            pullback_depth=2.0,
        )
        for index in range(5)
    ]
    overlap = [
        V4SessionLearningRow(
            session="new_york",
            phase=V4SessionPhase.EARLY,
            overlap=True,
            direction=Direction.BUY,
            anchor_type="ema200_cross",
            reached_3=True,
            reached_5=True,
            reached_10=False,
            mfe=7.0,
            mae=1.5,
            pullback_depth=1.5,
        )
        for _ in range(5)
    ]
    rows = london_early + overlap

    gated = fit_session_specialists(rows, min_samples=6)
    assert gated["london:early"]["sufficient"] is False
    assert gated["london_new_york_overlap"]["sufficient"] is False

    report = session_phase_report(rows, min_samples=5)
    assert report["london:early"]["sufficient"] is True
    assert report["london:early"]["reach_3_rate"] == 1.0
    assert report["london_new_york_overlap"]["samples"] == 5
    assert report["london_new_york_overlap"]["reach_5_rate"] == 1.0
