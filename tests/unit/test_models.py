"""Model invariants (decisions 2, 8, 12, 13, 14, 15).

The validators tested here are the ones that stop a bad document from ever
reaching Firestore, so each test names the failure it prevents rather than only
the rule it checks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from aureon.models import (
    SCHEMA_VERSION,
    BrokerOrderRequest,
    Candle,
    Detection,
    DetectionEvaluation,
    Direction,
    EvaluationRule,
    ExecutionSettings,
    FailureCode,
    Horizon,
    HorizonKind,
    HorizonResult,
    HorizonStatus,
    IndicatorSnapshot,
    InferredLink,
    LinkType,
    MarketTime,
    OrderType,
    PathClassification,
    QuoteSnapshot,
    ReferencePrice,
    SessionContext,
    SessionName,
    SymbolInfo,
    ThresholdOutcome,
    Timeframe,
    Trade,
    TradeRequest,
    TradeRequestStatus,
    TradeStatus,
    threshold_key,
    utc_now,
)

TZ = "Europe/Athens"
OPEN_TIME = MarketTime.from_utc(datetime(2026, 9, 18, 13, 5, tzinfo=UTC), TZ)
CLOSE_TIME = MarketTime.from_utc(datetime(2026, 9, 18, 13, 10, tzinfo=UTC), TZ)
SESSION = SessionContext(session=SessionName.LONDON, session_config_version=1)


def _detection(**overrides: object) -> Detection:
    base = dict(
        detection_id="d1",
        account_scope="primary",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        agent_name="ema_cross",
        agent_version="1.0.0",
        event_key="bullish",
        direction=Direction.BUY,
        detected_at=CLOSE_TIME,
        candle_open_time=OPEN_TIME,
        price=2403.5,
        session=SESSION,
        sequence_today=1,
        sequence_session=1,
    )
    return Detection(**(base | overrides))


# ── Timestamps ────────────────────────────────────────────────────────────────


def test_naive_timestamps_cannot_enter_a_model() -> None:
    """CLAUDE.md: every timestamp is tz-aware.

    A naive datetime would be interpreted as whatever zone the reader assumes,
    which for a candle boundary means silently wrong sessions.
    """
    with pytest.raises((ValidationError, ValueError)):
        MarketTime(utc=datetime(2026, 1, 1), market_tz=TZ)


def test_market_time_renders_both_clocks_consistently() -> None:
    assert OPEN_TIME.utc.tzinfo is UTC
    assert OPEN_TIME.market.utcoffset() == timedelta(hours=3)
    assert OPEN_TIME.market_date == "2026-09-18"


def test_market_date_follows_the_broker_day_not_utc() -> None:
    """23:30 UTC is already the next day in Athens.

    Getting this wrong would file a detection under the wrong trading day and
    corrupt every per-day count in the reviews.
    """
    late = MarketTime.from_utc(datetime(2026, 9, 18, 23, 30, tzinfo=UTC), TZ)
    assert late.utc.date().isoformat() == "2026-09-18"
    assert late.market_date == "2026-09-19"


# ── Detections ────────────────────────────────────────────────────────────────


def test_every_document_carries_a_schema_version() -> None:
    """Decision 12 (§6)."""
    assert _detection().schema_version == SCHEMA_VERSION


def test_detections_are_immutable() -> None:
    """CLAUDE.md: detections are immutable; outcomes go elsewhere."""
    detection = _detection()
    with pytest.raises(ValidationError):
        detection.price = 9999.0


def test_a_detection_rejects_an_unknown_field() -> None:
    """The structural half of "no field encodes future information".

    A future-information field such as ``outcome_reached_5`` cannot be added by
    accident; adding one deliberately means editing the model, where review will
    see it.
    """
    with pytest.raises(ValidationError):
        Detection(**(_detection().model_dump() | {"outcome_reached_5": True}))


def test_context_only_detections_may_omit_direction() -> None:
    """Wick and RSI agents contribute context, not a tradeable direction."""
    assert _detection(direction=None).is_context_only is True
    assert _detection().is_context_only is False


def test_indicator_values_are_stored_not_labels() -> None:
    snapshot = IndicatorSnapshot(ema={"fast": 2402.1, "slow": 2401.4}, rsi=61.4)
    assert snapshot.ema["fast"] == 2402.1
    assert snapshot.rsi == 61.4


# ── Candles and symbols ───────────────────────────────────────────────────────


def test_incoherent_candles_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Candle(
            symbol="XAUUSD",
            timeframe=Timeframe.M5,
            open_time=OPEN_TIME,
            open=2400,
            high=2399,
            low=2401,
            close=2400,
        )


def test_candle_close_time_follows_the_timeframe() -> None:
    candle = Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=OPEN_TIME,
        open=2400.0,
        high=2405.0,
        low=2399.0,
        close=2403.5,
    )
    assert candle.close_time == OPEN_TIME.utc + timedelta(minutes=5)
    assert candle.upper_wick == pytest.approx(1.5)
    assert candle.lower_wick == pytest.approx(1.0)


def test_off_grid_volume_is_rejected_never_rounded() -> None:
    """§42: silently rounding would trade a size the human did not authorise."""
    info = SymbolInfo(
        symbol="XAUUSD",
        point=0.01,
        digits=2,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
    )
    assert info.normalize_volume(0.30) == pytest.approx(0.30)
    for bad in (0.005, 100.0, 0.015):
        with pytest.raises(ValueError):
            info.normalize_volume(bad)


def test_close_only_symbols_are_not_tradeable() -> None:
    """Treating close_only as open would surface as an opaque broker rejection
    after a human had already confirmed."""
    info = SymbolInfo(
        symbol="X",
        point=0.01,
        digits=2,
        volume_min=0.01,
        volume_max=1.0,
        volume_step=0.01,
        trade_mode="close_only",
    )
    assert info.is_tradeable is False


def test_spread_in_points_is_unknown_without_a_point_size() -> None:
    """The spread guard must fail closed rather than compare to a guessed scale."""
    assert QuoteSnapshot(symbol="X", bid=1.0, ask=2.0).spread_points is None
    assert QuoteSnapshot(symbol="X", bid=1.0, ask=2.0, point=0.5).spread_points == 2.0


def test_quote_staleness_is_measured_not_stored() -> None:
    now = utc_now()
    quote = QuoteSnapshot(symbol="X", bid=1.0, ask=1.1, captured_at=now - timedelta(seconds=30))
    assert quote.is_stale(15, now=now) is True
    assert quote.is_stale(60, now=now) is False


# ── Trade requests (decisions 2, 8) ───────────────────────────────────────────


def _request(**overrides: object) -> TradeRequest:
    base = dict(
        request_id="r1",
        symbol="XAUUSD",
        order_type=OrderType.MARKET_BUY,
        volume=0.10,
        requested_by="user-1",
    )
    return TradeRequest(**(base | overrides))


def test_a_request_cannot_claim_an_inferred_link() -> None:
    """Decision 8: a guess must never harden into a fact about a real trade."""
    with pytest.raises(ValidationError, match="inferred"):
        _request(detection_id="d1", link_type=LinkType.INFERRED)


def test_a_linked_detection_requires_an_explicit_link_type() -> None:
    with pytest.raises(ValidationError):
        _request(detection_id="d1")
    assert _request(detection_id="d1", link_type=LinkType.EXPLICIT).detection_id == "d1"


def test_a_failure_code_requires_a_failed_status() -> None:
    """Decision 2: a failure is a status plus a code.

    A code on a live request would render a misleading reason in Discord.
    """
    with pytest.raises(ValidationError, match="non-failed"):
        _request(failure_code=FailureCode.SPREAD_LIMIT)
    ok = _request(status=TradeRequestStatus.FAILED, failure_code=FailureCode.SPREAD_LIMIT)
    assert ok.failure_code is FailureCode.SPREAD_LIMIT


def test_lease_is_held_only_by_its_owner_and_only_until_it_expires() -> None:
    """The gate that stops two workers resolving one request (§29)."""
    now = utc_now()
    live = _request(executor_instance_id="exec-1", lease_expires_at=now + timedelta(seconds=30))
    assert live.lease_held_by("exec-1", now=now) is True
    assert live.lease_held_by("exec-2", now=now) is False
    expired = _request(
        executor_instance_id="exec-1", lease_expires_at=now - timedelta(seconds=1)
    )
    assert expired.lease_held_by("exec-1", now=now) is False
    assert expired.lease_expired(now=now) is True


def test_confirmation_expiry_is_evaluated_against_a_deadline() -> None:
    now = utc_now()
    assert _request(expires_at=now - timedelta(seconds=1)).confirmation_expired(now=now) is True
    assert _request(expires_at=now + timedelta(seconds=30)).confirmation_expired(now=now) is False
    # No deadline set means nothing to expire yet (still REQUESTED).
    assert _request().confirmation_expired(now=now) is False


def test_a_pending_order_needs_an_entry_price() -> None:
    with pytest.raises(ValidationError):
        BrokerOrderRequest(
            symbol="X", order_type=OrderType.BUY_STOP, volume=1.0, magic=1, comment="AUR:ABCDEF"
        )


def test_broker_comment_fits_the_mt5_field() -> None:
    with pytest.raises(ValidationError):
        BrokerOrderRequest(
            symbol="X",
            order_type=OrderType.MARKET_BUY,
            volume=1.0,
            magic=1,
            comment="x" * 32,
        )


# ── Trades (decisions 8, 15) ──────────────────────────────────────────────────


def _trade(**overrides: object) -> Trade:
    base = dict(
        trade_id="t1",
        mt5_position_id=555,
        symbol="XAUUSD",
        direction=Direction.BUY,
        volume=0.25,
        open_price=2403.5,
        open_time=OPEN_TIME,
    )
    return Trade(**(base | overrides))


def test_a_trade_cannot_claim_an_inferred_link() -> None:
    with pytest.raises(ValidationError, match="inferred"):
        _trade(detection_id="d1", link_type=LinkType.INFERRED)


def test_closed_volume_cannot_exceed_opened_volume() -> None:
    with pytest.raises(ValidationError):
        _trade(closed_volume=0.5)
    assert _trade(closed_volume=0.10).remaining_volume == pytest.approx(0.15)


def test_a_closed_trade_must_say_when_it_closed() -> None:
    with pytest.raises(ValidationError):
        _trade(status=TradeStatus.CLOSED)
    assert _trade(status=TradeStatus.CLOSED, close_time=CLOSE_TIME).close_time is not None


def test_close_reason_is_a_convention_not_yet_an_enum() -> None:
    """Decision 15: an unrecognised broker reason is stored, not discarded.

    Discarding it would destroy the evidence needed to decide what the enum
    should contain once Phase 5 shows what MT5 actually reports.
    """
    trade = _trade(
        status=TradeStatus.CLOSED,
        close_time=CLOSE_TIME,
        close_reason="so_unexpected",
        close_reason_raw="mt5_reason_42",
    )
    assert trade.close_reason == "so_unexpected"
    assert trade.close_reason_raw == "mt5_reason_42"


# ── Evaluation (decisions 13, 14) ─────────────────────────────────────────────


def test_a_shipped_rule_cannot_be_retuned() -> None:
    """§21: a threshold change is a NEW rule_id, never an edit.

    Editing one in place would silently redefine what every stored result meant.
    """
    rule = EvaluationRule(
        rule_id="EMA_OUTCOME_V1",
        reference_price=ReferencePrice.NEXT_OPEN,
        horizons=(Horizon(id="c5", kind=HorizonKind.CANDLES, value=5),),
        thresholds=(3, 5, 10),
    )
    with pytest.raises(ValidationError):
        rule.thresholds = (1, 2)


def test_thresholds_must_be_positive_and_ascending() -> None:
    def build(thresholds: tuple[float, ...]) -> EvaluationRule:
        return EvaluationRule(
            rule_id="R",
            reference_price=ReferencePrice.CLOSE,
            horizons=(Horizon(id="c5", kind=HorizonKind.CANDLES, value=5),),
            thresholds=thresholds,
        )

    with pytest.raises(ValidationError):
        build((10, 5))
    with pytest.raises(ValidationError):
        build((0, 5))


def test_counted_and_event_driven_horizons_are_distinguished() -> None:
    with pytest.raises(ValidationError):
        Horizon(id="x", kind=HorizonKind.CANDLES)
    with pytest.raises(ValidationError):
        Horizon(id="x", kind=HorizonKind.SESSION_CLOSE, value=5)
    assert Horizon(id="x", kind=HorizonKind.SESSION_CLOSE).value is None


def test_threshold_keys_are_canonical() -> None:
    """Decision 14: 3 and 3.0 must not become two buckets."""
    assert threshold_key(3) == threshold_key(3.0) == "3"
    assert threshold_key(2.5) == "2.5"


def test_pending_means_unknown_not_missed() -> None:
    """The distinction that keeps hindsight out of the reviews.

    ``reached_threshold`` returns None so a caller cannot read "unknown" as "no"
    without noticing.
    """
    pending = HorizonResult(horizon_id="c5")
    assert pending.reached_threshold(3) is None


def _complete(**overrides: object) -> HorizonResult:
    base = dict(
        horizon_id="c5",
        status=HorizonStatus.COMPLETE,
        future_high=2410.0,
        future_low=2400.0,
        mfe=12.0,
        mae=-4.0,
        reached={"3": True, "5": True, "10": True, "15": False},
        time_to={"3": 60.0, "5": 120.0, "10": 300.0, "15": None},
        path=PathClassification.MFE_FIRST,
        completed_at=utc_now(),
    )
    return HorizonResult(**(base | overrides))


def test_a_complete_horizon_must_carry_its_excursions() -> None:
    """Decision 13: otherwise the reviews count it as an answer carrying none."""
    with pytest.raises(ValidationError, match="COMPLETE requires"):
        HorizonResult(horizon_id="c5", status=HorizonStatus.COMPLETE, reached={"3": True})
    assert _complete().reached_threshold(10) is True
    assert _complete().reached_threshold(15) is False


def test_a_complete_horizon_must_record_when_it_completed() -> None:
    with pytest.raises(ValidationError):
        _complete(completed_at=None)


def test_an_invalid_horizon_must_say_why() -> None:
    """Decision 13: otherwise a data gap is indistinguishable from a bug."""
    with pytest.raises(ValidationError, match="invalid_reason"):
        HorizonResult(horizon_id="c5", status=HorizonStatus.INVALID)
    ok = HorizonResult(
        horizon_id="c5", status=HorizonStatus.INVALID, invalid_reason="candle gap > 2 timeframes"
    )
    assert ok.invalid_reason


def test_time_to_cannot_describe_a_threshold_reached_does_not() -> None:
    with pytest.raises(ValidationError):
        HorizonResult(horizon_id="c5", reached={"3": True}, time_to={"3": 1.0, "99": 2.0})


def test_complete_horizons_is_the_only_safe_accessor() -> None:
    """Phase 7 aggregates through this so PENDING is never counted as a miss."""
    evaluation = DetectionEvaluation(
        detection_id="d1",
        rule_id="EMA_OUTCOME_V1",
        reference_price=ReferencePrice.NEXT_OPEN,
        horizons=(_complete(), HorizonResult(horizon_id="session_close")),
    )
    assert len(evaluation.horizons) == 2
    assert len(evaluation.complete_horizons) == 1
    assert len(evaluation.pending_horizons) == 1
    assert evaluation.is_fully_evaluated is False


def test_duplicate_horizon_results_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        DetectionEvaluation(
            detection_id="d1",
            rule_id="R",
            reference_price=ReferencePrice.CLOSE,
            horizons=(_complete(), _complete()),
        )


# ── Reviews and settings (decisions 8, 11) ────────────────────────────────────


def test_inferred_links_live_only_on_reviews_and_admit_their_confidence() -> None:
    link = InferredLink(detection_id="d1", trade_id="t1", confidence=0.8)
    assert link.link_type is LinkType.INFERRED
    with pytest.raises(ValidationError):
        InferredLink(detection_id="d", trade_id="t", confidence=0.5, link_type=LinkType.EXPLICIT)
    with pytest.raises(ValidationError):
        InferredLink(detection_id="d", trade_id="t", confidence=1.5)


def test_no_data_is_not_a_zero_hit_rate() -> None:
    """A rate of 0.0 claims the threshold was never reached -- that is a finding.
    No data is not a finding."""
    assert ThresholdOutcome(threshold=20).rate is None
    assert ThresholdOutcome(threshold=3, reached=7, evaluated=10).rate == pytest.approx(0.7)
    with pytest.raises(ValidationError):
        ThresholdOutcome(threshold=3, reached=11, evaluated=10)


def test_trading_is_disabled_by_default() -> None:
    """Decision 11: a fresh deploy must not be able to trade."""
    assert ExecutionSettings().trading_enabled is False


def test_an_empty_symbol_allowlist_is_not_an_allowlist() -> None:
    assert ExecutionSettings().symbol_allowed("XAUUSD") is True
    assert ExecutionSettings(allowed_symbols=("XAUUSD",)).symbol_allowed("EURUSD") is False
