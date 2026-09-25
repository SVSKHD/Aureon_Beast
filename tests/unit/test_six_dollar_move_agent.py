"""The favourable-move agent freezes one detection and records 6/20/40 milestones."""

from __future__ import annotations

from datetime import UTC, datetime

from aureon.models.base import MarketTime
from aureon.models.detection import Detection, SessionContext
from aureon.models.enums import (
    Direction,
    DirectionContext,
    SessionName,
    SetupAnchorKind,
    SetupEventType,
    SetupFamily,
    SetupState,
    Timeframe,
)
from aureon.models.market import Candle
from aureon.models.setup import Setup, SetupAnchor, SetupEvent
from aureon.services.six_dollar_move_agent import SixDollarMoveAgent

TZ = "Europe/Athens"
NOW = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)


class _Events:
    def __init__(self) -> None:
        self.rows: list[SetupEvent] = []

    def for_setup(self, setup_id: str) -> list[SetupEvent]:
        return [row for row in self.rows if row.setup_id == setup_id]


class _Repository:
    def __init__(self) -> None:
        self.events = _Events()


def _detection(price: float = 2400.0) -> Detection:
    market = MarketTime.from_utc(NOW, TZ)
    return Detection(
        detection_id="d1",
        account_scope="primary",
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        agent_name="ema_cross",
        agent_version="2.3.0",
        event_key="bullish",
        direction=Direction.BUY,
        detected_at=market,
        candle_open_time=market,
        price=price,
        session=SessionContext(
            session=SessionName.LONDON,
            session_config_version=1,
        ),
        sequence_today=1,
        sequence_session=1,
    )


def _setup(direction: DirectionContext = DirectionContext.BULLISH) -> Setup:
    return Setup(
        setup_id="s1",
        account_scope="primary",
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        family=SetupFamily.MOMENTUM_TRANSITION,
        direction_context=direction,
        state=SetupState.CONFIRMED,
        market_date="2026-09-25",
        anchor=SetupAnchor(kind=SetupAnchorKind.EMA_ZONE, price=2400.0),
        opened_at=NOW,
        confirmed_at=NOW,
        linked_detection_ids=("d1",),
    )


def _inputs(*, high: float, low: float, close: float):
    market = MarketTime.from_utc(NOW, TZ)
    candle = Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        open_time=market,
        open=2400.0,
        high=high,
        low=low,
        close=close,
        tick_volume=100,
    )

    class Inputs:
        pass

    result = Inputs()
    result.candle = candle
    return result


def _tracking_event(reference_price: float = 2400.0) -> SetupEvent:
    market = MarketTime.from_utc(NOW, TZ)
    return SetupEvent(
        event_id="track",
        setup_id="s1",
        event_type=SetupEventType.FAVOURABLE_MOVE_6_TRACKING,
        from_state=SetupState.CONFIRMED,
        to_state=SetupState.CONFIRMED,
        linked_detection_id="d1",
        market_time=market,
        context_snapshot={
            "reference_detection_id": "d1",
            "reference_price": f"{reference_price:.5f}",
            "target_move_price": "6.00000",
            "threshold_price": f"{reference_price + 6.0:.5f}",
            "source_timeframe": "M15",
        },
        reason="tracking",
    )


def test_agent_freezes_first_linked_detection_as_reference() -> None:
    repository = _Repository()
    detection = _detection()
    agent = SixDollarMoveAgent(
        setup_repository=repository,
        detection_lookup=lambda detection_id: detection if detection_id == "d1" else None,
    )

    observations = agent.observe(
        _setup(),
        _inputs(high=2402.0, low=2399.0, close=2401.0),
    )

    assert len(observations) == 1
    observation = observations[0]
    assert observation.event_type is SetupEventType.FAVOURABLE_MOVE_6_TRACKING
    assert observation.detail["reference_detection_id"] == "d1"
    assert observation.detail["reference_price"] == "2400.00000"
    assert observation.detail["threshold_6"] == "2406.00000"
    assert observation.detail["threshold_20"] == "2420.00000"
    assert observation.detail["threshold_40"] == "2440.00000"
    assert observation.detail["source_timeframe"] == "M15"


def test_bullish_setup_uses_intrabar_high_to_record_six_dollar_move() -> None:
    repository = _Repository()
    repository.events.rows.append(_tracking_event())
    agent = SixDollarMoveAgent(
        setup_repository=repository,
        detection_lookup=lambda _: _detection(),
    )

    observations = agent.observe(
        _setup(DirectionContext.BULLISH),
        _inputs(high=2406.25, low=2399.0, close=2404.0),
    )

    assert len(observations) == 1
    assert observations[0].event_type is SetupEventType.FAVOURABLE_MOVE_6_REACHED
    assert observations[0].detail["favourable_excursion"] == "6.25000"


def test_bearish_setup_uses_intrabar_low_to_record_six_dollar_move() -> None:
    repository = _Repository()
    tracking = _tracking_event()
    tracking = tracking.model_copy(
        update={
            "context_snapshot": {
                **tracking.context_snapshot,
                "threshold_price": "2394.00000",
            }
        }
    )
    repository.events.rows.append(tracking)
    agent = SixDollarMoveAgent(
        setup_repository=repository,
        detection_lookup=lambda _: _detection(),
    )

    observations = agent.observe(
        _setup(DirectionContext.BEARISH),
        _inputs(high=2401.0, low=2393.75, close=2395.0),
    )

    assert len(observations) == 1
    assert observations[0].event_type is SetupEventType.FAVOURABLE_MOVE_6_REACHED
    assert observations[0].detail["favourable_excursion"] == "6.25000"


def test_reached_outcome_is_never_emitted_twice() -> None:
    repository = _Repository()
    repository.events.rows.extend(
        [
            _tracking_event(),
            _tracking_event().model_copy(
                update={
                    "event_id": "reached",
                    "event_type": SetupEventType.FAVOURABLE_MOVE_6_REACHED,
                }
            ),
        ]
    )
    agent = SixDollarMoveAgent(
        setup_repository=repository,
        detection_lookup=lambda _: _detection(),
    )

    assert agent.observe(
        _setup(),
        _inputs(high=2410.0, low=2399.0, close=2408.0),
    ) == ()


def test_one_large_candle_records_all_new_milestones() -> None:
    repository = _Repository()
    repository.events.rows.append(_tracking_event())
    agent = SixDollarMoveAgent(
        setup_repository=repository,
        detection_lookup=lambda _: _detection(),
    )

    observations = agent.observe(
        _setup(DirectionContext.BULLISH),
        _inputs(high=2442.0, low=2399.0, close=2435.0),
    )

    assert [one.event_type for one in observations] == [
        SetupEventType.FAVOURABLE_MOVE_6_REACHED,
        SetupEventType.FAVOURABLE_MOVE_20_REACHED,
        SetupEventType.FAVOURABLE_MOVE_40_REACHED,
    ]
