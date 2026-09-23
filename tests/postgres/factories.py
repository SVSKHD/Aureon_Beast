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


# ── S-3b: the smaller tables ──────────────────────────────────────────────────


def a_symbol_state(symbol: str = "XAUUSD", **overrides: object):
    """One symbol's live panel."""
    from aureon.models.enums import MarketState
    from aureon.models.system import SymbolState

    base = dict(
        symbol=symbol,
        timeframe=Timeframe.M5,
        market_state=MarketState.OPEN,
        last_closed_candle_time=CLOSE,
        detections_today=2,
        ema_fast=2403.1,
        ema_slow=2401.9,
    )
    return SymbolState(**(base | overrides))


def a_system_state(*symbols, **overrides: object):
    """The whole snapshot, carrying however many symbol panels it was given."""
    from aureon.models.system import SystemState

    base = dict(
        updated_at=CLOSE,
        symbols=tuple(symbols) if symbols else (a_symbol_state(),),
        trading_enabled=True,
    )
    return SystemState(**(base | overrides))


def a_symbol_spec(symbol: str = "XAUUSD", **overrides: object):
    """Broker metadata as MT5 reports it."""
    from aureon.models.market import SymbolInfo

    base = dict(
        symbol=symbol,
        point=0.01,
        digits=2,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
    )
    return SymbolInfo(**(base | overrides))


def an_ops_event(name: str = "observer_stale", scope: str | None = "XAUUSD", **overrides):
    """A named condition, currently true.

    ``event_id`` is the id the repository derives, so a round trip compares equal. A test
    that wants the disagreeing case passes ``event_id=`` explicitly.
    """
    from aureon.models.ops import OpsEvent

    base = dict(
        event_id=f"{name}__{scope.upper()}" if scope else name,
        name=name,
        scope=scope,
        service="observer",
        active=True,
        since=CLOSE,
        onsets=1,
        detail="no candle for 3 intervals",
    )
    return OpsEvent(**(base | overrides))


def an_alert(alert_id: str = "a1", **overrides: object):
    """An armed price alert.

    ``created_at`` is stamped although the MODEL allows it to be absent: ``arm`` sets it on
    every real alert and the column is NOT NULL, so a fixture without one describes an alert
    the store would never hold. Tests that go through ``arm`` never noticed, because ``arm``
    supplies it; one that writes a settled alert directly does.
    """
    from aureon.models.alerts import PriceAlert

    base = dict(
        alert_id=alert_id,
        symbol="XAUUSD",
        level=2410.0,
        side="above",
        requested_by="trader",
        created_at=CLOSE,
    )
    return PriceAlert(**(base | overrides))


def an_assessment(assessment_id: str = "as1", **overrides: object):
    """A measured-only readout over a REAL-history cohort (9D)."""
    from aureon.models.assessment import Assessment, CohortFilter, TrendRead
    from aureon.models.enums import HistorySource, TrendBias

    base = dict(
        assessment_id=assessment_id,
        detection_id="d1",
        symbol="XAUUSD",
        rule_id="XAU_OUTCOME_V2",
        trend_read=TrendRead(bias=TrendBias.BULLISH, candles=120),
        cohort_filter=CohortFilter(
            symbol="XAUUSD", agent_name="ema_cross", direction=Direction.BUY
        ),
        n=42,
        history_source=HistorySource.REAL,
        real_days=30,
        created_at=CLOSE,
    )
    return Assessment(**(base | overrides))


def a_trade_note(note_id: str = "n1", **overrides: object):
    """A trader's own words about a trade."""
    from aureon.models.assessment import TradeNote

    base = dict(
        note_id=note_id,
        trade_id="t1",
        author="trader",
        text="took half off at the first target",
        at=CLOSE,
    )
    return TradeNote(**(base | overrides))


def a_setup_evaluation(setup_id: str = "s1", rule_id: str = "XAU_OUTCOME_V2", **overrides):
    """One setup's outcome under one rule."""
    from aureon.models import threshold_key
    from aureon.models.enums import (
        DirectionContext,
        HorizonStatus,
        ReferencePrice,
        SetupFamily,
    )
    from aureon.models.evaluation import HorizonResult, SetupEvaluation

    base = dict(
        setup_id=setup_id,
        rule_id=rule_id,
        family=SetupFamily.TREND_PULLBACK,
        direction_context=DirectionContext.BULLISH,
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        market_date="2026-09-22",
        setup_version="1.0.0",
        reference_price=ReferencePrice.CLOSE,
        reference_value=2403.5,
        horizons=(
            HorizonResult(
                horizon_id="h5",
                status=HorizonStatus.COMPLETE,
                future_high=2411.0,
                future_low=2401.5,
                mfe=7.5,
                mae=-2.0,
                candles_seen=5,
                reached={threshold_key(3.0): True},
                time_to={threshold_key(3.0): 2.0},
                completed_at=CLOSE + timedelta(minutes=25),
            ),
        ),
    )
    return SetupEvaluation(**(base | overrides))


def a_session_summary(market_date: str = "2026-09-22", **overrides: object):
    """One broker day's London session."""
    from aureon.models.session import SessionSummary

    base = dict(
        session_id=f"{market_date}__london",
        account_scope="primary",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        session=SessionName.LONDON,
        market_date=market_date,
        session_config_version="1",
        started_at=MarketTime.from_utc(OPEN, TZ),
        ended_at=MarketTime.from_utc(CLOSE, TZ),
        open=2400.0,
        high=2411.0,
        low=2399.0,
        close=2403.5,
        trend="up",
        change=3.5,
        change_points=350.0,
        range=12.0,
        candle_count=96,
    )
    return SessionSummary(**(base | overrides))


def a_market_day(market_date: str = "2026-09-22", **overrides: object):
    """One finished broker day's shape."""
    from aureon.models.market_day import MarketDay

    base = dict(
        symbol="XAUUSD",
        market_date=market_date,
        market_tz=TZ,
        open=2400.0,
        high=2411.0,
        low=2399.0,
        close=2403.5,
        bars=288,
        tick_volume=123456,
        first_bar_at=OPEN,
        last_bar_at=CLOSE,
        complete=True,
        frames=(Timeframe.M15, Timeframe.H1),
    )
    return MarketDay(**(base | overrides))


def a_market_day_frame(market_date: str = "2026-09-22", **overrides: object):
    """One day's aggregated bars at one timeframe."""
    from aureon.models.market_day import FrameBar, MarketDayFrame

    base = dict(
        symbol="XAUUSD",
        market_date=market_date,
        timeframe=Timeframe.M15,
        market_tz=TZ,
        bars=(
            FrameBar(at=OPEN, open=2400.0, high=2405.0, low=2399.0, close=2404.0),
            FrameBar(at=CLOSE, open=2404.0, high=2411.0, low=2403.0, close=2403.5),
        ),
        truncated=False,
        complete=True,
    )
    return MarketDayFrame(**(base | overrides))


def a_daily_review(market_date: str = "2026-09-22", **overrides: object):
    """A daily review for one symbol."""
    from aureon.models.review import DailyReview

    base = dict(
        period_start=OPEN,
        period_end=CLOSE,
        market_tz=TZ,
        generated_at=CLOSE,
        evaluation_rule_id="XAU_OUTCOME_V2",
        symbol="XAUUSD",
        market_date=market_date,
        detections_total=7,
    )
    return DailyReview(**(base | overrides))


def a_weekly_review(iso_year: int = 2026, iso_week: int = 39, **overrides: object):
    """A weekly review for one symbol."""
    from aureon.models.review import WeeklyReview

    base = dict(
        period_start=OPEN,
        period_end=CLOSE,
        market_tz=TZ,
        generated_at=CLOSE,
        evaluation_rule_id="XAU_OUTCOME_V2",
        symbol="XAUUSD",
        iso_year=iso_year,
        iso_week=iso_week,
        detections_total=31,
    )
    return WeeklyReview(**(base | overrides))
