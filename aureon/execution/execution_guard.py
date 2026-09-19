"""The last check before money moves (§41, §56, §57).

A **pure function**. Same request, settings, market state and broker snapshot means the
same verdict, with no clock of its own and no I/O -- which is what lets every rule be
unit-tested directly instead of only through a live send.

It runs **after** ``claim`` and **before** ``send_*``. That order is deliberate: a guard
that ran before the claim would be checking conditions that could change before the
order went out, and a guard that ran after the send would be an audit, not a gate.

## Fail closed, always

Every rule's "I cannot tell" answer is a refusal, not a pass. A missing symbol, an
unreadable spread, an absent settings document -- each blocks the trade. The asymmetry
is intentional: a refused trade costs an opportunity, an unchecked one can cost the
account.

## Provenance

``docs/ARCHITECTURE.md`` was not available, so the specific bullet list of §56/§57/§41
could not be read. Every rule below is inferred from the failure codes the spec names
plus the surrounding text, and each is recorded in decision 55. **This list should be
reconciled against the real sections before trading a funded account** -- a guard
missing a bullet is a money-losing bug, and only that document can say whether one is
missing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from aureon.execution.broker_capabilities import (
    check_filling_mode,
    check_pending_entry,
    check_stops,
    check_volume,
)
from aureon.models.base import utc_now
from aureon.models.broker import AccountInfo
from aureon.models.enums import Direction, FailureCode, MarketState
from aureon.models.market import QuoteSnapshot, SymbolInfo
from aureon.models.settings import ExecutionSettings
from aureon.models.trade import TradeRequest


@dataclass(frozen=True)
class BrokerSnapshot:
    """Everything the guard needs from the broker, read once at execution time.

    Gathered up front so the rules stay pure and so every rule sees the *same* prices --
    two rules reading different quotes could contradict each other.
    """

    symbol_info: SymbolInfo | None
    quote: QuoteSnapshot | None
    account: AccountInfo | None = None
    open_positions: int = 0
    trades_today: int = 0
    margin_required: float | None = None


@dataclass(frozen=True)
class GuardResult:
    """The verdict, plus the deviation the executor is permitted to send."""

    ok: bool
    failure_code: FailureCode | None = None
    message: str | None = None
    effective_deviation_points: int = 0
    rule: str | None = None

    @classmethod
    def allow(cls, *, deviation_points: int) -> GuardResult:
        return cls(ok=True, effective_deviation_points=deviation_points)

    @classmethod
    def block(cls, rule: str, code: FailureCode, message: str) -> GuardResult:
        return cls(ok=False, failure_code=code, message=message, rule=rule)


@dataclass(frozen=True)
class GuardContext:
    """The inputs to every rule."""

    request: TradeRequest
    settings: ExecutionSettings
    market_state: MarketState
    broker: BrokerSnapshot
    now: datetime

    @property
    def reference_price(self) -> float:
        """The price this side of the book would transact at, from the FRESH quote."""
        quote = self.broker.quote
        assert quote is not None  # guarded by rule_quote_available
        return quote.price_for(is_buy=self.request.order_type.direction is Direction.BUY)


Rule = Callable[[GuardContext], GuardResult | None]


# ── §57: the operator's switch ────────────────────────────────────────────────


def rule_trading_enabled(ctx: GuardContext) -> GuardResult | None:
    """``/trading disable`` must block the very next attempt (§57).

    Read from Firestore settings rather than config, so it takes effect without a
    redeploy (decision 11).
    """
    if not ctx.settings.trading_enabled:
        return GuardResult.block(
            "trading_enabled",
            FailureCode.TRADING_DISABLED,
            "trading is disabled"
            + (f": {ctx.settings.disabled_reason}" if ctx.settings.disabled_reason else ""),
        )
    return None


def rule_symbol_allowed(ctx: GuardContext) -> GuardResult | None:
    """An explicit symbol allowlist, when one is configured (§56)."""
    if not ctx.settings.symbol_allowed(ctx.request.symbol):
        return GuardResult.block(
            "symbol_allowed",
            FailureCode.SYMBOL_NOT_TRADEABLE,
            f"{ctx.request.symbol} is not in the Aureon allowlist "
            f"{list(ctx.settings.allowed_symbols)}",
        )
    return None


# ── §28: the human's authorisation is still valid ─────────────────────────────


def rule_confirmation_fresh(ctx: GuardContext) -> GuardResult | None:
    """The confirmation must not have expired (§28).

    ``claim`` already fails a stale confirmation, so reaching here means the TTL lapsed
    between claim and guard. Checked again because the window is real and the cost of
    being wrong is a trade the human no longer intends.
    """
    if ctx.request.confirmation_expired(now=ctx.now):
        return GuardResult.block(
            "confirmation_fresh",
            FailureCode.CONFIRMATION_EXPIRED,
            f"confirmation expired at {ctx.request.expires_at}",
        )
    return None


# ── §10: is the market actually open ─────────────────────────────────────────


def rule_market_open(ctx: GuardContext) -> GuardResult | None:
    """Only OPEN is tradeable; PREOPEN, STALE and UNKNOWN all refuse (§10).

    STALE especially: a feed that stopped ticking means the last price is unreliable, so
    sending into it is sending blind.
    """
    if ctx.market_state is not MarketState.OPEN:
        return GuardResult.block(
            "market_open",
            FailureCode.MARKET_CLOSED,
            f"market state is {ctx.market_state.value}, not open",
        )
    return None


# ── §41: the price we are about to trade at ──────────────────────────────────


def rule_symbol_available(ctx: GuardContext) -> GuardResult | None:
    if ctx.broker.symbol_info is None:
        return GuardResult.block(
            "symbol_available",
            FailureCode.SYMBOL_NOT_FOUND,
            f"broker reported no symbol info for {ctx.request.symbol}",
        )
    if not ctx.broker.symbol_info.is_tradeable:
        return GuardResult.block(
            "symbol_available",
            FailureCode.SYMBOL_NOT_TRADEABLE,
            f"{ctx.request.symbol} trade_mode is "
            f"{ctx.broker.symbol_info.trade_mode}, not full",
        )
    return None


def rule_quote_available(ctx: GuardContext) -> GuardResult | None:
    if ctx.broker.quote is None:
        return GuardResult.block(
            "quote_available",
            FailureCode.STALE_QUOTE,
            f"no quote available for {ctx.request.symbol}",
        )
    return None


def rule_quote_fresh(ctx: GuardContext) -> GuardResult | None:
    """The EXECUTION-time quote must be recent (§41).

    This is about the quote just read from the broker, not the one on the confirmation.
    A stale execution-time quote means every price comparison below is against a number
    that is no longer true.
    """
    quote = ctx.broker.quote
    assert quote is not None
    age = quote.age_seconds(now=ctx.now)
    if age > ctx.settings.quote_ttl_seconds:
        return GuardResult.block(
            "quote_fresh",
            FailureCode.STALE_QUOTE,
            f"execution quote is {age:.1f}s old (limit {ctx.settings.quote_ttl_seconds}s)",
        )
    return None


def rule_spread_within_limit(ctx: GuardContext) -> GuardResult | None:
    """Spread must be within ``max_spread_points`` (§41).

    An unknown point size is a refusal, not a pass: comparing against a fabricated
    scale is worse than not comparing at all.
    """
    quote = ctx.broker.quote
    assert quote is not None
    spread = quote.spread_points
    if spread is None:
        return GuardResult.block(
            "spread_within_limit",
            FailureCode.SPREAD_LIMIT,
            f"cannot measure spread for {ctx.request.symbol}: point size unknown",
        )
    if spread > ctx.settings.max_spread_points:
        return GuardResult.block(
            "spread_within_limit",
            FailureCode.SPREAD_LIMIT,
            f"spread {spread:.1f} points exceeds the limit "
            f"{ctx.settings.max_spread_points:.1f}",
        )
    return None


def rule_price_has_not_run_away(ctx: GuardContext) -> GuardResult | None:
    """The market must still be near the price the human confirmed (§41).

    Not the same as the spread rule. A human confirmed a BUY seeing 2400.30; if the ask
    is now 2405 they are being given a materially different trade. ``max_deviation_points``
    bounds how far that is allowed to drift.

    Only adverse movement counts. Refusing a fill that improved on what the human saw
    would be perverse.
    """
    confirmed = ctx.request.quote
    if confirmed is None:
        return None  # nothing to compare against; other rules still apply
    is_buy = ctx.request.order_type.direction is Direction.BUY
    confirmed_price = confirmed.price_for(is_buy=is_buy)
    current = ctx.reference_price
    point = (ctx.broker.symbol_info.point if ctx.broker.symbol_info else None) or (
        confirmed.point or 0.0
    )
    if not point:
        return None  # handled by the spread rule

    adverse = (current - confirmed_price) if is_buy else (confirmed_price - current)
    drift = adverse / point
    if drift > ctx.settings.max_deviation_points:
        return GuardResult.block(
            "price_has_not_run_away",
            FailureCode.DEVIATION_EXCEEDED,
            f"price moved {drift:.1f} points against the confirmed "
            f"{confirmed_price} (limit {ctx.settings.max_deviation_points})",
        )
    return None


# ── §42: will the broker accept the order's shape ────────────────────────────


def rule_volume_valid(ctx: GuardContext) -> GuardResult | None:
    info = ctx.broker.symbol_info
    assert info is not None
    check = check_volume(info, ctx.request.volume)
    if not check.ok:
        return GuardResult.block(
            "volume_valid", check.failure_code or FailureCode.VOLUME_INVALID, check.message or ""
        )
    return None


def rule_volume_within_max_lot(ctx: GuardContext) -> GuardResult | None:
    """Aureon's own ceiling, tighter than the broker's (§56)."""
    if ctx.request.volume > ctx.settings.max_lot:
        return GuardResult.block(
            "volume_within_max_lot",
            FailureCode.MAX_LOT_EXCEEDED,
            f"volume {ctx.request.volume} exceeds max_lot {ctx.settings.max_lot}",
        )
    return None


def rule_filling_mode_supported(ctx: GuardContext) -> GuardResult | None:
    info = ctx.broker.symbol_info
    assert info is not None
    check = check_filling_mode(info, ctx.request.filling_mode)
    if not check.ok:
        return GuardResult.block(
            "filling_mode_supported",
            check.failure_code or FailureCode.FILLING_MODE_UNSUPPORTED,
            check.message or "",
        )
    return None


def rule_stops_valid(ctx: GuardContext) -> GuardResult | None:
    info = ctx.broker.symbol_info
    assert info is not None
    check = check_stops(
        info,
        order_type=ctx.request.order_type,
        price=ctx.request.price,
        sl=ctx.request.sl,
        tp=ctx.request.tp,
        reference_price=ctx.reference_price,
    )
    if not check.ok:
        return GuardResult.block(
            "stops_valid", check.failure_code or FailureCode.INVALID_STOPS, check.message or ""
        )
    return None


def rule_pending_entry_valid(ctx: GuardContext) -> GuardResult | None:
    info = ctx.broker.symbol_info
    assert info is not None
    check = check_pending_entry(
        info,
        order_type=ctx.request.order_type,
        price=ctx.request.price,
        reference_price=ctx.reference_price,
    )
    if not check.ok:
        return GuardResult.block(
            "pending_entry_valid",
            check.failure_code or FailureCode.INVALID_STOPS,
            check.message or "",
        )
    return None


# ── §56: account and exposure ────────────────────────────────────────────────


def rule_margin_sufficient(ctx: GuardContext) -> GuardResult | None:
    """Enough free margin for the order.

    Skipped when the broker does not report a margin requirement -- the broker will
    reject it and the executor records that, which is better than blocking every trade
    on incomplete metadata.
    """
    account, required = ctx.broker.account, ctx.broker.margin_required
    if account is None or required is None:
        return None
    if required > account.margin_free:
        return GuardResult.block(
            "margin_sufficient",
            FailureCode.INSUFFICIENT_MARGIN,
            f"order needs {required:.2f} margin, {account.margin_free:.2f} free",
        )
    return None


def rule_within_open_position_limit(ctx: GuardContext) -> GuardResult | None:
    if ctx.broker.open_positions >= ctx.settings.max_open_positions:
        return GuardResult.block(
            "within_open_position_limit",
            FailureCode.MAX_OPEN_POSITIONS,
            f"{ctx.broker.open_positions} positions already open "
            f"(limit {ctx.settings.max_open_positions})",
        )
    return None


def rule_within_daily_trade_limit(ctx: GuardContext) -> GuardResult | None:
    if ctx.broker.trades_today >= ctx.settings.max_daily_trades:
        return GuardResult.block(
            "within_daily_trade_limit",
            FailureCode.MAX_DAILY_TRADES,
            f"{ctx.broker.trades_today} trades already today "
            f"(limit {ctx.settings.max_daily_trades})",
        )
    return None


# ── The ordered rule list ────────────────────────────────────────────────────

# Order matters for the MESSAGE a human sees, not for safety -- every rule must pass.
# Cheap, unambiguous refusals come first so the reported reason is the most useful one:
# "trading is disabled" is more helpful than "spread too wide" when both are true.
RULES: tuple[Rule, ...] = (
    rule_trading_enabled,
    rule_symbol_allowed,
    rule_confirmation_fresh,
    rule_market_open,
    rule_symbol_available,
    rule_quote_available,
    rule_quote_fresh,
    rule_spread_within_limit,
    rule_price_has_not_run_away,
    rule_volume_valid,
    rule_volume_within_max_lot,
    rule_filling_mode_supported,
    rule_stops_valid,
    rule_pending_entry_valid,
    rule_margin_sufficient,
    rule_within_open_position_limit,
    rule_within_daily_trade_limit,
)


def check(
    request: TradeRequest,
    settings: ExecutionSettings,
    *,
    market_state: MarketState,
    broker: BrokerSnapshot,
    now: datetime | None = None,
) -> GuardResult:
    """Run every rule. The first refusal wins (§56, §57, §41).

    On success, returns the deviation the executor may send: the requested value
    **clamped** to ``max_deviation_points``. The guard never widens it (§41) -- a
    request asking for more slippage tolerance than policy allows gets policy's.
    """
    ctx = GuardContext(
        request=request,
        settings=settings,
        market_state=market_state,
        broker=broker,
        now=now or utc_now(),
    )
    for rule in RULES:
        verdict = rule(ctx)
        if verdict is not None:
            return verdict
    return GuardResult.allow(
        deviation_points=min(request.deviation_points, settings.max_deviation_points)
    )
