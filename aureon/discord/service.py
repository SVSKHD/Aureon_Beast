"""The logic behind every Discord command (§37-§47, §57, §59).

Deliberately contains **no discord.py import**. Every rule that decides whether an
action is allowed, what a screen should say, or what gets written to Firestore lives
here as plain functions over plain data; ``bot.py`` and ``commands/`` are thin adapters
that render the results.

Two reasons, and both matter more than tidiness:

* **Testability.** The rules that protect real money -- who may confirm, whether a quote
  is too old, whether a lot is legal -- are tested directly, without a Discord gateway,
  a fake guild, or an event loop.
* **The boundary.** Discord never calls the broker and never computes an indicator
  (CLAUDE.md). Keeping the logic in one import-light module makes that easy to see and
  easy for the boundary test to enforce.

## What Discord is allowed to write

``trade_requests`` (as ``REQUESTED``), ``control_requests``, ``settings.trading_enabled``
and ``audit_logs``. Nothing else. In particular it never writes a trade, never resolves a
request, and never reports an outcome from its own return value -- outcomes come from the
executor and monitor via Firestore (§46, §47).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aureon.models.alerts import ALERT_SIDES, MAX_ARMED_ALERTS_PER_USER, PriceAlert
from aureon.models.assessment import MIN_COHORT
from aureon.models.base import to_utc, utc_now
from aureon.models.control import ControlRequest
from aureon.models.detection import Detection
from aureon.models.enums import (
    AccountMode,
    ControlRequestKind,
    FillingMode,
    Freshness,
    LinkType,
    MarketState,
    OrderType,
    PriceAlertStatus,
    SleepPhase,
    TradeRequestStatus,
)
from aureon.models.identity import new_alert_id
from aureon.models.market import QuoteSnapshot, SymbolInfo
from aureon.models.settings import ExecutionSettings
from aureon.models.system import DEFAULT_OFFLINE_AFTER_SECONDS, freshness_of
from aureon.models.trade import TradeRequest
from aureon.services.sleep_cycle import DEFAULT_SLEEP_HEARTBEAT_SECONDS

log = logging.getLogger(__name__)

#: What a field with no value renders as, everywhere a human reads one. A blank would be
#: indistinguishable from a zero at a glance, and a zero is a price.
UNKNOWN = "—"

#: Appended to every volume-profile line a human reads (11A, F-5).
#:
#: MT5's M5 candle reports ``tick_volume`` -- the number of price CHANGES in the bar -- and
#: says nothing about contracts traded or about where inside its range they happened. A line
#: saying "POC 2398.00" over a label reading "Volume" invites a reader to interpret it the way
#: a futures trader reads exchange volume at a price, which it is not and cannot be from this
#: data. Two words of honesty are cheaper than the wrong conclusion.
TICK_VOLUME_NOTE = "tick-volume profile (MT5), not exchange traded volume"


# ── Authorization (§71) ───────────────────────────────────────────────────────


class NotAuthorized(PermissionError):
    """The Discord user is not on the allowlist, or not the request's owner."""


def authorize(user_id: str, allowed: tuple[str, ...]) -> None:
    """Raise unless ``user_id`` may command Aureon (§71).

    Fails closed: an **empty allowlist authorises nobody**. Treating empty as "everyone"
    would turn a missing environment variable into an open trading bot.
    """
    if not allowed:
        raise NotAuthorized(
            "no authorized users are configured; set AUREON_AUTHORIZED_USER_IDS"
        )
    if str(user_id) not in allowed:
        raise NotAuthorized(f"user {user_id} is not authorized to command Aureon")


# ── Lot validation (§37, §42) ─────────────────────────────────────────────────


@dataclass(frozen=True)
class LotCheck:
    """Whether a requested lot size is acceptable, and why not if it is not."""

    ok: bool
    message: str | None = None
    normalized: float | None = None


def validate_lot(
    raw: str | float,
    info: SymbolInfo | None,
    settings: ExecutionSettings,
    *,
    symbol: str | None = None,
) -> LotCheck:
    """Validate a lot before the confirmation screen is ever shown (§37).

    Checked here so a human sees "0.15 is not a multiple of 0.01" while they can still
    fix it, rather than an opaque broker rejection after they pressed CONFIRM.

    Advisory, not authoritative: the published symbol copy may be stale, and the
    execution guard re-validates against the live symbol (§56). A missing copy therefore
    does **not** block -- Aureon's own ``max_lot`` still applies, and the guard catches
    the rest.
    """
    try:
        volume = float(raw)
    except (TypeError, ValueError):
        return LotCheck(False, f"`{raw}` is not a number")

    if volume <= 0:
        return LotCheck(False, "lot size must be greater than zero")
    # This SYMBOL's ceiling. One lot is 100 oz of gold and 5000 oz of silver, so a
    # single global number is two different notionals (9A).
    limits = settings.limits_for(symbol or (info.symbol if info else ""))
    if volume > limits.max_lot:
        return LotCheck(
            False,
            f"{volume} exceeds Aureon's maximum lot of {limits.max_lot}"
            + (f" for {limits.symbol}" if limits.overridden else ""),
        )

    if info is None:
        log.warning("no published symbol spec; lot validated against max_lot only")
        return LotCheck(True, None, volume)

    try:
        normalized = info.normalize_volume(volume)
    except ValueError as exc:
        return LotCheck(False, str(exc))
    return LotCheck(True, None, normalized)


def execution_modes(info: SymbolInfo | None) -> tuple[tuple[FillingMode, bool], ...]:
    """Every filling mode with whether this symbol supports it (§39).

    Returns all three rather than only the supported ones, so the UI can *show* FOK as
    unavailable with a reason instead of silently omitting it -- a human who expected FOK
    and cannot find it learns nothing from its absence.
    """
    supported = set(info.filling_modes) if info and info.filling_modes else set()
    return tuple((mode, mode in supported) for mode in FillingMode)


@dataclass
class FillingChoice:
    """Which market filling mode to send, and whether that needed saying out loud (9A).

    FOK is preferred: it either fills the whole volume at the price or nothing happens,
    which is the behaviour a human pressing CONFIRM on one lot is imagining. IOC can fill
    part of it, which is a different trade from the one on the screen.

    So a substitution is never silent. If FOK is unsupported and IOC is, the embed says
    which mode will be used and what that changes; if the symbol reports neither, the
    command refuses rather than falling back to "broker default" -- §39's whole point is
    that the mode is shown, and an unnamed default is not shown.
    """

    mode: FillingMode | None
    message: str | None = None
    ok: bool = True


#: The two sides ``/execute`` accepts, mapped to market order types. A shortcut that
#: accepted "long"/"short"/"b"/"s" would be guessing at intent on the one command that
#: moves money.
MARKET_SIDES: dict[str, OrderType] = {
    "buy": OrderType.MARKET_BUY,
    "sell": OrderType.MARKET_SELL,
}

#: Market filling modes in preference order. RETURN is absent on purpose: it leaves an
#: unfilled remainder resting as an order, which is not a market execution at all.
MARKET_FILLING_PREFERENCE: tuple[FillingMode, ...] = (FillingMode.FOK, FillingMode.IOC)


def market_filling_mode(info: SymbolInfo | None) -> FillingChoice:
    """The mode a market ``/execute`` will send for this symbol."""
    if info is None or not info.filling_modes:
        # No published copy, or a broker that reports nothing. The execution guard
        # re-reads the live symbol either way (§56), so this is advisory -- but it must
        # not claim a mode it has no evidence for.
        return FillingChoice(
            None,
            "No filling mode published for this symbol; the broker's default will be "
            "used and the guard will refuse it if the broker disagrees.",
        )
    for mode in MARKET_FILLING_PREFERENCE:
        if mode in info.filling_modes:
            if mode is FillingMode.FOK:
                return FillingChoice(mode)
            return FillingChoice(
                mode,
                f"FOK is not supported for {info.symbol}; using {mode.value.upper()}, "
                "which may fill only part of the volume.",
            )
    available = ", ".join(m.value.upper() for m in info.filling_modes)
    return FillingChoice(
        None,
        f"{info.symbol} supports no market filling mode (reports: {available}), so a "
        "market order cannot be built for it (§39). Use /execute-trade for a pending "
        "order instead.",
        ok=False,
    )


def unsupported_mode_notice(info: SymbolInfo | None, mode: FillingMode) -> str | None:
    """The §39 message, or ``None`` when the mode is fine."""
    if info is None or not info.filling_modes:
        return None
    if mode in info.filling_modes:
        return None
    available = ", ".join(m.value.upper() for m in info.filling_modes)
    return f"{mode.value.upper()} not supported for {info.symbol} — available: {available}"


# ── Reading the observer's published state (§59, decision 80) ─────────────────


def symbol_state_of(state: Any, symbol: str) -> Any:
    """The ``SymbolState`` for one symbol, or ``None``.

    Lives here rather than in each command module because Discord's only honest source
    of a price is the observer's published state: it may not call the broker (CLAUDE.md),
    so "no state for this symbol" and "a quote Aureon can vouch for" are the same
    question asked twice, and two copies of the answer would eventually disagree.
    """
    if state is None:
        return None
    wanted = symbol.upper()
    for entry in state.symbols:
        if entry.symbol.upper() == wanted:
            return entry
    return None


def quote_of(state: Any, symbol: str) -> QuoteSnapshot | None:
    entry = symbol_state_of(state, symbol)
    return entry.last_quote if entry is not None else None


def market_state_of(state: Any, symbol: str) -> MarketState:
    entry = symbol_state_of(state, symbol)
    return entry.market_state if entry is not None else MarketState.UNKNOWN


# ── Which symbols this deployment is allowed to talk about (9A) ───────────────


def unobserved_symbol_notice(symbol: str, observed: Sequence[str]) -> str | None:
    """``None`` when this process observes ``symbol``, else why it will not act on it.

    Refused in Discord rather than left to the execution allowlist, because the reason is
    different: `allowed_symbols` is a policy about what may be traded, and this is a fact
    about what this process knows. Without an observer for the symbol there is no published
    quote, no symbol spec and no detection history — so every field on the screen would be
    missing or, worse, another symbol's.
    """
    wanted = symbol.upper()
    if wanted in {one.upper() for one in observed}:
        return None
    return (
        f"{wanted} is not observed by this deployment ({', '.join(observed)}), so there is "
        "no quote or symbol spec to show you."
    )


def for_symbol(items: Sequence[Any], symbol: str | None) -> list[Any]:
    """The subset carrying this symbol, or everything when no symbol is named.

    Used by ``/status symbol:`` for the open-trade and pending-request counts. A count that
    silently included the other instrument would be the one number a trader reads to decide
    whether they are exposed.
    """
    if symbol is None:
        return list(items)
    wanted = symbol.upper()
    return [item for item in items if str(getattr(item, "symbol", "")).upper() == wanted]


@dataclass(frozen=True)
class Cadences:
    """How often the Discord process should beat and poll (11B)."""

    heartbeat_seconds: float
    poll_seconds: float


def discord_cadences(
    system_state: Any | None,
    *,
    awake_heartbeat: float,
    awake_poll: float,
    sleep_heartbeat: float,
    sleep_poll: float,
) -> Cadences:
    """Slow down while the observer says the market is shut, speed up when it opens.

    Discord is the one service that does not decide this for itself. It has no feed to
    classify -- it may not call the broker (CLAUDE.md) -- so it follows the phase the
    observer publishes. That also means it follows a *stale* phase if the observer dies
    while asleep, which is the right failure: a Discord process beating every five minutes
    over a weekend that has secretly ended is reported STALE by ``/status`` on its own
    heartbeat age, and speeds back up on the first state write.

    Pure, so the awkward part -- an absent or unparseable state document -- is a unit test
    rather than a live weekend.
    """
    phase = getattr(system_state, "sleep_phase", None)
    if phase in {SleepPhase.ASLEEP, SleepPhase.WAKING}:
        return Cadences(heartbeat_seconds=sleep_heartbeat, poll_seconds=sleep_poll)
    return Cadences(heartbeat_seconds=awake_heartbeat, poll_seconds=awake_poll)


def weekend_notice(system_state: Any | None) -> str | None:
    """Why an order cannot be placed right now, when the services are asleep (11B).

    A **refusal**, where a merely-CLOSED symbol row is only a warning, and the difference is
    the confirmation TTL. A stale feed or a symbol the broker has disabled on a Tuesday may
    clear within the sixty seconds a confirmation lives, so the screen says so and lets the
    human decide -- which is the posture every other gate on that screen takes. A confirmed
    weekly close will not clear for forty-eight hours, so the only thing a confirmation
    could do is expire: it would burn the human's confirm press, write a REQUESTED document
    that becomes an EXPIRED one, and teach them that Aureon's screens do not mean anything.

    Read from the observer's published phase, never computed: Discord may not call the
    broker, and a second opinion about the weekly boundary is a second thing to be wrong.
    """
    phase = getattr(system_state, "sleep_phase", None)
    if phase not in {SleepPhase.ASLEEP, SleepPhase.WAKING}:
        return None
    opens = getattr(system_state, "next_market_open", None)
    when = f" It next opens {to_utc(opens):%a %d %b %H:%M} UTC." if opens else ""
    return (
        f"The market is closed — Aureon is asleep ({phase.value}).{when} A confirmation "
        "lives for a minute, so this would expire rather than execute. `/remind` still "
        "works, and so does `/monitor`."
    )


# ── /execute: the market-order shortcut (9A) ──────────────────────────────────


@dataclass
class MarketOrderPlan:
    """Whether a ``/execute`` may proceed, and with what.

    A plan rather than a sequence of early returns inside the command, so every rule that
    can refuse a market order is testable without a Discord interaction -- which is how
    the rest of this module is arranged and why those rules are pinned rather than
    inspected.
    """

    ok: bool
    message: str | None = None
    draft: DraftRequest | None = None
    #: Said on the confirmation screen when the filling mode is not FOK, or unknown.
    filling_note: str | None = None


def plan_market_order(
    *,
    symbol: str,
    side: str,
    lot: str | float,
    requested_by: str,
    settings: ExecutionSettings,
    info: SymbolInfo | None,
    observed_symbols: Sequence[str],
    detection: Detection | None = None,
    system_state: Any | None = None,
) -> MarketOrderPlan:
    """Every rule ``/execute`` applies before a confirmation screen exists.

    In this order, because each one makes the next one meaningful: the side has to parse,
    the symbol has to be one this deployment observes (otherwise there is no quote, no
    spec and no detection history to put on the screen), the market has to be open, the lot
    has to be legal for that symbol, the symbol has to support a market filling mode at all
    (§39), and a named detection has to be for the same symbol -- a link that misdescribes
    what was acted on poisons every review built on it (§50).
    """
    symbol = symbol.upper()
    kind = MARKET_SIDES.get(str(side).lower())
    if kind is None:
        return MarketOrderPlan(
            False, f"`{side}` is not a side — use {' or '.join(MARKET_SIDES)}"
        )

    unobserved = unobserved_symbol_notice(symbol, observed_symbols)
    if unobserved:
        return MarketOrderPlan(False, unobserved)

    shut = weekend_notice(system_state)
    if shut:
        return MarketOrderPlan(False, shut)

    check = validate_lot(lot, info, settings, symbol=symbol)
    if not check.ok:
        return MarketOrderPlan(False, check.message or "invalid lot size")

    filling = market_filling_mode(info)
    if not filling.ok:
        return MarketOrderPlan(False, filling.message or "no market filling mode")

    if detection is not None and detection.symbol.upper() != symbol:
        return MarketOrderPlan(
            False,
            f"detection `{detection.detection_id}` is for {detection.symbol}, not "
            f"{symbol}. A link that misdescribes what was acted on poisons every review "
            "built on it.",
        )

    limits = settings.limits_for(symbol)
    return MarketOrderPlan(
        True,
        draft=DraftRequest(
            symbol=symbol,
            order_type=kind,
            volume=check.normalized if check.normalized is not None else float(lot),
            requested_by=requested_by,
            filling_mode=filling.mode,
            deviation_points=limits.max_deviation_points,
            detection_id=detection.detection_id if detection else None,
        ),
        filling_note=filling.message,
    )


# ── Detection linking (§37, §50) ──────────────────────────────────────────────


def linkable_detections(
    detections: list[Detection],
    *,
    symbol: str,
    window_minutes: int,
    now: datetime | None = None,
    limit: int = 10,
) -> list[Detection]:
    """The last few detections a human could plausibly be acting on (§37).

    Filtered to the symbol and a recent window, newest first. The window exists because a
    detection from six hours ago is not what someone is reacting to now, and offering it
    invites a link that would mislead every review built on it.
    """
    moment = to_utc(now or utc_now())
    cutoff = moment - timedelta(minutes=window_minutes)
    candidates = [
        d
        for d in detections
        if d.symbol == symbol and cutoff <= d.detected_at.utc <= moment
    ]
    candidates.sort(key=lambda d: d.detected_at.utc, reverse=True)
    return candidates[:limit]


# ── Building the request (§37, §40) ───────────────────────────────────────────


@dataclass
class DraftRequest:
    """A trade the human has specified but not yet confirmed."""

    symbol: str
    order_type: OrderType
    volume: float
    requested_by: str
    price: float | None = None
    sl: float | None = None
    tp: float | None = None
    filling_mode: FillingMode | None = None
    deviation_points: int = 20
    detection_id: str | None = None

    def to_request(self, *, request_id: str | None = None) -> TradeRequest:
        """Materialise it as a ``REQUESTED`` trade request.

        ``link_type`` is always ``EXPLICIT`` when a detection is attached: a human chose
        it from a list. Inferred links are Phase 7's business and never touch a request
        (decision 8).
        """
        return TradeRequest(
            request_id=request_id or f"req-{uuid.uuid4().hex[:16]}",
            status=TradeRequestStatus.REQUESTED,
            symbol=self.symbol,
            order_type=self.order_type,
            volume=self.volume,
            price=self.price,
            sl=self.sl,
            tp=self.tp,
            deviation_points=self.deviation_points,
            filling_mode=self.filling_mode,
            requested_by=self.requested_by,
            detection_id=self.detection_id,
            link_type=LinkType.EXPLICIT if self.detection_id else None,
        )


@dataclass
class ConfirmationScreen:
    """Everything §40 requires on the confirmation embed, as data.

    Assembled here rather than in the view so the *content* is testable without rendering
    a Discord embed -- and so a missing field is a failing test rather than something a
    human notices at the worst moment.
    """

    title: str
    fields: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Context, not caution (9D). Kept apart from ``warnings`` because a warning colours
    #: the embed amber and reads as "something is wrong"; "the last readout said X" is
    #: neither a problem nor an endorsement, and putting it in the warning list would make
    #: every informed order look like a risky one.
    info: list[str] = field(default_factory=list)
    quote_age_seconds: float = 0.0
    expires_in_seconds: float = 0.0

    def field_names(self) -> list[str]:
        return [name for name, _ in self.fields]


def build_confirmation(
    draft: DraftRequest,
    quote: QuoteSnapshot,
    info: SymbolInfo | None,
    settings: ExecutionSettings,
    *,
    market_state: MarketState,
    detection: Detection | None = None,
    now: datetime | None = None,
) -> ConfirmationScreen:
    """Assemble the confirmation screen (§40).

    Everything a human needs to decide, including the things that will *stop* the trade:
    a closed market and a too-wide spread appear as warnings here rather than as a
    surprise rejection after they press CONFIRM.
    """
    moment = to_utc(now or utc_now())
    is_buy = draft.order_type.direction.value == "buy"
    entry = draft.price if draft.price is not None else quote.price_for(is_buy=is_buy)
    # This symbol's limits, not the globals: two of the three are in points, and a point
    # is different money on every instrument (9A).
    limits = settings.limits_for(draft.symbol)

    screen = ConfirmationScreen(
        title=f"Confirm {draft.order_type.value.replace('_', ' ').upper()} {draft.symbol}",
        quote_age_seconds=quote.age_seconds(now=moment),
        expires_in_seconds=settings.confirmation_ttl_seconds,
    )
    screen.fields = [
        ("Symbol", draft.symbol),
        ("Order type", draft.order_type.value),
        ("Direction", draft.order_type.direction.value.upper()),
        ("Volume", f"{draft.volume:g} lots"),
        ("Entry", f"{entry:g}" + ("" if draft.price is not None else " (market)")),
        ("Stop loss", f"{draft.sl:g}" if draft.sl is not None else "none"),
        ("Take profit", f"{draft.tp:g}" if draft.tp is not None else "none"),
        (
            "Execution mode",
            draft.filling_mode.value.upper() if draft.filling_mode else "broker default",
        ),
        (
            "Max deviation",
            f"{min(draft.deviation_points, limits.max_deviation_points)} points",
        ),
        ("Bid / Ask", f"{quote.bid:g} / {quote.ask:g}"),
        (
            "Spread",
            f"{quote.spread_points:.1f} points" if quote.spread_points is not None else "unknown",
        ),
        ("Quote age", f"{screen.quote_age_seconds:.1f}s"),
        ("Market state", market_state.value),
        ("Linked detection", detection.event_key if detection else "none"),
        (
            "Limits",
            f"{limits.symbol}-specific ({', '.join(limits.overridden)})"
            if limits.overridden
            else "global",
        ),
        ("Requested by", draft.requested_by),
        ("Confirmation expires in", f"{settings.confirmation_ttl_seconds:g}s"),
    ]

    # Warnings, not blocks: the human decides, and the guard still refuses at execution
    # time. Telling them now is the difference between an informed choice and a surprise.
    if market_state is not MarketState.OPEN:
        screen.warnings.append(
            f"Market is {market_state.value} — this will be refused at execution."
        )
    if quote.spread_points is not None and quote.spread_points > limits.max_spread_points:
        screen.warnings.append(
            f"Spread {quote.spread_points:.1f} exceeds the "
            f"{limits.max_spread_points:g} point limit — this will be refused."
        )
    if not settings.trading_enabled:
        screen.warnings.append("Trading is DISABLED — this will be refused at execution.")
    notice = unsupported_mode_notice(info, draft.filling_mode) if draft.filling_mode else None
    if notice:
        screen.warnings.append(notice)
    return screen


# ── Confirming (§27, §28) ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConfirmGate:
    """Whether a CONFIRM press may proceed."""

    ok: bool
    reason: str | None = None
    needs_refresh: bool = False


def check_confirm_press(
    request: TradeRequest,
    pressed_by: str,
    quote: QuoteSnapshot | None,
    settings: ExecutionSettings,
    *,
    now: datetime | None = None,
) -> ConfirmGate:
    """Decide whether this press may confirm this request (§27, §28).

    Three gates, each protecting something different:

    * **owner** -- only the requester may confirm. Anyone else pressing the button would
      be authorising someone else's money (§27). Discord messages are visible to a
      channel, so this is not hypothetical.
    * **state** -- an already-CONFIRMED request needs no second press, and anything
      further along cannot be confirmed at all.
    * **quote age** -- a stale quote means the human is looking at a price that no longer
      exists. That is a **re-prompt**, not a refusal: they still want the trade, they just
      need current numbers (§27).
    """
    moment = to_utc(now or utc_now())

    if str(pressed_by) != str(request.requested_by):
        return ConfirmGate(
            False,
            f"only {request.requested_by} may confirm this request",
        )

    if request.status is TradeRequestStatus.CONFIRMED:
        # A duplicate click. Harmless -- repository.confirm() is a no-op -- but say so
        # rather than implying a second authorisation happened.
        return ConfirmGate(False, "this request is already confirmed")
    if request.status is not TradeRequestStatus.REQUESTED:
        return ConfirmGate(
            False, f"this request is {request.status.value} and can no longer be confirmed"
        )

    if quote is None:
        return ConfirmGate(False, "no quote available", needs_refresh=True)
    age = quote.age_seconds(now=moment)
    if age > settings.quote_ttl_seconds:
        return ConfirmGate(
            False,
            f"the quote is {age:.1f}s old (limit {settings.quote_ttl_seconds:g}s) — "
            "refreshing, please confirm again",
            needs_refresh=True,
        )
    return ConfirmGate(True)


# ── /remind: price alerts (9C) ────────────────────────────────────────────────


@dataclass
class AlertPlan:
    """Whether a ``/remind price`` may be armed, and with what."""

    ok: bool
    message: str | None = None
    alert: PriceAlert | None = None
    #: Said on the confirmation, e.g. how far the level is from here.
    note: str | None = None


def plan_price_alert(
    *,
    symbol: str,
    level: float,
    side: str,
    requested_by: str,
    quote: QuoteSnapshot | None,
    observed_symbols: Sequence[str],
    armed_count: int,
    note: str | None = None,
    max_per_user: int = MAX_ARMED_ALERTS_PER_USER,
    now: datetime | None = None,
) -> AlertPlan:
    """Every rule ``/remind price`` applies, in the order that makes each one meaningful.

    The one worth explaining is the **side check**. An alert to be told when gold goes
    *above* a level that is already below the current price has already happened: it would
    fire on the very next quote, which is not a reminder, it is an echo. Refusing it with the
    current price in the message is how a human notices they typed `above` for `below` --
    silently arming it would deliver a useless notification seconds later and teach them to
    distrust the channel.
    """
    symbol = symbol.upper()
    unobserved = unobserved_symbol_notice(symbol, observed_symbols)
    if unobserved:
        return AlertPlan(
            False,
            f"{unobserved} A level on a symbol nobody is watching would never be checked.",
        )
    if side not in ALERT_SIDES:
        return AlertPlan(False, f"`{side}` is not a side — use {' or '.join(ALERT_SIDES)}")
    if level <= 0:
        return AlertPlan(False, f"{level} is not a price")
    if armed_count >= max_per_user:
        return AlertPlan(
            False,
            f"You already have {armed_count} armed alerts (the limit is {max_per_user}). "
            "`/remind list` shows them; `/remind cancel` frees one.",
        )
    if quote is None:
        return AlertPlan(
            False,
            f"No published quote for {symbol}, so Aureon cannot tell which side of the "
            "market this level is on. The observer may be stopped — check `/status`.",
        )

    # The price this side would be measured against, so the check and the firing agree.
    reference = quote.ask if side == "above" else quote.bid
    if side == "above" and level <= reference:
        return AlertPlan(
            False,
            f"{symbol} is already at {reference:g}, which is at or above {level:g} — this "
            "would fire on the next quote. Did you mean `below`?",
        )
    if side == "below" and level >= reference:
        return AlertPlan(
            False,
            f"{symbol} is already at {reference:g}, which is at or below {level:g} — this "
            "would fire on the next quote. Did you mean `above`?",
        )

    alert = PriceAlert(
        alert_id=new_alert_id(),
        symbol=symbol,
        level=level,
        side=side,
        requested_by=str(requested_by),
        note=(note or None),
    )
    distance = abs(level - reference)
    points = distance / quote.point if quote.point else None
    _ = now
    return AlertPlan(
        True,
        alert=alert,
        note=(
            f"{distance:g} away from {reference:g}"
            + (f" ({points:.0f} points)" if points is not None else "")
        ),
    )


def render_alert_list(alerts: Sequence[PriceAlert], *, now: datetime | None = None) -> str:
    """``/remind list`` for one user: armed first, then whatever has been answered.

    Armed alerts carry their remaining time, because the useful question about an armed
    alert is when it stops watching. A fired one carries the price that answered it, which
    is the only part of a fired alert anybody re-reads.
    """
    if not alerts:
        return "No alerts. `/remind price` arms one."
    moment = to_utc(now or utc_now())
    lines = []
    for alert in sorted(
        alerts, key=lambda a: (a.status is not PriceAlertStatus.ARMED, a.symbol, a.level)
    ):
        head = f"`{alert.alert_id}` {alert.symbol} {alert.side} {alert.level:g}"
        if alert.status is PriceAlertStatus.ARMED and alert.expires_at is not None:
            remaining = (to_utc(alert.expires_at) - moment).total_seconds() / 3600
            tail = f"armed, {remaining:.1f}h left" if remaining > 0 else "armed, expiring"
        elif alert.status is PriceAlertStatus.FIRED:
            tail = f"fired at {alert.fired_price:g}" if alert.fired_price else "fired"
        else:
            tail = alert.status.value
        note = f" — {alert.note}" if alert.note else ""
        lines.append(f"{head} · {tail}{note}")
    return "\n".join(lines)


# ── Control requests (§46, §47) ───────────────────────────────────────────────


def build_control_request(
    kind: ControlRequestKind,
    target: str,
    requested_by: str,
    *,
    symbol: str | None = None,
    volume: float | None = None,
) -> ControlRequest:
    """A cancel or close, for the executor to perform (§46, §47).

    Discord writes this and stops. It does **not** call the broker, and it does not report
    an outcome from its own return value: the executor performs it under the same
    claim/lease discipline as a trade, and the monitor records what actually happened.
    Reporting optimistically here is how "cancelled" gets shown for an order that had
    already filled.
    """
    return ControlRequest(
        control_id=f"ctl-{uuid.uuid4().hex[:16]}",
        kind=kind,
        target=str(target),
        symbol=symbol,
        volume=volume,
        requested_by=str(requested_by),
    )


# ── Naming a symbol on a cancel or a close (9A, §46, §47) ─────────────────────


@dataclass
class TargetCheck:
    """Whether a ticket or position id may be acted on under the named symbol."""

    ok: bool
    message: str | None = None


def check_target_symbol(
    *, symbol: str | None, kind: ControlRequestKind, target: str, found_symbol: str | None
) -> TargetCheck:
    """Refuse a cancel or close whose target is not the symbol the human named.

    With one symbol observed, ``symbol:`` on these commands would be decoration. With two it
    is a **cross-check on a number nobody can read**: a broker ticket is eight digits and a
    mistyped one on a two-symbol account points at a real order belonging to the other
    instrument. Naming the symbol turns a typo from "closed the wrong position" into a
    refusal.

    For that to hold, an unverifiable claim has to be refused as well. If Aureon holds no
    record tying this target to a symbol, it cannot confirm the human is acting on what they
    think they are, and proceeding would let the guarantee fail silently exactly when the
    record is missing. Acting on the raw ticket is still available by omitting ``symbol:``,
    which is then plainly an unchecked action rather than a checked one.
    """
    if symbol is None:
        return TargetCheck(True)
    wanted = symbol.upper()
    noun = "order" if kind is ControlRequestKind.CANCEL else "position"
    if found_symbol is None:
        return TargetCheck(
            False,
            f"Aureon has no record tying {noun} `{target}` to a symbol, so it cannot "
            f"confirm it is {wanted}. Re-run without `symbol:` to act on the {noun} "
            "itself — that is an unchecked action, and worth choosing deliberately.",
        )
    if found_symbol.upper() != wanted:
        return TargetCheck(
            False,
            f"{noun.title()} `{target}` is {found_symbol}, not {wanted}. Nothing was sent.",
        )
    return TargetCheck(True)


@dataclass
class CloseChoice:
    """Which position ``/close symbol:`` will close, if it can tell."""

    ok: bool
    message: str | None = None
    position_id: int | None = None
    symbol: str | None = None


def resolve_close_target(open_trades: Sequence[Any], symbol: str) -> CloseChoice:
    """The one open position for this symbol, or a refusal that says what it found.

    ``/close symbol:XAGUSD`` is for the common case — one position, close it now, no ids to
    copy. Two positions on the same symbol is **not** that case, and picking the newest, the
    largest or the first would be Aureon deciding which of a human's trades to end. It lists
    them instead and asks for `/close-trade`.
    """
    wanted = symbol.upper()
    # ``mt5_position_id`` is the field name on Trade: the broker's position id, kept under a
    # name that says whose id it is (§49). Reading a plain ``position_id`` here would find
    # nothing on any real trade and quietly report "nothing open".
    matches = [
        trade
        for trade in open_trades
        if str(getattr(trade, "symbol", "")).upper() == wanted
        and getattr(trade, "mt5_position_id", None)
    ]
    if not matches:
        return CloseChoice(
            False,
            f"No open {wanted} position. `/status symbol:{wanted}` shows what Aureon knows.",
        )
    if len(matches) > 1:
        listed = ", ".join(f"`{trade.mt5_position_id}`" for trade in matches)
        return CloseChoice(
            False,
            f"{len(matches)} open {wanted} positions: {listed}. Use "
            "`/close-trade position_id:` — Aureon will not choose which of your trades to "
            "close.",
        )
    only = matches[0]
    return CloseChoice(True, position_id=int(only.mt5_position_id), symbol=only.symbol)


# ── 9C: what a detection notification says ────────────────────────────────────


@dataclass
class NotificationScreen:
    """One detection, rendered for a channel (9C).

    Built here rather than in ``embeds.py`` for the reason everything else is: the content
    is a decision about what a human needs to see, and it is tested without a Discord
    client. ``embeds.py`` only turns it into an embed.
    """

    title: str
    symbol: str
    detection_id: str
    fields: list[tuple[str, str]] = field(default_factory=list)
    footer: str = ""
    #: Prefills the Execute button. The lot is deliberately absent (9C).
    side: str | None = None


#: What the footer says on every detection embed. Not decoration: an embed with a direction
#: and a price looks like a recommendation, and the only thing separating the two is a
#: sentence saying which it is.
RESEARCH_ONLY = "research only · not a recommendation"


def build_notification(detection: Detection) -> NotificationScreen:
    """The §59 detection embed: what the machine saw, and nothing it did not (9C).

    Every line comes from the **stored detection** — its own indicator snapshot, its own
    session, and 9B's volume and volatility context. Discord computes nothing (CLAUDE.md),
    and a field it cannot fill reads "—" rather than being dropped, so two embeds of the
    same agent always have the same shape.
    """
    direction = detection.direction.value if detection.direction else "context"
    screen = NotificationScreen(
        title=f"{detection.symbol} · {detection.agent_name} · {direction}",
        symbol=detection.symbol,
        detection_id=detection.detection_id,
        side=detection.direction.value if detection.direction else None,
        footer=f"{RESEARCH_ONLY} · {TICK_VOLUME_NOTE} · {detection.detection_id}",
    )

    ema = detection.indicators.ema or {}
    fast, slow = ema.get("fast"), ema.get("slow")
    relation = UNKNOWN
    if isinstance(fast, (int, float)) and isinstance(slow, (int, float)):
        relation = "fast above slow" if fast >= slow else "fast below slow"

    screen.fields = [
        ("Price", _fmt(detection.price)),
        ("Event", detection.event_key),
        ("EMA", f"{_fmt(fast)} / {_fmt(slow)} — {relation}"),
        (
            "RSI",
            f"{_fmt(detection.indicators.rsi, digits=1)} "
            f"{_rsi_zone(detection.indicators.rsi)}",
        ),
        (
            "Session",
            f"{detection.session.session.value}"
            + (f" · {detection.session.trend}" if getattr(detection.session, "trend", None)
               else ""),
        ),
        ("Tick volume", _volume_line(detection)),
        ("Volatility", _volatility_line(detection)),
    ]
    wick = _wick_line(detection)
    if wick:
        screen.fields.append(("Wick", wick))
    return screen


def _rsi_zone(rsi: float | None) -> str:
    """Label a stored reading with the CURRENT definition of the zones.

    Detections store the value and not the label on purpose (``IndicatorSnapshot``), so
    the label has to be applied when the embed is built. It comes from the agent's own
    function and the agent's own boundaries -- a 70/30 written out here would eventually
    disagree with what the agent called it, and the embed is where a human would read the
    disagreement without any way to notice it.

    This is labelling, not computing: nothing here reads a candle (CLAUDE.md).
    """
    if rsi is None:
        return UNKNOWN
    from aureon.agents.rsi_agent import DEFAULT_OVERBOUGHT, DEFAULT_OVERSOLD, rsi_zone

    return (
        rsi_zone(rsi, overbought=DEFAULT_OVERBOUGHT, oversold=DEFAULT_OVERSOLD) or UNKNOWN
    )


def _volume_line(detection: Detection) -> str:
    """Where price stood in Asia's value area, and the nodes nearest it (9B, 9C)."""
    ref = detection.volume_profile_ref
    if ref is None:
        return f"{UNKNOWN} (no profile yet)"
    parts = [f"{ref.price_vs_va or UNKNOWN} VA"]
    if ref.va_low is not None and ref.va_high is not None:
        parts.append(f"({_fmt(ref.va_low)}–{_fmt(ref.va_high)})")
    parts.append(f"POC {_fmt(ref.poc_price)}")
    parts.append(f"LVN {_fmt(ref.nearest_lvn)}")
    parts.append(f"HVN {_fmt(ref.nearest_hvn)}")
    return " ".join(parts)


def _volatility_line(detection: Detection) -> str:
    context = detection.volatility
    if context is None:
        return UNKNOWN
    regime = context.regime or UNKNOWN
    atr = _fmt(context.atr_14)
    ratio = context.session_range_vs_median
    tail = f" ({ratio:.2f}× median)" if ratio is not None else ""
    return f"{regime} · ATR14 {atr}{tail}"


def _wick_line(detection: Detection) -> str | None:
    """The wick agent's own classification, when this detection is one (9C)."""
    if detection.agent_name != "wick":
        return None
    return detection.event_key


def should_notify(detection: Detection, settings: Any) -> bool:
    """Whether this detection is announced at all (9C).

    The decision is the settings document's, read fresh: silencing a noisy agent at 02:00
    must take effect on the next detection rather than on the next deploy.
    """
    return bool(settings.announces(detection.agent_name))


# ── 9D: the measured assessment ───────────────────────────────────────────────


#: What every estimate is labelled with. Not decoration: a target and a stop rendered as
#: prices look exactly like a recommendation, and the only thing separating the two is a
#: sentence saying how they were arrived at.
MEASURED_ONLY = "measured from n={n} prior detections · not advice"


@dataclass
class MonitorScreen:
    """A `/monitor` readout, rendered for one human (9D)."""

    title: str
    symbol: str
    detection_id: str
    assessment_id: str
    bias: str
    evidence: list[str] = field(default_factory=list)
    cohort: str = ""
    dropped: str | None = None
    fields: list[tuple[str, str]] = field(default_factory=list)
    next_move: str = ""
    disagreement: str | None = None
    insufficient: str | None = None
    #: Where the cohort's history came from, and whether it spans enough verified days
    #: (11C, F-9). A line, not a badge: the difference between a rate measured on replayed
    #: fixture bars and one measured on bars a broker served is the difference between a
    #: statement about `gen_fixtures.py` and a statement about the market.
    history: str | None = None
    footer: str = ""


def _pct(value: float | None) -> str:
    return UNKNOWN if value is None else f"{value * 100:.0f}%"


def _interval(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return ""
    return f" [CI {_pct(low)}–{_pct(high)}]"


def _cohort_line(assessment: Any) -> str:
    """Which prior detections were counted, in words a human can check."""
    wanted = assessment.cohort_filter
    parts = [
        f"{wanted.symbol} {wanted.agent_name} {wanted.direction.value}",
        f"{wanted.session.value if wanted.session else UNKNOWN} session",
    ]
    if wanted.trend_aligned is not None:
        parts.append("trend-aligned" if wanted.trend_aligned else "against the trend")
    if wanted.volatility_regime:
        parts.append(f"{wanted.volatility_regime} volatility")
    if wanted.price_vs_va:
        parts.append(f"{wanted.price_vs_va} the value area")
    if wanted.wick_tag:
        parts.append(wanted.wick_tag.replace("has_", "").replace("_", " "))
    return f"n={assessment.n} · " + ", ".join(parts)


def history_line(assessment: Any) -> str | None:
    """One line about where this readout's history came from (11C, F-9).

    ``None`` only for a wholly real cohort spanning enough verified days -- the case that
    needs no caveat. Everything else gets one, and the wording separates the three states
    that are easy to conflate:

    * **synthetic** -- replayed fixture bars. A generated random walk has the distribution
      its generator was given, so every rate on the screen is a statement about
      `scripts/gen_fixtures.py`.
    * **mixed** -- some of each, which is the worst of the three to read silently: the rate
      is a weighted average of a real frequency and a generated one and nothing about it
      looks unusual.
    * **immature** -- real, but spanning fewer than ``MATURE_REAL_DAYS`` verified days. Two
      hundred detections from one Tuesday are not two hundred independent observations.

    ``UNKNOWN`` says so plainly rather than guessing. An assessment stored before this field
    existed has no provenance recorded, and "we do not know" is the honest rendering.
    """
    from aureon.models.assessment import MATURE_REAL_DAYS
    from aureon.models.enums import HistorySource

    source = getattr(assessment, "history_source", HistorySource.UNKNOWN)
    days = int(getattr(assessment, "real_days", 0) or 0)

    if source is HistorySource.SYNTHETIC:
        return (
            "history: SYNTHETIC — every rate here is measured on replayed fixture bars, "
            "so it describes the generator, not the market"
        )
    if source is HistorySource.MIXED:
        return (
            f"history: MIXED — {days} verified broker day(s) plus generated bars. Each "
            "rate is a weighted average of a real frequency and a synthetic one"
        )
    if source is HistorySource.UNKNOWN:
        return "history: not recorded for this readout — treat the rates as unverified"
    if days < MATURE_REAL_DAYS:
        return (
            f"history: {days} verified broker day(s), fewer than {MATURE_REAL_DAYS} — "
            "immature. A single quiet session moves every number above"
        )
    return None


def build_monitor(assessment: Any, detection: Detection) -> MonitorScreen:
    """The §62 readout: a trend read, a cohort, and what followed it before (9D).

    Every percentage on this screen is a measured frequency from stored outcomes and every
    price a quantile of measured excursions. Nothing here is a forecast, and the footer says
    so on every render rather than once in a pinned message nobody scrolls back to.

    The n and the interval are rendered beside each rate, not underneath: "60%" from five
    detections and "60%" from three hundred are the same four characters, and a reader
    deciding in ten seconds will read the ones that are adjacent.
    """
    direction = detection.direction.value if detection.direction else "context"
    screen = MonitorScreen(
        title=f"{detection.symbol} · {detection.agent_name} · {direction}",
        symbol=detection.symbol,
        detection_id=detection.detection_id,
        assessment_id=assessment.assessment_id,
        bias=assessment.trend_read.bias.value,
        evidence=list(assessment.trend_read.evidence),
        cohort=_cohort_line(assessment),
        footer=MEASURED_ONLY.format(n=assessment.n) + f" · {assessment.assessment_id}",
    )

    if assessment.cohort_filter.dropped:
        # Reported, never silent. A cohort that quietly stopped matching on volatility is
        # answering a different question while wearing the same words.
        given_up = ", ".join(
            name.replace("_", " ") for name in assessment.cohort_filter.dropped
        )
        screen.dropped = f"widened by dropping: {given_up}"

    screen.history = history_line(assessment)

    if assessment.disagrees_with_detection:
        screen.disagreement = (
            f"The trend read is {assessment.trend_read.bias.value} and this detection is "
            f"{direction}. They disagree — that is reported, not resolved."
        )

    if assessment.insufficient:
        # Stop here. A percentage from eleven detections is worse than no percentage,
        # because it is read as one from three hundred.
        screen.insufficient = (
            f"insufficient history (n={assessment.n}, need "
            f"{MIN_COHORT}) — no rates, no estimates"
        )
        screen.next_move = f"Bias {screen.bias} · cohort n={assessment.n} · not enough history"
        screen.footer = f"no measurement · {assessment.assessment_id}"
        return screen

    for horizon in assessment.confirmations:
        rows = []
        for row in horizon.thresholds:
            rows.append(
                f"{row.threshold:g}: {_pct(row.rate)} ({row.reached}/{row.evaluated})"
                f"{_interval(row.ci_low, row.ci_high)}"
            )
        adverse = (
            f" · adverse first {_pct(horizon.mae_first / horizon.evaluated)}"
            if horizon.evaluated
            else ""
        )
        ambiguous = (
            f" · {horizon.path_ambiguous} unobservable order"
            if horizon.path_ambiguous
            else ""
        )
        screen.fields.append(
            (f"Reached within {horizon.horizon_id}", "\n".join(rows) + adverse + ambiguous)
        )

    if assessment.tp_estimates:
        screen.fields.append(
            (
                "Target, measured",
                " · ".join(
                    f"p{e.quantile * 100:.0f} {e.points:g}pt"
                    + (f" ({_fmt(e.price)})" if e.price is not None else "")
                    for e in assessment.tp_estimates
                ),
            )
        )
    if assessment.sl_estimates:
        screen.fields.append(
            (
                "Adverse, measured",
                " · ".join(
                    f"p{e.quantile * 100:.0f} {e.points:g}pt"
                    + (f" ({_fmt(e.price)})" if e.price is not None else "")
                    for e in assessment.sl_estimates
                ),
            )
        )

    paired = assessment.paired
    if paired is not None and paired.rate is not None:
        screen.fields.append(
            (
                f"+{paired.favourable:g} before −{paired.adverse:g}",
                f"{_pct(paired.rate)} ({paired.favourable_first}/{paired.evaluated})"
                f"{_interval(paired.ci_low, paired.ci_high)}",
            )
        )

    screen.next_move = _next_move_line(assessment)
    return screen


def _next_move_line(assessment: Any) -> str:
    """The one-line summary (§62's template), built from the same numbers as the table.

    Derived rather than written separately: a summary line computed from its own reading of
    the cohort is how a headline comes to disagree with the table under it, and the headline
    is the part people quote.
    """
    horizon = assessment.confirmations[0] if assessment.confirmations else None
    row = horizon.thresholds[0] if horizon and horizon.thresholds else None
    reached = (
        f"reached +{row.threshold:g} within {horizon.horizon_id} in {_pct(row.rate)}"
        f"{_interval(row.ci_low, row.ci_high)}"
        if row is not None
        else "no reached-rate"
    )
    adverse = (
        f" · typical adverse first −{assessment.sl_estimates[0].points:g}pt"
        if assessment.sl_estimates
        else ""
    )
    return (
        f"Bias {assessment.trend_read.bias.value} · cohort n={assessment.n} · "
        f"{reached}{adverse}"
    )


@dataclass(frozen=True)
class LiveEnableGate:
    """Whether ``/trading enable`` may proceed on this account, and what to show first.

    Two conditions, both required, and they are deliberately not the same thing (11A F-3):

    * ``AUREON_ALLOW_LIVE_EXECUTION=true`` on the box -- a line somebody wrote on the
      machine while looking at which terminal was open;
    * a second confirmation screen naming the login, server, balance and the word LIVE --
      so the person pressing the button has read what they are arming.

    Without the flag there is no second screen to reach: enabling would arm a switch the
    executor is going to ignore anyway, and a Discord message saying "enabled" over an
    executor in reconcile-only mode is the worst of both.
    """

    allowed: bool
    needs_live_confirmation: bool
    message: str
    #: The LIVE screen's own lines, when one is needed. Built here rather than in the view
    #: for the reason every other screen is: the content is a decision, and it is tested
    #: without a Discord client.
    fields: tuple[tuple[str, str], ...] = ()


def plan_trading_enable(state: Any, *, allow_live_execution: bool) -> LiveEnableGate:
    """What ``/trading enable`` should do, given the account the observer published.

    Reads the published ``account_mode`` rather than a terminal: Discord holds no broker and
    no provider (CLAUDE.md). That is a weaker source than the executor's own read, and it is
    the right one for this job -- this gate decides what a HUMAN is shown, and the executor
    re-decides what it will actually do from its own connection.

    An unpublished mode is treated as real money, like everywhere else: the observer not
    having said yet is not the same as it having said "demo".
    """
    mode = getattr(state, "account_mode", None) or AccountMode.UNKNOWN
    if not mode.is_real_money:
        return LiveEnableGate(
            allowed=True, needs_live_confirmation=False, message=ENABLE_DETAILS
        )

    known = mode is AccountMode.REAL
    where = "a REAL-money account" if known else "an account it could not identify"
    if not allow_live_execution:
        return LiveEnableGate(
            allowed=False,
            needs_live_confirmation=False,
            message=(
                f"The observer reports {where}, and `AUREON_ALLOW_LIVE_EXECUTION` is not "
                "`true` on the executor's box.\n\n"
                "Enabling here would arm a switch the executor is going to ignore — it "
                "starts in reconcile-only mode on a live account and refuses every "
                "request with `live_execution_not_allowed`. Set the variable on the box "
                "first, deliberately, then come back."
            ),
        )

    quote = getattr(state, "symbols", ()) or ()
    first = quote[0] if quote else None
    return LiveEnableGate(
        allowed=True,
        needs_live_confirmation=True,
        message=(
            "**This is a LIVE account.** Orders confirmed after this will move real "
            "money.\n\n" + ENABLE_DETAILS
        ),
        fields=(
            ("Account", mode.value.upper()),
            (
                "Symbol",
                getattr(first, "symbol", UNKNOWN) if first is not None else UNKNOWN,
            ),
            ("Observer state", str(getattr(first, "market_state", UNKNOWN))),
        ),
    )


def assessment_info_line(assessment: Any, *, now: datetime | None = None) -> str | None:
    """One line about the last `/monitor` readout for this symbol (9D-4, §64).

    An **info** line and nothing more. Deliberately carries the bias, the n and the
    reached-rate but **not** the target and stop prices: those are quantiles of past
    excursions, and printing a price on the screen where somebody is about to place an
    order is one copy-and-paste away from being treated as a level. `/monitor` shows them
    in a context that says what they are.

    Nothing anywhere prefills an SL or a TP from an assessment, and a test asserts the
    confirmation carries neither field.
    """
    if assessment is None:
        return None
    age = ""
    if assessment.created_at is not None:
        minutes = (to_utc(now or utc_now()) - to_utc(assessment.created_at)).total_seconds() / 60
        # Stated plainly: a readout from four hours ago describes a market that may be gone,
        # and "last assessment" without a time reads as "current".
        age = f", {minutes:.0f}m ago"
    if assessment.insufficient:
        return (
            f"Last assessment{age}: insufficient history (n={assessment.n}). "
            "Run `/monitor` for the detail."
        )
    rate = ""
    if assessment.confirmations and assessment.confirmations[0].thresholds:
        row = assessment.confirmations[0].thresholds[0]
        rate = (
            f", reached +{row.threshold:g} in {_pct(row.rate)} of {row.evaluated}"
        )
    return (
        f"Last assessment{age}: bias {assessment.trend_read.bias.value}, "
        f"cohort n={assessment.n}{rate}. Measured, not advice — `/monitor` for the detail."
    )


async def attach_assessment(context, screen, symbol: str) -> None:
    """Add the "last assessment" info line, if there is one (9D-4, §64).

    A read, appended to the screen, and nothing else. Never prefills a stop or a target:
    an assessment's quantiles describe what happened to similar detections, and an order
    screen is the one place where a number is read as an instruction.

    A failure here is swallowed. The line is context; the confirmation is the thing that
    matters, and a readout that could not be read must not stop somebody placing the trade
    they had already decided on.
    """
    if getattr(context, "assessments", None) is None:
        return
    try:
        latest = await context.run(context.assessments.latest_for_symbol, symbol)
    except Exception:  # noqa: BLE001 - context, never critical
        log.debug("could not read the last assessment for %s", symbol, exc_info=True)
        return
    line = assessment_info_line(latest)
    if line:
        screen.info.append(line)


@dataclass
class ReminderScreen:
    """A fired ``/remind`` alert, rendered from its FROZEN snapshot (9C)."""

    title: str
    symbol: str
    alert_id: str
    requested_by: str
    fields: list[tuple[str, str]] = field(default_factory=list)
    footer: str = ""
    note: str | None = None
    side: str | None = None


def build_reminder(alert: PriceAlert) -> ReminderScreen:
    """What a fired alert says, entirely from what was frozen when it crossed (9C).

    Not one value is read live. The whole point of the snapshot is that the message
    describes the market that crossed the level rather than the market a few seconds later,
    by which time the move may have reversed — and a reader has no way to tell those apart
    once the message is written.

    The ``side`` prefilled on the Execute button follows the alert's own direction: an alert
    for price going *above* a level is a reason somebody might buy. It is a prefill and
    nothing more — the lot is still typed and CONFIRM is still required.
    """
    snapshot = alert.fired_snapshot or {}
    screen = ReminderScreen(
        title=f"{alert.symbol} {alert.side} {alert.level:g} — reached",
        symbol=alert.symbol,
        alert_id=alert.alert_id,
        requested_by=alert.requested_by,
        note=alert.note,
        side="buy" if alert.side == "above" else "sell",
        footer=f"{RESEARCH_ONLY} · {TICK_VOLUME_NOTE} · {alert.alert_id}",
    )

    fired_at = alert.fired_at.isoformat() if alert.fired_at else UNKNOWN
    minutes = snapshot.get("minutes_since_cross")
    screen.fields = [
        ("Price", f"{_fmt(alert.fired_price)} (level {alert.level:g})"),
        ("At", fired_at),
        (
            "EMA",
            f"{_fmt(snapshot.get('ema_fast'))} / {_fmt(snapshot.get('ema_slow'))}"
            f" — fast {snapshot.get('ema_relation') or UNKNOWN}"
            + _distance_tail(snapshot.get("ema_distance")),
        ),
        (
            "Last cross",
            _event(snapshot.get("last_cross"), "direction")
            + (f", {minutes:g}m ago" if isinstance(minutes, (int, float)) else ""),
        ),
        (
            "RSI",
            f"{_fmt(snapshot.get('rsi'), digits=1)} {snapshot.get('rsi_zone') or UNKNOWN}",
        ),
        (
            "Session",
            f"{snapshot.get('session') or UNKNOWN} "
            f"{snapshot.get('session_trend') or UNKNOWN}",
        ),
        (
            "Session range",
            f"{_fmt(snapshot.get('session_low'))}–{_fmt(snapshot.get('session_high'))}",
        ),
        ("Detections today", str(snapshot.get("detections_today") or 0)),
        ("Tick volume", _snapshot_volume_line(snapshot)),
        ("Volatility", _snapshot_volatility_line(snapshot)),
        ("Last wick", _event(snapshot.get("last_wick"), "classification")),
        ("Last sweep", _event(snapshot.get("last_sweep"), "direction", "level_type")),
        (
            "Last breakout",
            _event(snapshot.get("last_breakout"), "direction", "level_type"),
        ),
    ]
    return screen


def _distance_tail(distance: Any) -> str:
    """The EMA gap, when there is one.

    Rendered beside the relation rather than instead of it: "fast above" is the bias and
    the gap is how convinced it is, and a reader in a hurry needs the first without
    subtracting the second.
    """
    if not isinstance(distance, (int, float)):
        return ""
    return f" by {abs(distance):g}"


def _snapshot_volume_line(snapshot: dict[str, Any]) -> str:
    profiles = snapshot.get("volume_profile") or {}
    asia = profiles.get("asia") or profiles.get("current_session") or {}
    if not asia:
        return UNKNOWN
    return (
        f"POC {_fmt(asia.get('poc_price'))} "
        f"VA {_fmt(asia.get('value_area_low'))}–{_fmt(asia.get('value_area_high'))}"
    )


def _snapshot_volatility_line(snapshot: dict[str, Any]) -> str:
    context = snapshot.get("volatility") or {}
    if not context:
        return UNKNOWN
    ratio = context.get("session_range_vs_median")
    tail = f" ({ratio:.2f}× median)" if isinstance(ratio, (int, float)) else ""
    return f"{context.get('regime') or UNKNOWN} · ATR14 {_fmt(context.get('atr_14'))}{tail}"


# ── /status (§59, §61-§63) ────────────────────────────────────────────────────


@dataclass
class ServiceStatus:
    name: str
    freshness: Freshness
    updated_at: datetime | None
    detail: str | None = None


@dataclass
class StatusScreen:
    """What ``/status`` shows (§59, §61-§63)."""

    overall: Freshness
    services: list[ServiceStatus] = field(default_factory=list)
    symbols: list[tuple[str, str]] = field(default_factory=list)
    trading_enabled: bool = False
    open_trades: int = 0
    pending_requests: int = 0
    review_summary: str | None = None
    market_closed: bool = False
    #: Where the observer said it was in the weekly cycle, and when the market next opens
    #: (11B). READ, never computed: Discord may not call the broker (CLAUDE.md), so it has
    #: no feed to classify and no business holding a second opinion about the weekly
    #: boundary. The process with the terminal publishes both in ``system_state``.
    sleep_phase: SleepPhase | None = None
    next_market_open: datetime | None = None
    #: The symbol this screen was scoped to, or ``None`` for every observed symbol (9A).
    #: Stated rather than implied: with two symbols observed, a panel showing one of them
    #: and a count covering both would be read as one picture of one instrument.
    symbol: str | None = None
    #: The live §59 panels, one entry per symbol. Populated only when the market is
    #: OPEN: on a closed market the numbers are a snapshot of whenever it shut, and
    #: showing them beside a live layout invites reading them as current.
    live_panels: list[LivePanel] = field(default_factory=list)
    #: When system_state was last written, and how long ago at render time (§59).
    #: Both, because they answer different questions: the age is what tells a reader
    #: whether to trust the numbers, and the timestamp is what they quote when
    #: something looks wrong. A freshness word alone hides how far past the threshold
    #: a STALE screen has drifted -- 46 seconds and six hours read identically.
    updated_at: datetime | None = None
    age_seconds: float | None = None

    @property
    def asleep(self) -> bool:
        return self.sleep_phase in {SleepPhase.ASLEEP, SleepPhase.WAKING}

    @property
    def closed_line(self) -> str | None:
        """``closed — next open Sun 20 Sep 22:00 UTC``, or None while the market is open.

        The next open is the whole point of the line. "Closed" on its own reads as a fault
        to anybody who has not checked the calendar, and the first thing they would do about
        it is restart something.
        """
        if not self.market_closed and not self.asleep:
            return None
        phase = (self.sleep_phase or SleepPhase.ASLEEP).value
        if self.next_market_open is None:
            return f"closed ({phase}) — next open unknown"
        stamp = self.next_market_open.strftime("%a %d %b %H:%M")
        return f"closed ({phase}) — next open {stamp} UTC"

    @property
    def updated_line(self) -> str:
        """``last updated HH:MM:SSZ (12s ago) — LIVE``, or a plain never."""
        if self.updated_at is None:
            return f"last updated never — {self.overall.value.upper()}"
        age = "" if self.age_seconds is None else f" ({self.age_seconds:.0f}s ago)"
        stamp = self.updated_at.strftime("%H:%M:%SZ")
        return f"last updated {stamp}{age} — {self.overall.value.upper()}"


NO_REVIEW_YET = "no completed review yet"
def _fmt(value: object, *, digits: int = 2) -> str:
    """A number for a human, or an em dash.

    ``None`` renders as a dash rather than 0 or "n/a": a zero is a measurement and a dash
    is the absence of one, and on a status screen that difference is the whole point of
    the field.
    """
    if value is None:
        return UNKNOWN
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


def _fmt_signed(value: float | None, *, digits: int = 2) -> str:
    return UNKNOWN if value is None else f"{value:+.{digits}f}"


def _fmt_at(value: object) -> str:
    """An instant as HH:MM:SS UTC, or a dash. Never a bare ISO string on a screen."""
    if value is None:
        return UNKNOWN
    if isinstance(value, str):
        return value[11:19] + "Z" if len(value) >= 19 else value
    if isinstance(value, datetime):
        return value.strftime("%H:%M:%SZ")
    return str(value)


@dataclass
class LivePanel:
    """One symbol's live §59 lines, already rendered.

    Rendered here rather than in ``embeds.py`` so the layout is unit-testable without a
    Discord client -- the same reason every other decision in this module lives here.
    """

    symbol: str
    market_state: str
    lines: list[str] = field(default_factory=list)


def build_live_panel(state: Any) -> LivePanel:
    """The §59 layout for one symbol, from a ``SymbolState``.

    Every field is rendered whether or not it has a value, because a panel that hides
    its empty rows changes shape as data arrives -- and a reader cannot tell a missing
    EMA from a panel that never had that row.
    """
    panel = LivePanel(
        symbol=getattr(state, "symbol", "?"),
        market_state=getattr(getattr(state, "market_state", None), "value", "unknown"),
    )
    quote = getattr(state, "last_quote", None)
    session = getattr(state, "session", None)

    panel.lines = [
        f"bid/ask {_fmt(getattr(quote, 'bid', None))} / {_fmt(getattr(quote, 'ask', None))}",
        f"EMA {_fmt(state.ema_fast)} / {_fmt(state.ema_slow)}"
        f"  (dist {_fmt_signed(state.ema_distance)})",
        f"RSI {_fmt(state.rsi, digits=1)} {state.rsi_zone or UNKNOWN}",
        f"session {getattr(session, 'value', UNKNOWN)} {state.session_trend or UNKNOWN}"
        f"  H {_fmt(state.session_high)}  L {_fmt(state.session_low)}",
        f"crosses today {state.ema_crosses_today}"
        f" ({state.bullish_crosses_today}↑ {state.bearish_crosses_today}↓)"
        f"  session {state.ema_crosses_session}"
        f" ({state.bullish_crosses_session}↑ {state.bearish_crosses_session}↓)",
        f"last cross {_event(state.last_cross, 'direction')} at "
        f"{_fmt_at((state.last_cross or {}).get('at'))}",
        f"last sweep {_event(state.last_sweep, 'direction', 'level_type')} at "
        f"{_fmt_at((state.last_sweep or {}).get('at'))}",
        f"last breakout {_event(state.last_breakout, 'direction', 'level_type')} at "
        f"{_fmt_at((state.last_breakout or {}).get('at'))}",
        f"last wick {_event(state.last_wick, 'classification')} at "
        f"{_fmt_at((state.last_wick or {}).get('at'))}",
        f"detections today {state.detections_today}",
        *_mtf_lines(state),
        *_context_lines(state, quote),
    ]
    return panel


def _mtf_lines(state: Any) -> list[str]:
    """The higher timeframes, one row, from the observer's published reads (11D).

    Rendered whether or not the reads exist, like every other row here: a block that appeared
    only when populated would change the panel's shape as data arrives.

    A timeframe that was never read prints a dash rather than "sideways", because
    "nobody computed H4" and "H4 was flat" are different facts and this panel is the place a
    reader would otherwise conflate them. No alignment: alignment needs a direction, and this
    panel describes a market rather than a signal.
    """
    from aureon.models.enums import Timeframe

    mtf = getattr(state, "mtf", None)
    shown = (Timeframe.M15, Timeframe.M30, Timeframe.H1, Timeframe.H4, Timeframe.D1)
    if mtf is None:
        return [f"higher tf {' '.join(f'{tf.value}:{UNKNOWN}' for tf in shown)}"]
    cells = []
    for timeframe in shown:
        bias = mtf.bias_of(timeframe)
        cells.append(f"{timeframe.value}:{UNKNOWN if bias is None else bias.value[:4]}")
    return [f"higher tf {' '.join(cells)}"]


def _context_lines(state: Any, quote: Any) -> list[str]:
    """The §19 block: value areas, the nodes nearest price, and volatility (9B).

    Rendered whether or not the values exist, like every other row on this panel: a block
    that appears only when populated changes the panel's shape as data arrives, and a reader
    cannot tell "no profile yet" from "this panel never had that row".

    Nearest node is computed against the CURRENT bid rather than stored, because "nearest"
    depends on where price is now and a stored answer would be as old as the last candle.
    """
    profiles = getattr(state, "volume_profile", None) or {}
    volatility = getattr(state, "volatility", None)
    price = getattr(quote, "bid", None)

    # Named once at the top of the block rather than on each line: three rows each carrying
    # the same caveat reads as noise and gets skipped, which defeats the caveat (11A, F-5).
    lines = [f"— {TICK_VOLUME_NOTE} —"]
    for scope in ("current_session", "asia"):
        summary = profiles.get(scope)
        label = scope.replace("_", " ")
        if summary is None:
            lines.append(f"{label} tick-volume profile {UNKNOWN}")
            continue
        lines.append(
            f"{label} ({summary.scope}) POC {_fmt(summary.poc_price)}"
            f"  VA {_fmt(summary.value_area_low)}–{_fmt(summary.value_area_high)}"
        )
    day = profiles.get("day")
    if day is not None and price is not None:
        lines.append(
            f"nearest HVN {_fmt(_closest(day.hvn, price))}"
            f"  LVN {_fmt(_closest(day.lvn, price))}"
        )
    else:
        lines.append(f"nearest HVN {UNKNOWN}  LVN {UNKNOWN}")

    if volatility is None:
        lines.append(f"ATR14 {UNKNOWN}  regime {UNKNOWN}")
    else:
        ratio = volatility.session_range_vs_median
        lines.append(
            f"ATR14 {_fmt(volatility.atr_14)}"
            f" ({_fmt(volatility.atr_points, digits=0)} pts)"
            f"  regime {volatility.regime or UNKNOWN}"
            + (f" ({ratio:.2f}× median)" if ratio is not None else "")
        )
    return lines


def _closest(prices: tuple[float, ...], price: float) -> float | None:
    if not prices:
        return None
    return min(prices, key=lambda candidate: (abs(candidate - price), candidate))


def _event(payload: dict[str, object] | None, *keys: str) -> str:
    if not payload:
        return UNKNOWN
    parts = [str(payload.get(key)) for key in keys if payload.get(key) is not None]
    return " ".join(parts) or UNKNOWN


def build_status(
    *,
    system_state: Any | None,
    heartbeats: dict[str, Any],
    settings: ExecutionSettings,
    open_trades: int = 0,
    pending_requests: int = 0,
    latest_review: Any | None = None,
    latest_reviews: dict[str, Any] | None = None,
    stale_after: float | None = None,
    symbol: str | None = None,
    now: datetime | None = None,
    sleep_heartbeat_seconds: float = DEFAULT_SLEEP_HEARTBEAT_SECONDS,
) -> StatusScreen:
    """Assemble the status screen from Firestore reads only (§59).

    Freshness is **computed here from ``updated_at``**, never read from a stored flag. A
    stored "live" would keep claiming liveness precisely when the writer had died -- the
    one moment the field matters.
    """
    moment = to_utc(now or utc_now())
    limit = stale_after if stale_after is not None else settings.status_stale_after_seconds

    # 11B: the hang detector has to widen while the services are asleep, or every one of
    # them reads STALE all weekend. Their heartbeats are five minutes apart by then and the
    # awake threshold is forty-five seconds, so without this the first thing an operator
    # sees on a Saturday is four dead services -- and a screen that cries wolf every weekend
    # is a screen nobody reads on the Monday it is right. Twice the sleep cadence: one
    # missed beat is a gap, two is a hang.
    published_phase = getattr(system_state, "sleep_phase", None)
    if published_phase in {SleepPhase.ASLEEP, SleepPhase.WAKING}:
        limit = max(limit, 2 * sleep_heartbeat_seconds)

    services: list[ServiceStatus] = []
    for name in ("observer", "executor", "monitor", "discord"):
        beat = heartbeats.get(name)
        updated = getattr(beat, "updated_at", None)
        services.append(
            ServiceStatus(
                name=name,
                freshness=freshness_of(
                    updated,
                    stale_after=limit,
                    offline_after=max(limit * 4, DEFAULT_OFFLINE_AFTER_SECONDS),
                    now=moment,
                ),
                updated_at=updated,
                detail=None if beat is not None else "never reported",
            )
        )

    overall = freshness_of(
        getattr(system_state, "updated_at", None),
        stale_after=limit,
        offline_after=max(limit * 4, DEFAULT_OFFLINE_AFTER_SECONDS),
        now=moment,
    )

    symbols: list[tuple[str, str]] = []
    market_closed = False
    for symbol_state in getattr(system_state, "symbols", ()) or ():
        state = symbol_state.market_state
        symbols.append((f"{symbol_state.symbol} {symbol_state.timeframe.value}", state.value))
        if state is MarketState.CLOSED:
            market_closed = True

    # A published ASLEEP is as good as a CLOSED symbol row, and better: the row is whatever
    # the market-state service last said, and the phase is what the observer acted on.
    if published_phase in {SleepPhase.ASLEEP, SleepPhase.WAKING}:
        market_closed = True

    state_updated = getattr(system_state, "updated_at", None)
    screen = StatusScreen(
        updated_at=to_utc(state_updated) if state_updated else None,
        age_seconds=(
            (moment - to_utc(state_updated)).total_seconds() if state_updated else None
        ),
        overall=overall,
        services=services,
        symbols=symbols,
        trading_enabled=settings.trading_enabled,
        open_trades=open_trades,
        pending_requests=pending_requests,
        market_closed=market_closed,
        symbol=symbol.upper() if symbol else None,
        sleep_phase=published_phase,
        next_market_open=(
            to_utc(getattr(system_state, "next_market_open", None))
            if getattr(system_state, "next_market_open", None)
            else None
        ),
    )

    # §59 vs §61-§63: the two modes are mutually exclusive, deliberately.
    #
    # On an OPEN market the screen is live: the indicator, session and last-event panels
    # below. On a CLOSED market those numbers are a snapshot of whenever the market shut,
    # and rendering them in a live layout invites reading them as current -- so the closed
    # screen shows the completed review instead, which is what a reader actually wants
    # after the close.
    if market_closed:
        if latest_reviews:
            # One line per symbol, each labelled (9A). Reviews are per symbol, so a single
            # unlabelled summary would be a statement about whichever one was picked.
            screen.review_summary = "\n".join(
                f"**{name}** — {summarise_review(review)}"
                for name, review in sorted(latest_reviews.items())
            )
        else:
            screen.review_summary = (
                summarise_review(latest_review)
                if latest_review is not None
                else NO_REVIEW_YET
            )
    else:
        screen.live_panels = [
            build_live_panel(symbol_state)
            for symbol_state in getattr(system_state, "symbols", ()) or ()
        ]
    return screen


def summarise_review(review: Any) -> str:
    """One-line summary of a daily or weekly review (§61, §63).

    Reports the PENDING count alongside the totals, because a reached-N figure without it
    invites exactly the misreading Phase 3 exists to prevent.
    """
    detections = getattr(review, "detections_total", 0)
    trades = getattr(review, "trades_total", 0)
    pnl = getattr(review, "realized_pnl", 0.0)
    excluded = getattr(review, "pending_horizons_excluded", 0)
    rule = getattr(review, "evaluation_rule_id", "?")
    return (
        f"{detections} detections, {trades} trades, P&L {pnl:+.2f} "
        f"({excluded} horizons still pending, excluded) — rule {rule}"
    )


# ── /trading (§57) ────────────────────────────────────────────────────────────


ENABLE_DETAILS = (
    "Enabling trading means a CONFIRMED trade request will be sent to the broker with "
    "real money. Before confirming, check:\n"
    "• the executor is LIVE (`/status`)\n"
    "• the account and symbol are the ones you intend\n"
    "• `max_lot` and `max_spread_points` are set for current conditions\n"
    "Disabling takes effect on the very next request; enabling does not place anything by "
    "itself."
)


def trading_change_summary(enabled: bool, actor: str, reason: str | None = None) -> str:
    if enabled:
        return f"Trading ENABLED by {actor}."
    return f"Trading DISABLED by {actor}" + (f" — {reason}" if reason else ".")
