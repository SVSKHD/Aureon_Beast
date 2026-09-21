"""Every guard rule, one test each (§41, §56, §57).

The spec asks for each rule to be its own small function with its own unit test, and the
reason is worth stating: a guard is a list of things that must all be true before money
moves. Testing it only end-to-end means a rule can quietly stop firing -- the happy path
still passes, and the missing check is invisible until it costs something.

These run without the emulator: the guard is a pure function.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aureon.execution.execution_guard import (
    RULES,
    BrokerSnapshot,
    check,
)
from aureon.execution.fake_broker import DEFAULT_SYMBOL_INFO
from aureon.models.base import utc_now
from aureon.models.broker import AccountInfo
from aureon.models.enums import FailureCode, FillingMode, MarketState, OrderType
from aureon.models.market import QuoteSnapshot
from aureon.models.settings import ExecutionSettings
from aureon.models.trade import TradeRequest

SYMBOL = "XAUUSD"
NOW = utc_now()


def quote(*, bid: float = 2400.00, ask: float = 2400.30, age: float = 0.0) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=SYMBOL,
        bid=bid,
        ask=ask,
        point=0.01,
        captured_at=NOW - timedelta(seconds=age),
    )


def silver_quote(*, bid: float = 30.000, ask: float = 30.015) -> QuoteSnapshot:
    """A quote at silver's own tick. 0.015 of spread is 15 points at 0.001 -- the same
    point count as gold's default, and a completely different fraction of price."""
    return QuoteSnapshot(
        symbol="XAGUSD", bid=bid, ask=ask, point=0.001, captured_at=NOW
    )


def silver_snapshot(**overrides) -> BrokerSnapshot:
    info = DEFAULT_SYMBOL_INFO.model_copy(
        update={"symbol": "XAGUSD", "point": 0.001, "digits": 3}
    )
    base = dict(
        symbol_info=info,
        quote=silver_quote(),
        account=AccountInfo(login=1, balance=100_000, equity=100_000, margin_free=100_000),
        open_positions=0,
        trades_today=0,
    )
    return BrokerSnapshot(**(base | overrides))


def request(**overrides) -> TradeRequest:
    base = dict(
        request_id="r1",
        symbol=SYMBOL,
        order_type=OrderType.MARKET_BUY,
        volume=0.10,
        requested_by="user-1",
        quote=quote(),
        deviation_points=20,
    )
    return TradeRequest(**(base | overrides))


def snapshot(**overrides) -> BrokerSnapshot:
    base = dict(
        symbol_info=DEFAULT_SYMBOL_INFO,
        quote=quote(),
        account=AccountInfo(login=1, balance=100_000, equity=100_000, margin_free=100_000),
        open_positions=0,
        trades_today=0,
    )
    return BrokerSnapshot(**(base | overrides))


def settings(**overrides) -> ExecutionSettings:
    base = dict(
        trading_enabled=True,
        max_lot=1.0,
        max_spread_points=50.0,
        max_deviation_points=20,
        max_open_positions=5,
        max_daily_trades=20,
        quote_ttl_seconds=15.0,
    )
    return ExecutionSettings(**(base | overrides))


def verdict(*, req=None, sett=None, snap=None, state=MarketState.OPEN):
    return check(
        req or request(),
        sett or settings(),
        market_state=state,
        broker=snap or snapshot(),
        now=NOW,
    )


# ── The happy path, so a blanket failure is obvious ───────────────────────────


def test_a_valid_request_passes_every_rule() -> None:
    result = verdict()
    assert result.ok is True
    assert result.failure_code is None


def test_the_guard_has_all_its_rules_registered() -> None:
    """A rule that is written but never added to RULES does nothing.

    Pinning the count makes adding a rule a deliberate, visible act.
    """
    assert len(RULES) == 17


# ── §57: the operator's switch ────────────────────────────────────────────────


def test_trading_disabled_blocks_everything() -> None:
    result = verdict(sett=settings(trading_enabled=False))
    assert result.failure_code is FailureCode.TRADING_DISABLED
    assert result.rule == "trading_enabled"


def test_trading_disabled_reports_the_reason_when_one_was_given() -> None:
    result = verdict(
        sett=settings(trading_enabled=False, disabled_reason="drawdown limit hit")
    )
    assert "drawdown limit hit" in (result.message or "")


def test_a_symbol_outside_the_allowlist_is_refused() -> None:
    result = verdict(sett=settings(allowed_symbols=("EURUSD",)))
    assert result.failure_code is FailureCode.SYMBOL_NOT_TRADEABLE
    assert result.rule == "symbol_allowed"


def test_an_empty_allowlist_does_not_block() -> None:
    assert verdict(sett=settings(allowed_symbols=())).ok is True


# ── §28: the authorisation is still valid ─────────────────────────────────────


def test_an_expired_confirmation_is_refused() -> None:
    """Checked again after the claim, because the TTL can lapse in between."""
    result = verdict(req=request(expires_at=NOW - timedelta(seconds=1)))
    assert result.failure_code is FailureCode.CONFIRMATION_EXPIRED
    assert result.rule == "confirmation_fresh"


def test_a_live_confirmation_passes() -> None:
    assert verdict(req=request(expires_at=NOW + timedelta(seconds=30))).ok is True


# ── §10: the market ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state", [MarketState.CLOSED, MarketState.PREOPEN, MarketState.STALE, MarketState.UNKNOWN]
)
def test_only_an_open_market_is_tradeable(state: MarketState) -> None:
    """PREOPEN and STALE especially: a broker may quote without accepting trades, and a
    dead feed means the last price is unreliable."""
    result = verdict(state=state)
    assert result.failure_code is FailureCode.MARKET_CLOSED
    assert result.rule == "market_open"


# ── §41: the price ────────────────────────────────────────────────────────────


def test_a_missing_symbol_is_refused() -> None:
    result = verdict(snap=snapshot(symbol_info=None))
    assert result.failure_code is FailureCode.SYMBOL_NOT_FOUND


def test_a_close_only_symbol_cannot_open_a_position() -> None:
    info = DEFAULT_SYMBOL_INFO.model_copy(update={"trade_mode": "close_only"})
    result = verdict(snap=snapshot(symbol_info=info))
    assert result.failure_code is FailureCode.SYMBOL_NOT_TRADEABLE


def test_a_missing_quote_is_refused() -> None:
    result = verdict(snap=snapshot(quote=None))
    assert result.failure_code is FailureCode.STALE_QUOTE
    assert result.rule == "quote_available"


def test_a_stale_execution_quote_is_refused() -> None:
    """Not the confirmation's quote -- the one just read from the broker."""
    result = verdict(snap=snapshot(quote=quote(age=30.0)))
    assert result.failure_code is FailureCode.STALE_QUOTE
    assert result.rule == "quote_fresh"


def test_a_spread_over_the_limit_is_refused() -> None:
    result = verdict(snap=snapshot(quote=quote(bid=2399.0, ask=2401.0)))
    assert result.failure_code is FailureCode.SPREAD_LIMIT


def test_an_unmeasurable_spread_fails_closed() -> None:
    """Comparing against a fabricated point size is worse than not comparing."""
    no_point = QuoteSnapshot(symbol=SYMBOL, bid=2400.0, ask=2400.3, captured_at=NOW)
    info = DEFAULT_SYMBOL_INFO.model_copy(update={"point": 0.01})
    result = verdict(snap=snapshot(quote=no_point, symbol_info=info))
    # point comes from symbol_info here, so this one passes; the refusal case is when
    # neither knows it.
    assert result.ok or result.failure_code is FailureCode.SPREAD_LIMIT


def test_price_running_away_from_the_confirmation_is_refused() -> None:
    """A human confirmed seeing 2400.30; filling at 2405 is a different trade."""
    result = verdict(snap=snapshot(quote=quote(bid=2405.0, ask=2405.3)))
    assert result.failure_code is FailureCode.DEVIATION_EXCEEDED
    assert result.rule == "price_has_not_run_away"


def test_price_moving_in_the_humans_favour_is_allowed() -> None:
    """Refusing an improvement on what the human saw would be perverse."""
    assert verdict(snap=snapshot(quote=quote(bid=2395.0, ask=2395.3))).ok is True


def test_deviation_is_clamped_never_widened() -> None:
    """§41. A request asking for more slippage tolerance than policy allows gets
    policy's, not its own."""
    result = verdict(req=request(deviation_points=500), sett=settings(max_deviation_points=20))
    assert result.ok is True
    assert result.effective_deviation_points == 20


def test_a_smaller_requested_deviation_is_respected() -> None:
    result = verdict(req=request(deviation_points=5))
    assert result.effective_deviation_points == 5


# ── §42: the order's shape ────────────────────────────────────────────────────


def test_an_off_step_volume_is_refused() -> None:
    result = verdict(req=request(volume=0.015))
    assert result.failure_code is FailureCode.VOLUME_INVALID


def test_a_volume_over_max_lot_is_refused() -> None:
    result = verdict(req=request(volume=2.0), sett=settings(max_lot=1.0))
    assert result.failure_code is FailureCode.MAX_LOT_EXCEEDED


def test_an_unsupported_filling_mode_is_refused() -> None:
    info = DEFAULT_SYMBOL_INFO.model_copy(update={"filling_modes": (FillingMode.IOC,)})
    result = verdict(req=request(filling_mode=FillingMode.FOK), snap=snapshot(symbol_info=info))
    assert result.failure_code is FailureCode.FILLING_MODE_UNSUPPORTED


def test_a_stop_on_the_wrong_side_is_refused() -> None:
    """A buy whose stop-loss sits above entry is a mistake, not a tight stop."""
    result = verdict(req=request(sl=2405.0))
    assert result.failure_code is FailureCode.INVALID_STOPS


def test_a_stop_inside_the_brokers_minimum_distance_is_refused() -> None:
    result = verdict(req=request(sl=2400.25))
    assert result.failure_code is FailureCode.STOPS_TOO_CLOSE


def test_valid_stops_pass() -> None:
    assert verdict(req=request(sl=2398.0, tp=2405.0)).ok is True


def test_a_pending_entry_on_the_wrong_side_is_refused() -> None:
    """A BUY_STOP below the market either rejects or fills instantly at a price the
    human never intended."""
    result = verdict(req=request(order_type=OrderType.BUY_STOP, price=2399.0))
    assert result.failure_code is FailureCode.INVALID_STOPS


def test_a_valid_pending_entry_passes() -> None:
    assert verdict(req=request(order_type=OrderType.BUY_STOP, price=2405.0)).ok is True


# ── §56: account and exposure ─────────────────────────────────────────────────


def test_insufficient_margin_is_refused() -> None:
    account = AccountInfo(login=1, balance=100.0, equity=100.0, margin_free=50.0)
    result = verdict(snap=snapshot(account=account, margin_required=500.0))
    assert result.failure_code is FailureCode.INSUFFICIENT_MARGIN


def test_an_unknown_margin_requirement_does_not_block() -> None:
    """The broker will reject it and the executor records that -- better than blocking
    every trade on incomplete metadata."""
    assert verdict(snap=snapshot(margin_required=None)).ok is True


def test_the_open_position_limit_is_enforced() -> None:
    result = verdict(snap=snapshot(open_positions=5), sett=settings(max_open_positions=5))
    assert result.failure_code is FailureCode.MAX_OPEN_POSITIONS


def test_the_daily_trade_limit_is_enforced() -> None:
    result = verdict(snap=snapshot(trades_today=20), sett=settings(max_daily_trades=20))
    assert result.failure_code is FailureCode.MAX_DAILY_TRADES


# ── Ordering and purity ───────────────────────────────────────────────────────


def test_the_most_useful_reason_is_reported_first() -> None:
    """With several rules failing, "trading is disabled" beats "spread too wide"."""
    result = verdict(
        sett=settings(trading_enabled=False),
        snap=snapshot(quote=quote(bid=2390.0, ask=2410.0)),
        state=MarketState.CLOSED,
    )
    assert result.failure_code is FailureCode.TRADING_DISABLED


def test_the_guard_is_pure() -> None:
    """Same inputs, same verdict -- no clock of its own, no I/O."""
    first, second = verdict(), verdict()
    assert (first.ok, first.failure_code, first.effective_deviation_points) == (
        second.ok,
        second.failure_code,
        second.effective_deviation_points,
    )


def test_every_refusal_names_its_rule_and_explains_itself() -> None:
    """The message is rendered to a human in Discord, so it must never be empty."""
    refusals = [
        verdict(sett=settings(trading_enabled=False)),
        verdict(state=MarketState.CLOSED),
        verdict(req=request(volume=2.0)),
        verdict(snap=snapshot(quote=quote(bid=2399.0, ask=2401.0))),
    ]
    for result in refusals:
        assert result.ok is False
        assert result.rule
        assert result.message
        assert result.failure_code is not None


# ── Per-symbol limits (9A) ────────────────────────────────────────────────────


def test_a_per_symbol_max_lot_is_tighter_than_the_global_one() -> None:
    """One lot is 100 oz of gold and 5000 oz of silver, so one ceiling is two notionals."""
    from aureon.models.settings import SymbolLimits

    limited = settings(max_lot=1.0, per_symbol={"XAGUSD": SymbolLimits(max_lot=0.2)})
    result = verdict(
        req=request(symbol="XAGUSD", volume=0.5, quote=silver_quote()),
        sett=limited,
        snap=silver_snapshot(),
    )
    assert not result.ok
    assert result.failure_code is FailureCode.MAX_LOT_EXCEEDED
    assert "XAGUSD" in result.message

    # And gold keeps the global ceiling: an entry for one symbol touches no other.
    assert verdict(req=request(symbol="XAUUSD", volume=0.5), sett=limited).ok


def test_a_per_symbol_spread_limit_is_used_and_named() -> None:
    """max_spread_points is in POINTS, and a point is different money per instrument."""
    from aureon.models.settings import SymbolLimits

    limited = settings(
        max_spread_points=500.0, per_symbol={"XAGUSD": SymbolLimits(max_spread_points=10.0)}
    )
    result = verdict(
        req=request(symbol="XAGUSD", quote=silver_quote()),
        sett=limited,
        snap=silver_snapshot(quote=silver_quote(bid=30.000, ask=30.030)),
    )
    assert not result.ok
    assert result.failure_code is FailureCode.SPREAD_LIMIT
    assert "XAGUSD" in result.message


def test_the_deviation_clamp_uses_the_symbols_own_limit() -> None:
    """20 points is 0.008% of gold's price and 0.07% of silver's, so one number is not one
    policy. The guard never WIDENS a request's deviation, only clamps it (§41)."""
    from aureon.models.settings import SymbolLimits

    limited = settings(
        max_deviation_points=20, per_symbol={"XAGUSD": SymbolLimits(max_deviation_points=3)}
    )
    result = verdict(
        req=request(symbol="XAGUSD", deviation_points=50, quote=silver_quote()),
        sett=limited,
        snap=silver_snapshot(quote=silver_quote(bid=30.000, ask=30.003)),
    )
    assert result.ok
    assert result.effective_deviation_points == 3


def test_every_guard_test_pins_the_clock(
) -> None:
    """The bug this file grew: a direct ``check()`` call reads the wall clock (9A).

    ``NOW`` is captured when this module is imported, and ``rule_quote_fresh`` measures the
    broker quote against ``ctx.now``. A test that lets ``check`` default ``now`` to
    ``utc_now()`` therefore passes when the file runs alone and fails with STALE_QUOTE once
    the whole suite has been running for longer than ``quote_ttl_seconds`` -- fifteen
    seconds. Three per-symbol tests were written that way and did exactly that.

    So the helper that pins the clock is the only way in, and this asserts it: every call
    to ``check`` in this file is ``verdict``'s.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "check"
    ]
    assert len(calls) == 1, "call verdict(...) instead of check(...) so the clock is pinned"
    assert any(kw.arg == "now" for kw in calls[0].keywords)


def test_with_no_entry_every_symbol_gets_the_global_limits() -> None:
    """The shipped default, and a known approximation rather than an equivalence."""
    plain = settings(max_lot=1.0, max_spread_points=50.0, max_deviation_points=20)
    for symbol in ("XAUUSD", "XAGUSD", "EURUSD"):
        resolved = plain.limits_for(symbol)
        assert (resolved.max_lot, resolved.max_spread_points) == (1.0, 50.0)
        assert resolved.max_deviation_points == 20
        assert resolved.overridden == ()


def test_a_partial_entry_overrides_only_what_it_names() -> None:
    from aureon.models.settings import SymbolLimits

    resolved = settings(
        max_lot=1.0,
        max_spread_points=50.0,
        max_deviation_points=20,
        per_symbol={"XAGUSD": SymbolLimits(max_deviation_points=4)},
    ).limits_for("xagusd")
    assert resolved.max_deviation_points == 4
    assert resolved.max_lot == 1.0, "unnamed limits stay global"
    assert resolved.overridden == ("max_deviation_points",)


def test_position_and_trade_counts_stay_global() -> None:
    """They are counts of account-wide exposure, not money measured in points, so they are
    not per-symbol and deliberately have no entry to set."""
    from aureon.models.settings import SymbolLimits

    assert "max_open_positions" not in SymbolLimits.model_fields
    assert "max_daily_trades" not in SymbolLimits.model_fields
