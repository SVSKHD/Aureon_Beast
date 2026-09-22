"""Domain models for the repository tests, built once and shared.

Built from the real pydantic models rather than from dicts, because the thing under test
is the ROUND TRIP: a model written to a row and read back must be equal to what went in.
A test that constructed a dict by hand would be asserting that the repository agrees with
the test's idea of the model, which is not the same claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aureon.models.base import MarketTime
from aureon.models.detection import Detection, SessionContext
from aureon.models.enums import (
    Direction,
    OrderType,
    SessionName,
    Timeframe,
    TradeRequestStatus,
)
from aureon.models.trade import TradeRequest

TZ = "Europe/Athens"
OPEN = datetime(2026, 9, 22, 13, 5, tzinfo=UTC)
CLOSE = datetime(2026, 9, 22, 13, 10, tzinfo=UTC)
SESSION = SessionContext(session=SessionName.LONDON, session_config_version=1)


def a_detection(detection_id: str = "d1", **overrides: object) -> Detection:
    base = dict(
        detection_id=detection_id,
        account_scope="primary",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        agent_name="ema_cross",
        agent_version="2.2.0",
        event_key="bullish_cross",
        direction=Direction.BUY,
        detected_at=MarketTime.from_utc(CLOSE, TZ),
        candle_open_time=MarketTime.from_utc(OPEN, TZ),
        price=2403.5,
        session=SESSION,
        sequence_today=1,
        sequence_session=1,
    )
    return Detection(**(base | overrides))


def a_trade_request(request_id: str = "r1", **overrides: object) -> TradeRequest:
    base = dict(
        request_id=request_id,
        symbol="XAUUSD",
        order_type=OrderType.MARKET_BUY,
        volume=0.10,
        requested_by="trader",
        requested_at=CLOSE,
    )
    return TradeRequest(**(base | overrides))


def a_confirmed_request(
    request_id: str = "r1",
    *,
    ttl_seconds: float = 60.0,
    **overrides: object,
) -> TradeRequest:
    """A request a human has authorised, with a live confirmation.

    The state §15's claim operates on. ``expires_at`` is set explicitly rather than left
    to a default, because the stale path is the one behaviour of the claim that is easy to
    get wrong and every test of it needs to control the clock.
    """
    return a_trade_request(
        request_id,
        status=TradeRequestStatus.CONFIRMED,
        confirmed_by="trader",
        confirmed_at=CLOSE,
        expires_at=CLOSE + timedelta(seconds=ttl_seconds),
        **overrides,
    )


def an_evaluation(
    detection_id: str = "d1",
    rule_id: str = "XAU_OUTCOME_V2",
    **overrides: object,
):
    """A detection evaluation with one PENDING and one COMPLETE horizon.

    Both states on purpose: §22's whole point is that a COMPLETE horizon is frozen and a
    PENDING one is not yet an answer, and a round trip that only ever carried one of them
    would not show that the distinction survived storage.
    """
    from aureon.models import threshold_key
    from aureon.models.enums import HorizonStatus, ReferencePrice
    from aureon.models.evaluation import DetectionEvaluation, HorizonResult

    base = dict(
        detection_id=detection_id,
        rule_id=rule_id,
        reference_price=ReferencePrice.CLOSE,
        reference_value=2403.5,
        horizons=(
            HorizonResult(
                horizon_id="h5",
                status=HorizonStatus.COMPLETE,
                # decision 13: a COMPLETE horizon must carry the extremes it was measured
                # from. The model refuses one without them, which is why they are here.
                future_high=2411.0,
                future_low=2401.5,
                mfe=7.5,
                mae=-2.0,
                candles_seen=5,
                # And an answer for every threshold, which the model also requires: a
                # COMPLETE horizon with no reached[] would be a finished measurement that
                # measured nothing.
                reached={threshold_key(3.0): True, threshold_key(20.0): False},
                time_to={threshold_key(3.0): 2.0, threshold_key(20.0): None},
                # And when it finished. §22 freezes a COMPLETE horizon, so the moment it
                # became frozen is part of what makes it re-readable a month later.
                completed_at=CLOSE + timedelta(minutes=25),
            ),
            HorizonResult(horizon_id="h10", status=HorizonStatus.PENDING),
        ),
    )
    return DetectionEvaluation(**(base | overrides))


def a_setup(setup_id: str = "s1", **overrides: object):
    """A setup freshly opened, in OBSERVING."""
    from aureon.models.enums import DirectionContext, SetupAnchorKind, SetupFamily
    from aureon.models.setup import Setup, SetupAnchor

    base = dict(
        setup_id=setup_id,
        account_scope="primary",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        family=SetupFamily.TREND_PULLBACK,
        direction_context=DirectionContext.BULLISH,
        market_date="2026-09-22",
        anchor=SetupAnchor(kind=SetupAnchorKind.EMA_ZONE, price=2400.0),
        opened_at=CLOSE,
    )
    return Setup(**(base | overrides))


def an_event(
    event_id: str = "e1",
    setup_id: str = "s1",
    *,
    to_state=None,
    event_type=None,
    minutes: int = 5,
    **overrides: object,
):
    """One setup event.

    ``from_state`` is supplied because the model requires it, and then OVERWRITTEN by the
    repository from the setup it locked -- which is the point: a caller cannot assert what
    the setup's state was, only the transaction can know it.
    """
    from aureon.models.enums import SetupEventType, SetupState
    from aureon.models.setup import SetupEvent

    base = dict(
        event_id=event_id,
        setup_id=setup_id,
        event_type=event_type or SetupEventType.WATCH_STARTED,
        from_state=SetupState.OBSERVING,
        to_state=to_state or SetupState.WATCH,
        market_time=MarketTime.from_utc(CLOSE + timedelta(minutes=minutes), TZ),
    )
    return SetupEvent(**(base | overrides))


def a_control_request(control_id: str = "c1", **overrides: object):
    """A control request in REQUESTED."""
    from aureon.models.control import ControlRequest
    from aureon.models.enums import ControlRequestKind

    base = dict(
        control_id=control_id,
        kind=ControlRequestKind.CLOSE,
        target="123456789012",
        requested_by="trader",
        requested_at=CLOSE,
    )
    return ControlRequest(**(base | overrides))


def a_trade(position_id: int = 123456789012, **overrides: object):
    """An open trade, keyed on the broker's position id.

    A ten-digit position id on purpose: that is what a real MT5 ticket looks like now, and
    it is the value an ``int4`` column would have silently failed on.
    """
    from aureon.models.enums import Direction, TradeSource
    from aureon.models.trade import Trade
    from aureon.storage.postgres.repositories.trades import trade_id_for

    base = dict(
        trade_id=trade_id_for(position_id, account_scope="primary"),
        mt5_position_id=position_id,
        source=TradeSource.AUREON,
        symbol="XAUUSD",
        direction=Direction.BUY,
        volume=0.10,
        open_price=2403.6,
        open_time=MarketTime.from_utc(CLOSE, TZ),
    )
    return Trade(**(base | overrides))
