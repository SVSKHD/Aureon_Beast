"""Acceptance coverage for Aureon V4 Gold TODO 021-040."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aureon.models.base import MarketTime
from aureon.models.detection import (
    AgentEvidence,
    Detection,
    IndicatorSnapshot,
    SessionContext,
)
from aureon.models.ema_journey_v4 import (
    V4PullbackClass,
    V4PullbackStatus,
    V4RetestKind,
)
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.market import Candle
from aureon.services.ema_journey_tracker import EMAMovementJourneyTracker
from aureon.services.v4_pullback_tracker import (
    V4PullbackTracker,
    compare_cross_vs_reentry,
)

TZ = "Europe/Athens"
BASE = datetime(2026, 10, 2, 7, 0, tzinfo=UTC)


def _detection(
    *,
    agent: str,
    direction: Direction,
    price: float,
    minutes: int,
    suffix: str,
    tick_volume: float,
    volume_3: float,
    volume_5: float,
    volume_ratio: float,
) -> Detection:
    close_at = BASE + timedelta(minutes=minutes)
    return Detection(
        detection_id=f"v4pb-{suffix}",
        account_scope="test",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        agent_name=agent,
        agent_version="test",
        event_key="bullish" if direction is Direction.BUY else "bearish",
        direction=direction,
        detected_at=MarketTime.from_utc(close_at, TZ),
        candle_open_time=MarketTime.from_utc(close_at - timedelta(minutes=5), TZ),
        price=price,
        session=SessionContext(
            session=SessionName.LONDON,
            session_config_version=1,
        ),
        sequence_today=1,
        sequence_session=1,
        indicators=IndicatorSnapshot(
            ema={"fast": 4101.0, "slow": 4099.0, "ema200": 4085.0}
        ),
        evidence=AgentEvidence(
            numeric={
                "tick_volume": tick_volume,
                "tick_volume_3bar_mean": volume_3,
                "tick_volume_5bar_mean": volume_5,
                "tick_volume_ratio_to_median": volume_ratio,
            },
            categorical={
                "trend_direction": "UP",
                "ema_relation": "fast_above",
                "ema200_context": "price_above_ema200",
                "volume_state": "expanding",
            },
            flags={},
        ),
    )


def _candle(
    *,
    minutes: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: int,
) -> Candle:
    return Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(BASE + timedelta(minutes=minutes), TZ),
        open=open_,
        high=high,
        low=low,
        close=close,
        tick_volume=volume,
    )


def _journey():
    journey_tracker = EMAMovementJourneyTracker(max_horizon_bars=96)
    journey_tracker.on_detection(
        _detection(
            agent="ema200_pre_cross",
            direction=Direction.BUY,
            price=4097.0,
            minutes=0,
            suffix="pre",
            tick_volume=95.0,
            volume_3=90.0,
            volume_5=88.0,
            volume_ratio=0.95,
        )
    )
    journey = journey_tracker.on_detection(
        _detection(
            agent="ema_cross",
            direction=Direction.BUY,
            price=4100.0,
            minutes=10,
            suffix="cross",
            tick_volume=140.0,
            volume_3=118.0,
            volume_5=110.0,
            volume_ratio=1.40,
        )
    )
    assert journey is not None
    return journey


def test_detects_and_measures_normal_pullback_with_retests() -> None:
    journey = _journey()
    tracker = V4PullbackTracker()

    # Expansion to +10.  It cannot be a pullback on the same candle that made
    # the new favourable extreme.
    assert tracker.on_closed_candle(
        journey,
        _candle(
            minutes=15,
            open_=4100.0,
            high=4110.0,
            low=4099.5,
            close=4109.0,
            volume=160,
        ),
        ema20=4106.0,
        ema50=4102.0,
        ema200=4088.0,
        atr=4.0,
        structure_level=4106.0,
    ) is None

    pullback = tracker.on_closed_candle(
        journey,
        _candle(
            minutes=20,
            open_=4109.0,
            high=4109.0,
            low=4106.0,
            close=4107.0,
            volume=105,
        ),
        ema20=4107.5,
        ema50=4105.0,
        ema200=4090.0,
        atr=4.0,
        structure_level=4106.0,
    )
    assert pullback is not None
    assert pullback.depth == 4.0
    assert pullback.depth_atr == 1.0
    assert pullback.retracement_fraction == 0.4
    assert pullback.classification is V4PullbackClass.NORMAL
    assert pullback.bars == 1
    assert V4RetestKind.EMA20 in pullback.retests
    assert V4RetestKind.STRUCTURE in pullback.retests
    assert pullback.structure_intact is True
    assert pullback.ema_aligned is True


def test_reentry_requires_structure_and_ema_alignment_then_tracks_outcome() -> None:
    journey = _journey()
    tracker = V4PullbackTracker()

    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=15,
            open_=4100.0,
            high=4110.0,
            low=4099.5,
            close=4109.0,
            volume=160,
        ),
        ema20=4106.0,
        ema50=4102.0,
        ema200=4088.0,
        atr=4.0,
        structure_level=4106.0,
    )
    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=20,
            open_=4109.0,
            high=4109.0,
            low=4106.0,
            close=4107.0,
            volume=105,
        ),
        ema20=4107.5,
        ema50=4105.0,
        ema200=4090.0,
        atr=4.0,
        structure_level=4106.0,
    )

    # Price resumes upward, but a deliberately broken EMA ordering blocks re-entry.
    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=25,
            open_=4107.0,
            high=4109.0,
            low=4107.0,
            close=4108.5,
            volume=110,
        ),
        ema20=4104.0,
        ema50=4105.0,
        ema200=4090.0,
        atr=4.0,
        structure_level=4106.0,
    )
    assert tracker.reentries_for(journey.journey_id) == []

    # Alignment returns and price resumes again: one research-only re-entry anchor.
    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=30,
            open_=4108.5,
            high=4110.0,
            low=4108.0,
            close=4109.5,
            volume=125,
        ),
        ema20=4108.0,
        ema50=4106.0,
        ema200=4091.0,
        atr=4.0,
        structure_level=4106.0,
    )
    reentries = tracker.reentries_for(journey.journey_id)
    assert len(reentries) == 1
    observation = reentries[0]
    assert observation.structure_intact is True
    assert observation.ema_aligned is True
    assert observation.price == 4109.5

    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=35,
            open_=4109.5,
            high=4116.0,
            low=4109.0,
            close=4115.0,
            volume=150,
        ),
        ema20=4110.0,
        ema50=4107.0,
        ema200=4092.0,
        atr=4.0,
        structure_level=4106.0,
    )
    assert observation.outcome.reached_3 is True
    assert observation.outcome.reached_5 is True
    assert observation.outcome.mfe == 6.5
    assert observation.outcome.mae == 0.5

    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=40,
            open_=4115.0,
            high=4120.0,
            low=4114.0,
            close=4119.0,
            volume=170,
        ),
        ema20=4112.0,
        ema50=4108.0,
        ema200=4093.0,
        atr=4.0,
        structure_level=4106.0,
    )
    assert observation.outcome.reached_10 is True
    assert observation.outcome.completed is True
    pullback = tracker.active_pullback(journey.journey_id)
    assert pullback is not None
    assert pullback.status is V4PullbackStatus.CONTINUED


def test_deep_pullback_breaks_structure_and_cannot_reenter() -> None:
    journey = _journey()
    tracker = V4PullbackTracker()

    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=15,
            open_=4100.0,
            high=4110.0,
            low=4099.5,
            close=4109.0,
            volume=160,
        ),
        ema20=4106.0,
        ema50=4102.0,
        ema200=4088.0,
        atr=4.0,
    )
    pullback = tracker.on_closed_candle(
        journey,
        _candle(
            minutes=20,
            open_=4109.0,
            high=4109.0,
            low=4102.0,
            close=4102.5,
            volume=115,
        ),
        ema20=4105.0,
        ema50=4103.0,
        ema200=4090.0,
        atr=4.0,
    )
    assert pullback is not None
    assert pullback.classification is V4PullbackClass.DEEP

    # Second deepening takes the retracement beyond the structural safety fraction.
    pullback = tracker.on_closed_candle(
        journey,
        _candle(
            minutes=25,
            open_=4102.5,
            high=4104.0,
            low=4101.0,
            close=4101.5,
            volume=120,
        ),
        ema20=4104.0,
        ema50=4103.0,
        ema200=4090.0,
        atr=4.0,
    )
    assert pullback is not None
    assert pullback.structure_intact is False
    assert pullback.status is V4PullbackStatus.FAILED
    assert tracker.reentries_for(journey.journey_id) == []


def test_phase_volume_keeps_pre_cross_cross_and_expansion_separate() -> None:
    journey = _journey()
    tracker = V4PullbackTracker()

    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=15,
            open_=4100.0,
            high=4106.0,
            low=4099.5,
            close=4105.0,
            volume=160,
        ),
        ema20=4103.0,
        ema50=4101.0,
        ema200=4088.0,
        atr=4.0,
    )
    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=20,
            open_=4105.0,
            high=4109.0,
            low=4104.5,
            close=4108.0,
            volume=180,
        ),
        ema20=4105.0,
        ema50=4102.0,
        ema200=4089.0,
        atr=4.0,
    )
    # Pullback start: its low-volume candle must not be counted as expansion volume.
    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=25,
            open_=4108.0,
            high=4108.0,
            low=4105.0,
            close=4106.0,
            volume=80,
        ),
        ema20=4106.0,
        ema50=4103.0,
        ema200=4090.0,
        atr=4.0,
    )

    volume = tracker.volume_for(journey.journey_id)
    assert volume is not None
    assert volume.pre_cross_3bar_mean == 90.0
    assert volume.pre_cross_5bar_mean == 88.0
    assert volume.cross_tick_volume == 140.0
    assert volume.cross_volume_ratio == 1.40
    assert volume.expansion_volume_bars == 2
    assert volume.expansion_volume_mean == 170.0


def test_failed_reentry_is_saved_and_cross_vs_pullback_reported() -> None:
    journey = _journey()
    tracker = V4PullbackTracker(reentry_failure_move=3.0)

    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=15,
            open_=4100.0,
            high=4110.0,
            low=4099.5,
            close=4109.0,
            volume=160,
        ),
        ema20=4106.0,
        ema50=4102.0,
        ema200=4088.0,
        atr=4.0,
        structure_level=4106.0,
    )
    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=20,
            open_=4109.0,
            high=4109.0,
            low=4106.0,
            close=4107.0,
            volume=105,
        ),
        ema20=4107.5,
        ema50=4105.0,
        ema200=4090.0,
        atr=4.0,
        structure_level=4106.0,
    )
    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=25,
            open_=4107.0,
            high=4109.0,
            low=4107.0,
            close=4108.5,
            volume=120,
        ),
        ema20=4108.0,
        ema50=4105.0,
        ema200=4090.0,
        atr=4.0,
        structure_level=4106.0,
    )
    observation = tracker.reentries_for(journey.journey_id)[0]

    tracker.on_closed_candle(
        journey,
        _candle(
            minutes=30,
            open_=4108.5,
            high=4109.0,
            low=4105.0,
            close=4105.5,
            volume=140,
        ),
        ema20=4107.0,
        ema50=4105.0,
        ema200=4090.0,
        atr=4.0,
        structure_level=4105.0,
    )
    assert observation.outcome.failed is True
    assert observation.outcome.failure_reason == "adverse_before_plus_3"
    assert observation.outcome.completed is True

    report = compare_cross_vs_reentry(
        cross_rows=[
            {
                "mfe": 8.0,
                "mae": 2.0,
                "reach_3": True,
                "reach_5": True,
                "reach_10": False,
            }
        ],
        reentries=[observation],
    )
    assert report["cross"]["samples"] == 1
    assert report["pullback_reentry"]["samples"] == 1
