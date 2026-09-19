"""The Discord layer's rules (§27, §37-§47, §57, §59, §71).

Tested against ``service.py`` directly, which imports no discord.py: the rules that
protect real money -- who may confirm, whether a quote is too old, whether a lot is legal
-- are exercised without a gateway, a guild, or an event loop.

The four cases the phase names explicitly are here: an unauthorized user is rejected, the
wrong user clicking CONFIRM is rejected, a stale quote re-prompts, and ``/status`` renders
STALE when ``updated_at`` is 46 seconds old.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aureon.discord.service import (
    ENABLE_DETAILS,
    NO_REVIEW_YET,
    ConfirmGate,
    DraftRequest,
    NotAuthorized,
    authorize,
    build_confirmation,
    build_control_request,
    build_status,
    check_confirm_press,
    execution_modes,
    linkable_detections,
    summarise_review,
    trading_change_summary,
    unsupported_mode_notice,
    validate_lot,
)
from aureon.execution.fake_broker import DEFAULT_SYMBOL_INFO
from aureon.models.base import MarketTime, utc_now
from aureon.models.detection import Detection, SessionContext
from aureon.models.enums import (
    ControlRequestKind,
    Direction,
    FillingMode,
    Freshness,
    LinkType,
    MarketState,
    OrderType,
    SessionName,
    Timeframe,
    TradeRequestStatus,
)
from aureon.models.market import QuoteSnapshot
from aureon.models.settings import ExecutionSettings
from aureon.models.system import Heartbeat, SymbolState, SystemState

NOW = utc_now()
TZ = "Europe/Athens"
SYMBOL = "XAUUSD"
OWNER = "user-1"
ALLOWED = ("user-1", "user-2")


def quote(*, bid: float = 2400.00, ask: float = 2400.30, age: float = 0.0) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=SYMBOL, bid=bid, ask=ask, point=0.01, captured_at=NOW - timedelta(seconds=age)
    )


def settings(**overrides) -> ExecutionSettings:
    base = dict(
        trading_enabled=True,
        max_lot=1.0,
        max_spread_points=50.0,
        max_deviation_points=20,
        quote_ttl_seconds=15.0,
        confirmation_ttl_seconds=60.0,
        status_stale_after_seconds=45.0,
    )
    return ExecutionSettings(**(base | overrides))


def draft(**overrides) -> DraftRequest:
    base = dict(
        symbol=SYMBOL, order_type=OrderType.MARKET_BUY, volume=0.10, requested_by=OWNER
    )
    return DraftRequest(**(base | overrides))


def detection(
    *, minutes_ago: float = 5.0, symbol: str = SYMBOL, event: str = "bullish"
) -> Detection:
    moment = NOW - timedelta(minutes=minutes_ago)
    return Detection(
        detection_id=f"d-{event}-{minutes_ago}",
        account_scope="primary",
        symbol=symbol,
        timeframe=Timeframe.M5,
        agent_name="ema_cross",
        agent_version="1.0.0",
        event_key=event,
        direction=Direction.BUY,
        detected_at=MarketTime.from_utc(moment, TZ),
        candle_open_time=MarketTime.from_utc(moment - timedelta(minutes=5), TZ),
        price=2400.0,
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
    )


# ── §71: authorization ────────────────────────────────────────────────────────


def test_an_unauthorized_user_is_rejected() -> None:
    with pytest.raises(NotAuthorized, match="not authorized"):
        authorize("intruder", ALLOWED)


def test_an_authorized_user_passes() -> None:
    authorize("user-1", ALLOWED)


def test_an_empty_allowlist_authorises_nobody() -> None:
    """Treating empty as "everyone" would turn a missing env var into an open bot."""
    with pytest.raises(NotAuthorized, match="no authorized users"):
        authorize("user-1", ())


def test_authorization_compares_as_strings() -> None:
    """Discord ids arrive as ints in some paths and strings in others."""
    authorize(12345, ("12345",))  # type: ignore[arg-type]


# ── §27: who may confirm, and when ────────────────────────────────────────────


def request(**overrides):
    from aureon.models.trade import TradeRequest

    base = dict(
        request_id="r1",
        symbol=SYMBOL,
        order_type=OrderType.MARKET_BUY,
        volume=0.10,
        requested_by=OWNER,
        quote=quote(),
    )
    return TradeRequest(**(base | overrides))


def test_the_wrong_user_clicking_confirm_is_rejected() -> None:
    """§27. Discord messages are visible to a channel, so this is not hypothetical:
    another authorised user could otherwise authorise someone else's money."""
    gate = check_confirm_press(request(), "user-2", quote(), settings(), now=NOW)
    assert gate.ok is False
    assert "only user-1" in (gate.reason or "")
    assert gate.needs_refresh is False


def test_the_owner_may_confirm() -> None:
    assert check_confirm_press(request(), OWNER, quote(), settings(), now=NOW).ok is True


def test_a_stale_quote_re_prompts_rather_than_refusing() -> None:
    """§27. They still want the trade; they need current numbers."""
    gate = check_confirm_press(request(), OWNER, quote(age=30.0), settings(), now=NOW)
    assert gate.ok is False
    assert gate.needs_refresh is True
    assert "old" in (gate.reason or "")


def test_a_missing_quote_re_prompts_and_never_proceeds() -> None:
    gate = check_confirm_press(request(), OWNER, None, settings(), now=NOW)
    assert gate.ok is False
    assert gate.needs_refresh is True


def test_a_second_confirm_press_is_refused_not_repeated() -> None:
    """The repository's confirm() is idempotent, but saying "already confirmed" is
    honest where implying a second authorisation would not be."""
    gate = check_confirm_press(
        request(status=TradeRequestStatus.CONFIRMED), OWNER, quote(), settings(), now=NOW
    )
    assert gate.ok is False
    assert "already confirmed" in (gate.reason or "")
    assert gate.needs_refresh is False


@pytest.mark.parametrize(
    "status",
    [
        TradeRequestStatus.FILLED,
        TradeRequestStatus.FAILED,
        TradeRequestStatus.CANCELLED,
        TradeRequestStatus.EXECUTING,
    ],
)
def test_a_request_past_confirmation_cannot_be_confirmed(status) -> None:
    gate = check_confirm_press(request(status=status), OWNER, quote(), settings(), now=NOW)
    assert gate.ok is False
    assert gate.needs_refresh is False


def test_a_quote_inside_the_ttl_passes() -> None:
    assert check_confirm_press(request(), OWNER, quote(age=14.0), settings(), now=NOW).ok


# ── §42: lot validation before the confirmation screen ────────────────────────


def test_an_off_step_lot_is_rejected_with_a_readable_reason() -> None:
    check = validate_lot("0.015", DEFAULT_SYMBOL_INFO, settings())
    assert check.ok is False
    assert "volume_step" in (check.message or "")


def test_a_lot_over_aureons_max_is_rejected() -> None:
    check = validate_lot("5.0", DEFAULT_SYMBOL_INFO, settings(max_lot=1.0))
    assert check.ok is False
    assert "maximum lot" in (check.message or "")


@pytest.mark.parametrize("bad", ["abc", "", "0", "-1"])
def test_nonsense_lot_input_is_rejected(bad: str) -> None:
    assert validate_lot(bad, DEFAULT_SYMBOL_INFO, settings()).ok is False


def test_a_valid_lot_is_normalised() -> None:
    check = validate_lot("0.30", DEFAULT_SYMBOL_INFO, settings())
    assert check.ok is True
    assert check.normalized == pytest.approx(0.30)


def test_a_missing_symbol_spec_does_not_block_the_trade() -> None:
    """The published copy is advisory; the execution guard re-validates live (§56).

    Blocking on a stale or missing convenience copy would refuse legitimate trades for a
    reason that is not about the trade.
    """
    check = validate_lot("0.10", None, settings())
    assert check.ok is True
    # Aureon's own ceiling still applies even without the spec.
    assert validate_lot("5.0", None, settings(max_lot=1.0)).ok is False


# ── §39: execution modes ──────────────────────────────────────────────────────


def test_every_mode_is_listed_with_whether_it_is_supported() -> None:
    """Showing FOK as unavailable teaches more than omitting it silently."""
    info = DEFAULT_SYMBOL_INFO.model_copy(update={"filling_modes": (FillingMode.IOC,)})
    modes = dict(execution_modes(info))
    assert modes[FillingMode.IOC] is True
    assert modes[FillingMode.FOK] is False
    assert set(modes) == set(FillingMode)


def test_the_unsupported_mode_notice_names_what_is_available() -> None:
    info = DEFAULT_SYMBOL_INFO.model_copy(update={"filling_modes": (FillingMode.IOC,)})
    notice = unsupported_mode_notice(info, FillingMode.FOK)
    assert notice is not None
    assert "FOK not supported" in notice
    assert "IOC" in notice


def test_a_supported_mode_produces_no_notice() -> None:
    assert unsupported_mode_notice(DEFAULT_SYMBOL_INFO, FillingMode.FOK) is None


def test_an_unknown_spec_produces_no_notice() -> None:
    """Without metadata we cannot claim a mode is unsupported; the broker decides."""
    assert unsupported_mode_notice(None, FillingMode.FOK) is None


# ── §37: detection linking ────────────────────────────────────────────────────


def test_only_recent_detections_for_the_symbol_are_offered() -> None:
    """A detection from hours ago is not what someone is reacting to, and offering it
    would produce a link that misleads every review built on it."""
    candidates = linkable_detections(
        [
            detection(minutes_ago=5),
            detection(minutes_ago=200),  # outside the window
            detection(minutes_ago=5, symbol="EURUSD"),  # wrong symbol
        ],
        symbol=SYMBOL,
        window_minutes=90,
        now=NOW,
    )
    assert len(candidates) == 1
    assert candidates[0].symbol == SYMBOL


def test_detections_are_offered_newest_first_and_capped() -> None:
    many = [detection(minutes_ago=float(i), event=f"e{i}") for i in range(1, 30)]
    candidates = linkable_detections(many, symbol=SYMBOL, window_minutes=90, now=NOW, limit=10)
    assert len(candidates) == 10
    ages = [d.detected_at.utc for d in candidates]
    assert ages == sorted(ages, reverse=True)


def test_a_linked_request_is_always_an_explicit_link() -> None:
    """A human chose it from a list. Inferred links are Phase 7's and never touch a
    request (decision 8)."""
    built = draft(detection_id="d1").to_request()
    assert built.link_type is LinkType.EXPLICIT
    assert draft().to_request().link_type is None


def test_a_new_request_starts_as_requested() -> None:
    """Discord never confirms on the human's behalf."""
    assert draft().to_request().status is TradeRequestStatus.REQUESTED


# ── §40: the confirmation screen ──────────────────────────────────────────────


REQUIRED_FIELDS = {
    "Symbol",
    "Order type",
    "Direction",
    "Volume",
    "Entry",
    "Stop loss",
    "Take profit",
    "Execution mode",
    "Max deviation",
    "Bid / Ask",
    "Spread",
    "Quote age",
    "Market state",
    "Linked detection",
    "Requested by",
    "Confirmation expires in",
}


def test_the_confirmation_screen_shows_every_required_field() -> None:
    """§40. A missing field is a failing test rather than something a human notices at
    the worst possible moment."""
    screen = build_confirmation(
        draft(sl=2398.0, tp=2405.0),
        quote(),
        DEFAULT_SYMBOL_INFO,
        settings(),
        market_state=MarketState.OPEN,
        now=NOW,
    )
    assert REQUIRED_FIELDS <= set(screen.field_names())


def test_a_closed_market_is_warned_about_before_confirming() -> None:
    """Better now than as a surprise rejection after they press CONFIRM."""
    screen = build_confirmation(
        draft(), quote(), DEFAULT_SYMBOL_INFO, settings(), market_state=MarketState.CLOSED, now=NOW
    )
    assert any("closed" in w.lower() for w in screen.warnings)


def test_a_wide_spread_is_warned_about() -> None:
    screen = build_confirmation(
        draft(),
        quote(bid=2399.0, ask=2401.0),
        DEFAULT_SYMBOL_INFO,
        settings(max_spread_points=50.0),
        market_state=MarketState.OPEN,
        now=NOW,
    )
    assert any("Spread" in w for w in screen.warnings)


def test_disabled_trading_is_warned_about() -> None:
    screen = build_confirmation(
        draft(),
        quote(),
        DEFAULT_SYMBOL_INFO,
        settings(trading_enabled=False),
        market_state=MarketState.OPEN,
        now=NOW,
    )
    assert any("DISABLED" in w for w in screen.warnings)


def test_a_clean_trade_has_no_warnings() -> None:
    screen = build_confirmation(
        draft(), quote(), DEFAULT_SYMBOL_INFO, settings(), market_state=MarketState.OPEN, now=NOW
    )
    assert screen.warnings == []


def test_the_screen_reports_the_clamped_deviation() -> None:
    """§41: never widened. The human should see the value that will be used."""
    screen = build_confirmation(
        draft(deviation_points=500),
        quote(),
        DEFAULT_SYMBOL_INFO,
        settings(max_deviation_points=20),
        market_state=MarketState.OPEN,
        now=NOW,
    )
    assert ("Max deviation", "20 points") in screen.fields


def test_a_pending_order_shows_its_entry_not_the_market() -> None:
    screen = build_confirmation(
        draft(order_type=OrderType.BUY_STOP, price=2405.0),
        quote(),
        DEFAULT_SYMBOL_INFO,
        settings(),
        market_state=MarketState.OPEN,
        now=NOW,
    )
    assert ("Entry", "2405") in screen.fields


# ── §46, §47: control requests ────────────────────────────────────────────────


def test_a_cancel_request_carries_no_volume() -> None:
    """A pending order is cancelled whole; a volume would be meaningless."""
    built = build_control_request(ControlRequestKind.CANCEL, "12345", OWNER)
    assert built.kind is ControlRequestKind.CANCEL
    assert built.volume is None
    assert built.target == "12345"


def test_a_close_request_may_carry_a_partial_volume() -> None:
    built = build_control_request(ControlRequestKind.CLOSE, "555", OWNER, volume=0.10)
    assert built.volume == pytest.approx(0.10)


def test_control_requests_start_as_requested() -> None:
    from aureon.models.enums import ControlRequestStatus

    built = build_control_request(ControlRequestKind.CLOSE, "555", OWNER)
    assert built.status is ControlRequestStatus.REQUESTED


# ── §59: /status ──────────────────────────────────────────────────────────────


def test_status_renders_stale_when_updated_at_is_46_seconds_old() -> None:
    """The case the phase names explicitly, at the default 45s threshold."""
    screen = build_status(
        system_state=SystemState(updated_at=NOW - timedelta(seconds=46)),
        heartbeats={},
        settings=settings(status_stale_after_seconds=45.0),
        now=NOW,
    )
    assert screen.overall is Freshness.STALE


def test_status_renders_live_just_inside_the_threshold() -> None:
    screen = build_status(
        system_state=SystemState(updated_at=NOW - timedelta(seconds=44)),
        heartbeats={},
        settings=settings(status_stale_after_seconds=45.0),
        now=NOW,
    )
    assert screen.overall is Freshness.LIVE


def test_status_renders_offline_with_no_state_at_all() -> None:
    screen = build_status(system_state=None, heartbeats={}, settings=settings(), now=NOW)
    assert screen.overall is Freshness.OFFLINE


def test_every_service_is_listed_even_when_it_never_reported() -> None:
    """A missing service must be visible as missing, not simply absent from the list."""
    screen = build_status(
        system_state=SystemState(updated_at=NOW),
        heartbeats={"observer": Heartbeat(service="observer", updated_at=NOW)},
        settings=settings(),
        now=NOW,
    )
    names = {s.name for s in screen.services}
    assert names == {"observer", "executor", "monitor", "discord"}
    executor = next(s for s in screen.services if s.name == "executor")
    assert executor.freshness is Freshness.OFFLINE
    assert executor.detail == "never reported"


def test_status_reports_the_trading_switch() -> None:
    screen = build_status(
        system_state=SystemState(updated_at=NOW),
        heartbeats={},
        settings=settings(trading_enabled=False),
        now=NOW,
    )
    assert screen.trading_enabled is False


def test_a_closed_market_shows_the_review_placeholder_until_phase_7() -> None:
    """§61-§63. Saying so plainly beats an empty panel."""
    screen = build_status(
        system_state=SystemState(
            updated_at=NOW,
            symbols=(
                SymbolState(
                    symbol=SYMBOL, timeframe=Timeframe.M5, market_state=MarketState.CLOSED
                ),
            ),
        ),
        heartbeats={},
        settings=settings(),
        latest_review=None,
        now=NOW,
    )
    assert screen.market_closed is True
    assert screen.review_summary == NO_REVIEW_YET


def test_an_open_market_shows_no_review_section() -> None:
    screen = build_status(
        system_state=SystemState(
            updated_at=NOW,
            symbols=(
                SymbolState(symbol=SYMBOL, timeframe=Timeframe.M5, market_state=MarketState.OPEN),
            ),
        ),
        heartbeats={},
        settings=settings(),
        now=NOW,
    )
    assert screen.market_closed is False
    assert screen.review_summary is None


def test_a_review_summary_reports_the_excluded_pending_count() -> None:
    """A reached-N figure without it invites exactly the misreading Phase 3 prevents."""

    class FakeReview:
        detections_total = 12
        trades_total = 3
        realized_pnl = 41.5
        pending_horizons_excluded = 40
        evaluation_rule_id = "EMA_OUTCOME_V1"

    summary = summarise_review(FakeReview())
    assert "12 detections" in summary
    assert "40 horizons still pending" in summary
    assert "EMA_OUTCOME_V1" in summary


# ── §57: the trading switch ───────────────────────────────────────────────────


def test_the_enable_details_name_what_to_check_first() -> None:
    """§57. Enabling arms real money, so the prompt has to say what that means."""
    assert "real money" in ENABLE_DETAILS
    assert "/status" in ENABLE_DETAILS
    assert "max_lot" in ENABLE_DETAILS


def test_a_disable_summary_records_the_reason() -> None:
    assert "drawdown" in trading_change_summary(False, OWNER, "drawdown")
    assert "DISABLED" in trading_change_summary(False, OWNER)
    assert "ENABLED" in trading_change_summary(True, OWNER)


def test_a_confirm_gate_is_a_plain_value() -> None:
    """Deliberately data, not a raised exception: every caller renders it."""
    assert ConfirmGate(True).ok is True
    assert ConfirmGate(False, "no").reason == "no"
