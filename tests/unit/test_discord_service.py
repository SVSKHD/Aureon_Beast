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
    check_target_symbol,
    execution_modes,
    for_symbol,
    linkable_detections,
    market_filling_mode,
    market_state_of,
    plan_market_order,
    quote_of,
    resolve_close_target,
    summarise_review,
    trading_change_summary,
    unobserved_symbol_notice,
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
from aureon.models.settings import ExecutionSettings, SymbolLimits
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


def test_the_per_symbol_ceiling_applies_without_a_published_spec() -> None:
    """9A. ``validate_lot`` takes the symbol from the caller, not only from the spec.

    A missing or stale published ``SymbolInfo`` must not silently promote a symbol back
    to the global ceiling, which on silver is a different notional entirely.
    """
    limited = settings(max_lot=1.0, per_symbol={SILVER: SymbolLimits(max_lot=0.20)})
    assert validate_lot("0.50", None, limited, symbol=SILVER).ok is False
    assert validate_lot("0.50", None, limited, symbol=SYMBOL).ok is True


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


# ── 9A: /execute, the market-order shortcut ───────────────────────────────────

SILVER = "XAGUSD"
SILVER_INFO = DEFAULT_SYMBOL_INFO.model_copy(
    update={"symbol": SILVER, "point": 0.001, "digits": 3}
)
OBSERVED = (SYMBOL, SILVER)


def plan(**overrides):
    base = dict(
        symbol=SILVER,
        side="buy",
        lot="0.10",
        requested_by=OWNER,
        settings=settings(),
        info=SILVER_INFO,
        observed_symbols=OBSERVED,
    )
    return plan_market_order(**(base | overrides))


def test_a_market_shortcut_plan_carries_everything_the_request_needs() -> None:
    result = plan()
    assert result.ok is True
    assert result.draft is not None
    assert result.draft.symbol == SILVER
    assert result.draft.order_type is OrderType.MARKET_BUY
    assert result.draft.volume == pytest.approx(0.10)
    assert result.draft.requested_by == OWNER
    # The mode is named, never left to a broker default (§39).
    assert result.draft.filling_mode is FillingMode.FOK
    assert result.filling_note is None


@pytest.mark.parametrize(
    ("side", "expected"),
    [
        ("buy", OrderType.MARKET_BUY),
        ("sell", OrderType.MARKET_SELL),
        ("SELL", OrderType.MARKET_SELL),
    ],
)
def test_both_sides_parse_case_insensitively(side: str, expected: OrderType) -> None:
    result = plan(side=side)
    assert result.ok is True
    assert result.draft is not None
    assert result.draft.order_type is expected


@pytest.mark.parametrize("side", ["long", "b", "", "market_buy"])
def test_a_side_aureon_does_not_recognise_is_refused_not_guessed(side: str) -> None:
    """Guessing at "long" on the one command that moves money is not a convenience."""
    result = plan(side=side)
    assert result.ok is False
    assert result.draft is None


def test_a_symbol_this_deployment_does_not_observe_is_refused() -> None:
    """There would be no quote, no spec and no detection history to put on the screen."""
    result = plan(symbol="EURUSD", info=None)
    assert result.ok is False
    assert "not observed" in (result.message or "")
    assert "XAUUSD" in (result.message or "")


def test_a_lot_over_the_per_symbol_maximum_is_refused_before_any_screen_exists() -> None:
    """9A. One lot is 100 oz of gold and 5000 oz of silver.

    The refusal happens in the plan, which is what the command needs: no draft means no
    ``trade_requests`` document and no embed, so there is nothing for the executor to
    find and nothing for a human to press.
    """
    limited = settings(max_lot=1.0, per_symbol={SILVER: SymbolLimits(max_lot=0.20)})
    result = plan(lot="0.50", settings=limited)
    assert result.ok is False
    assert result.draft is None
    assert "0.2" in (result.message or "")
    assert SILVER in (result.message or "")
    # The same lot is fine on the symbol that has no override.
    assert plan(symbol=SYMBOL, lot="0.50", info=DEFAULT_SYMBOL_INFO, settings=limited).ok
    # And with no published spec at all, the ceiling is still this symbol's: the limit is
    # looked up from what the human typed, not from a convenience copy that may be absent.
    assert plan(lot="0.50", info=None, settings=limited).ok is False


def test_the_per_symbol_deviation_reaches_the_draft() -> None:
    """A point is different money on every instrument, so the number must follow."""
    limited = settings(
        max_deviation_points=20, per_symbol={SILVER: SymbolLimits(max_deviation_points=5)}
    )
    silver = plan(settings=limited)
    gold = plan(symbol=SYMBOL, info=DEFAULT_SYMBOL_INFO, settings=limited)
    assert silver.draft is not None and gold.draft is not None
    assert silver.draft.deviation_points == 5
    assert gold.draft.deviation_points == 20


def test_an_ioc_substitution_is_stated_rather_than_made_silently() -> None:
    info = SILVER_INFO.model_copy(update={"filling_modes": (FillingMode.IOC,)})
    result = plan(info=info)
    assert result.ok is True
    assert result.draft is not None
    assert result.draft.filling_mode is FillingMode.IOC
    assert "FOK is not supported" in (result.filling_note or "")
    assert "part of the volume" in (result.filling_note or "")


def test_a_symbol_with_no_market_filling_mode_refuses_rather_than_defaulting() -> None:
    """§39: RETURN leaves a resting remainder, which is not a market execution."""
    info = SILVER_INFO.model_copy(update={"filling_modes": (FillingMode.RETURN,)})
    result = plan(info=info)
    assert result.ok is False
    assert result.draft is None
    assert "/execute-trade" in (result.message or "")


def test_a_missing_spec_proceeds_but_says_the_mode_is_unknown() -> None:
    """The published copy is advisory; the guard re-reads the live symbol (§56)."""
    result = plan(info=None)
    assert result.ok is True
    assert result.draft is not None
    assert result.draft.filling_mode is None
    assert "No filling mode published" in (result.filling_note or "")


def test_a_detection_for_another_symbol_is_refused_not_attached() -> None:
    """§50. A link that misdescribes what was acted on poisons every review on it."""
    result = plan(detection=detection(symbol=SYMBOL))
    assert result.ok is False
    assert result.draft is None
    assert SYMBOL in (result.message or "") and SILVER in (result.message or "")


def test_a_detection_for_this_symbol_is_linked_explicitly() -> None:
    linked = detection(symbol=SILVER)
    result = plan(detection=linked)
    assert result.ok is True
    assert result.draft is not None
    assert result.draft.detection_id == linked.detection_id
    assert result.draft.to_request().link_type is LinkType.EXPLICIT


def test_the_shortcut_has_no_default_lot() -> None:
    """A wrong lot is the one field that costs money, so there is nothing to default."""
    import inspect

    parameters = inspect.signature(plan_market_order).parameters
    assert parameters["lot"].default is inspect.Parameter.empty


def test_the_filling_preference_prefers_fok_over_ioc() -> None:
    both = market_filling_mode(DEFAULT_SYMBOL_INFO)
    assert both.mode is FillingMode.FOK
    assert both.message is None


# ── 9A: reading one symbol out of the observer's published state ──────────────


def test_a_symbols_quote_is_found_by_name_not_by_position() -> None:
    state = SystemState(
        symbols=(
            SymbolState(
                symbol=SYMBOL,
                timeframe=Timeframe.M5,
                market_state=MarketState.OPEN,
                last_quote=quote(),
            ),
            SymbolState(
                symbol=SILVER,
                timeframe=Timeframe.M5,
                market_state=MarketState.CLOSED,
                last_quote=QuoteSnapshot(symbol=SILVER, bid=30.0, ask=30.02, point=0.001),
            ),
        )
    )
    assert quote_of(state, SILVER).bid == pytest.approx(30.0)
    assert market_state_of(state, SILVER) is MarketState.CLOSED
    assert market_state_of(state, SYMBOL) is MarketState.OPEN


def test_an_absent_symbol_yields_no_quote_and_an_unknown_market() -> None:
    """Discord may not call the broker, so "no state" must not become "some price"."""
    assert quote_of(None, SILVER) is None
    assert market_state_of(None, SILVER) is MarketState.UNKNOWN
    empty = SystemState(symbols=())
    assert quote_of(empty, SILVER) is None
    assert market_state_of(empty, SILVER) is MarketState.UNKNOWN


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


# ── 9A: naming a symbol on the other commands ─────────────────────────────────


def open_trade(symbol: str, position_id: int):
    """A real ``Trade``, not a double (9A).

    A stand-in with a ``position_id`` attribute was the first version of this helper, and it
    made ``resolve_close_target`` pass while reading a field no ``Trade`` has -- the broker's
    id lives on ``mt5_position_id``. Against real documents the rule would have reported
    "nothing open" for every symbol. The same class of gap as decisions 145 and 149, so the
    helper builds the document the command actually receives.
    """
    from aureon.models.trade import Trade

    return Trade(
        trade_id=f"t-{position_id}",
        mt5_position_id=position_id,
        symbol=symbol,
        direction=Direction.BUY,
        volume=0.10,
        open_price=2400.0 if symbol == SYMBOL else 30.0,
        open_time=MarketTime.from_utc(NOW - timedelta(minutes=5), TZ),
    )


def test_an_observed_symbol_passes_and_an_unobserved_one_says_what_is_here() -> None:
    assert unobserved_symbol_notice("xagusd", OBSERVED) is None
    notice = unobserved_symbol_notice("EURUSD", OBSERVED)
    assert notice is not None
    assert SYMBOL in notice and SILVER in notice


def test_filtering_by_symbol_is_by_name_and_none_means_everything() -> None:
    """The count on /status is what a trader reads to decide whether they are exposed."""
    items = [open_trade(SYMBOL, 1), open_trade(SILVER, 2), open_trade(SILVER, 3)]
    assert len(for_symbol(items, None)) == 3
    assert [t.mt5_position_id for t in for_symbol(items, "xagusd")] == [2, 3]
    assert for_symbol(items, "EURUSD") == []


def test_no_named_symbol_means_no_cross_check() -> None:
    """The ticket alone still works; it is simply unchecked, which is the status quo."""
    assert check_target_symbol(
        symbol=None, kind=ControlRequestKind.CLOSE, target="900", found_symbol=None
    ).ok


def test_a_named_symbol_that_matches_the_record_passes() -> None:
    assert check_target_symbol(
        symbol="xagusd", kind=ControlRequestKind.CLOSE, target="900", found_symbol=SILVER
    ).ok


def test_a_named_symbol_that_disagrees_with_the_record_refuses() -> None:
    """9A. A mistyped eight-digit ticket points at the other instrument's real order."""
    check = check_target_symbol(
        symbol=SILVER, kind=ControlRequestKind.CLOSE, target="900", found_symbol=SYMBOL
    )
    assert check.ok is False
    assert SYMBOL in (check.message or "") and SILVER in (check.message or "")
    assert "Nothing was sent" in (check.message or "")


def test_a_symbol_aureon_cannot_verify_is_refused_rather_than_assumed() -> None:
    """A guarantee that lapses when the record is missing is not a guarantee.

    The refusal says how to proceed deliberately instead, which keeps the unchecked action
    available without letting it happen by accident.
    """
    check = check_target_symbol(
        symbol=SILVER, kind=ControlRequestKind.CANCEL, target="900", found_symbol=None
    )
    assert check.ok is False
    assert "no record" in (check.message or "")
    assert "without `symbol:`" in (check.message or "")
    # And it calls a cancel's target an order, not a position.
    assert "order" in (check.message or "")


def test_the_close_shortcut_finds_the_one_open_position_for_the_symbol() -> None:
    trades = [open_trade(SYMBOL, 11), open_trade(SILVER, 22)]
    choice = resolve_close_target(trades, "xagusd")
    assert choice.ok is True
    assert choice.position_id == 22
    assert choice.symbol == SILVER


def test_the_close_shortcut_refuses_to_choose_between_two_positions() -> None:
    """9A. Newest, largest or first would all be Aureon ending a trade of its own choosing."""
    trades = [open_trade(SILVER, 22), open_trade(SILVER, 33)]
    choice = resolve_close_target(trades, SILVER)
    assert choice.ok is False
    assert choice.position_id is None
    assert "22" in (choice.message or "") and "33" in (choice.message or "")
    assert "/close-trade" in (choice.message or "")


def test_the_close_shortcut_says_so_when_there_is_nothing_open() -> None:
    choice = resolve_close_target([open_trade(SYMBOL, 11)], SILVER)
    assert choice.ok is False
    assert SILVER in (choice.message or "")


def test_the_shortcut_reads_the_field_a_real_trade_actually_has() -> None:
    """``mt5_position_id``, not ``position_id`` -- see ``open_trade`` above.

    Asserted directly because the failure is silent: reading the wrong attribute makes every
    symbol look flat, which is the most reassuring possible way to be wrong.
    """
    trade = open_trade(SILVER, 44)
    assert not hasattr(trade, "position_id")
    assert resolve_close_target([trade], SILVER).position_id == 44


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


# ── 9A: /status scoped to one symbol ──────────────────────────────────────────


def test_a_scoped_status_screen_names_the_symbol_it_is_about() -> None:
    """Stated, not implied: one symbol's panel beside both symbols' counts would read as
    one picture of one instrument."""
    screen = build_status(
        system_state=SystemState(
            symbols=(
                SymbolState(
                    symbol=SILVER, timeframe=Timeframe.M5, market_state=MarketState.OPEN
                ),
            ),
            updated_at=NOW,
        ),
        heartbeats={},
        settings=settings(),
        symbol="xagusd",
        now=NOW,
    )
    assert screen.symbol == SILVER
    assert [name for name, _ in screen.symbols] == [f"{SILVER} {Timeframe.M5.value}"]

    from aureon.discord.embeds import status_embed

    assert SILVER in status_embed(screen).title


def test_an_unscoped_status_screen_says_nothing_about_a_symbol() -> None:
    screen = build_status(
        system_state=None, heartbeats={}, settings=settings(), now=NOW
    )
    assert screen.symbol is None

    from aureon.discord.embeds import status_embed

    assert "·" not in status_embed(screen).title


# ── 9C: /remind price ─────────────────────────────────────────────────────────


def alert_plan(**overrides):
    from aureon.discord.service import plan_price_alert

    base = dict(
        symbol=SYMBOL,
        level=2450.0,
        side="above",
        requested_by=OWNER,
        quote=quote(),
        observed_symbols=OBSERVED,
        armed_count=0,
    )
    return plan_price_alert(**(base | overrides))


def test_an_alert_above_the_market_is_armed_with_its_distance() -> None:
    plan = alert_plan(level=2450.0)
    assert plan.ok is True
    assert plan.alert is not None
    assert plan.alert.symbol == SYMBOL
    assert plan.alert.side == "above"
    assert plan.alert.requested_by == OWNER
    # The distance is from the price the firing will measure -- the ask, for `above`.
    assert "49.7" in (plan.note or "")
    assert "4970 points" in (plan.note or "")


def test_an_alert_that_has_already_happened_is_refused_with_the_current_price() -> None:
    """9C. `above` a level already below the market would fire on the next quote.

    That is not a reminder, it is an echo — and silently arming it would deliver a useless
    notification seconds later and teach the trader to distrust the channel. The refusal
    carries the current price, which is how somebody notices they typed the wrong side.
    """
    plan = alert_plan(level=2300.0, side="above")
    assert plan.ok is False
    assert "already at 2400.3" in (plan.message or "")
    assert "Did you mean `below`?" in (plan.message or "")

    other = alert_plan(level=2500.0, side="below")
    assert other.ok is False
    assert "Did you mean `above`?" in (other.message or "")


def test_a_level_inside_the_spread_is_refused_from_either_side() -> None:
    """`above` is checked against the ask and `below` against the bid — the same prices the
    firing uses — so a level between them is already reached whichever way you name it.

    With bid 2400.00 and ask 2400.30, a level of 2400.20 is below the ask (so `above` has
    happened) and above the bid (so `below` has happened). Refusing both is the honest
    answer: an alert inside the current spread fires on the next quote either way.
    """
    assert alert_plan(level=2400.20, side="above").ok is False
    assert alert_plan(level=2400.20, side="below").ok is False
    # Just outside it, each side is armable and the other is not.
    assert alert_plan(level=2400.40, side="above").ok is True
    assert alert_plan(level=2400.40, side="below").ok is False
    assert alert_plan(level=2399.90, side="below").ok is True
    assert alert_plan(level=2399.90, side="above").ok is False


def test_an_unobserved_symbol_is_refused_because_nothing_would_check_it() -> None:
    plan = alert_plan(symbol="EURUSD")
    assert plan.ok is False
    assert "not observed" in (plan.message or "")
    assert "never be checked" in (plan.message or "")


def test_an_unknown_side_and_a_nonsense_level_are_refused() -> None:
    assert alert_plan(side="sideways").ok is False
    assert alert_plan(level=0.0).ok is False
    assert alert_plan(level=-1.0).ok is False


def test_the_cap_is_checked_before_a_quote_is_even_needed() -> None:
    """Twenty levels is more than anyone watches; the hundredth makes the channel useless."""
    from aureon.models.alerts import MAX_ARMED_ALERTS_PER_USER

    plan = alert_plan(armed_count=MAX_ARMED_ALERTS_PER_USER)
    assert plan.ok is False
    assert "/remind cancel" in (plan.message or "")


def test_no_published_quote_means_no_alert_rather_than_an_unchecked_one() -> None:
    """Without a quote Aureon cannot tell which side of the market the level is on."""
    plan = alert_plan(quote=None)
    assert plan.ok is False
    assert "No published quote" in (plan.message or "")


def test_a_note_is_carried_and_an_empty_one_is_not_stored() -> None:
    assert alert_plan(note="range high").alert.note == "range high"
    assert alert_plan(note="").alert.note is None


def test_the_listing_puts_armed_alerts_first_with_their_remaining_time() -> None:
    from datetime import timedelta

    from aureon.discord.service import render_alert_list
    from aureon.models.alerts import PriceAlert
    from aureon.models.enums import PriceAlertStatus

    armed = PriceAlert(
        alert_id="al-armed",
        symbol=SYMBOL,
        level=2450.0,
        side="above",
        requested_by=OWNER,
        expires_at=NOW + timedelta(hours=6),
        note="range high",
    )
    fired = PriceAlert(
        alert_id="al-fired",
        symbol=SYMBOL,
        level=2350.0,
        side="below",
        requested_by=OWNER,
        status=PriceAlertStatus.FIRED,
        fired_price=2349.80,
    )
    text = render_alert_list([fired, armed], now=NOW)
    lines = text.splitlines()
    assert "al-armed" in lines[0] and "6.0h left" in lines[0]
    assert "range high" in lines[0]
    assert "al-fired" in lines[1] and "fired at 2349.8" in lines[1]


def test_an_empty_listing_says_how_to_make_one() -> None:
    from aureon.discord.service import render_alert_list

    assert "/remind price" in render_alert_list([])


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
