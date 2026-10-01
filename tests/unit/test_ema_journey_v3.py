"""V3 EMA movement-journey tracking."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aureon.models.base import MarketTime
from aureon.models.detection import Detection, SessionContext
from aureon.models.ema_journey_v3 import JourneyEndReason, JourneyStatus
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.market import Candle
from aureon.services.ema_journey_tracker import EMAMovementJourneyTracker

TZ = "Europe/Athens"
BASE = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _detection(
    *,
    agent: str,
    direction: Direction,
    price: float,
    minutes: int,
    suffix: str,
) -> Detection:
    close_at = BASE + timedelta(minutes=minutes)
    return Detection(
        detection_id=f"d-{suffix}",
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
    )


def _candle(
    *,
    minutes: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> Candle:
    return Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(BASE + timedelta(minutes=minutes), TZ),
        open=open_,
        high=high,
        low=low,
        close=close,
    )


def test_links_pre_cross_and_confirmed_crosses_into_one_journey() -> None:
    tracker = EMAMovementJourneyTracker(max_horizon_bars=96)
    pre = tracker.on_detection(
        _detection(
            agent="ema200_pre_cross",
            direction=Direction.SELL,
            price=4197.0,
            minutes=0,
            suffix="pre",
        )
    )
    assert pre is not None

    cross20 = tracker.on_detection(
        _detection(
            agent="ema_cross",
            direction=Direction.SELL,
            price=4190.0,
            minutes=15,
            suffix="20-50",
        )
    )
    cross200 = tracker.on_detection(
        _detection(
            agent="ema200_cross",
            direction=Direction.SELL,
            price=4180.0,
            minutes=30,
            suffix="200",
        )
    )

    assert cross20 is cross200
    assert cross200 is not None
    assert len(cross200.anchors) == 3
    assert cross200.anchors[1].movement_from_journey_start == 7.0
    assert cross200.anchors[2].movement_from_journey_start == 17.0
    assert cross200.movement_consumed_before_latest_cross == 17.0


def test_tracks_mfe_mae_target_ladder_and_time_from_each_anchor() -> None:
    tracker = EMAMovementJourneyTracker(max_horizon_bars=10)
    journey = tracker.on_detection(
        _detection(
            agent="ema_cross",
            direction=Direction.SELL,
            price=4180.0,
            minutes=0,
            suffix="cross",
        )
    )
    assert journey is not None

    tracker.on_closed_candle(
        _candle(
            minutes=0,
            open_=4180.0,
            high=4181.0,
            low=4179.0,
            close=4179.5,
        )
    )
    # Detection candle is ignored. First future bar reaches +5 and experiences $2 MAE.
    tracker.on_closed_candle(
        _candle(
            minutes=5,
            open_=4179.5,
            high=4182.0,
            low=4174.0,
            close=4175.0,
        )
    )
    tracker.on_closed_candle(
        _candle(
            minutes=10,
            open_=4175.0,
            high=4176.0,
            low=4169.0,
            close=4170.0,
        )
    )

    current = tracker.active_for("XAUUSD", "M5")
    assert current is not None
    outcome = current.anchors[0].outcome
    assert outcome.mfe == 11.0
    assert outcome.mae == 2.0
    assert outcome.targets["5"].reached is True
    assert outcome.targets["5"].bars_to == 1
    assert outcome.targets["10"].reached is True
    assert outcome.targets["10"].bars_to == 2
    assert outcome.targets["20"].reached is False
    assert outcome.targets["30"].reached is False
    assert outcome.targets["40"].reached is False


def test_opposite_confirmed_cross_closes_old_journey_and_starts_new_one() -> None:
    tracker = EMAMovementJourneyTracker()
    old = tracker.on_detection(
        _detection(
            agent="ema_cross",
            direction=Direction.SELL,
            price=4180.0,
            minutes=0,
            suffix="sell",
        )
    )
    assert old is not None

    new = tracker.on_detection(
        _detection(
            agent="ema_cross",
            direction=Direction.BUY,
            price=4185.0,
            minutes=20,
            suffix="buy",
        )
    )

    assert old.status is JourneyStatus.CLOSED
    assert old.end_reason is JourneyEndReason.OPPOSITE_CONFIRMED_CROSS
    assert new is not None
    assert new.direction is Direction.BUY
    assert new.journey_id != old.journey_id


def test_market_day_change_is_a_deterministic_end_reason() -> None:
    tracker = EMAMovementJourneyTracker()
    journey = tracker.on_detection(
        _detection(
            agent="ema200_cross",
            direction=Direction.SELL,
            price=4180.0,
            minutes=0,
            suffix="day",
        )
    )
    assert journey is not None

    next_day = Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(BASE + timedelta(days=1), TZ),
        open=4170.0,
        high=4171.0,
        low=4169.0,
        close=4170.0,
    )
    tracker.on_closed_candle(next_day)

    assert journey.status is JourneyStatus.CLOSED
    assert journey.end_reason is JourneyEndReason.MARKET_DAY_CHANGE
    assert journey.anchors[0].outcome.completed is True
