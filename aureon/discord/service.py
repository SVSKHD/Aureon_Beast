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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.control import ControlRequest
from aureon.models.detection import Detection
from aureon.models.enums import (
    ControlRequestKind,
    FillingMode,
    Freshness,
    LinkType,
    MarketState,
    OrderType,
    TradeRequestStatus,
)
from aureon.models.market import QuoteSnapshot, SymbolInfo
from aureon.models.settings import ExecutionSettings
from aureon.models.system import DEFAULT_OFFLINE_AFTER_SECONDS, freshness_of
from aureon.models.trade import TradeRequest

log = logging.getLogger(__name__)


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
    if volume > settings.max_lot:
        return LotCheck(
            False, f"{volume} exceeds Aureon's maximum lot of {settings.max_lot}"
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


def unsupported_mode_notice(info: SymbolInfo | None, mode: FillingMode) -> str | None:
    """The §39 message, or ``None`` when the mode is fine."""
    if info is None or not info.filling_modes:
        return None
    if mode in info.filling_modes:
        return None
    available = ", ".join(m.value.upper() for m in info.filling_modes)
    return f"{mode.value.upper()} not supported for {info.symbol} — available: {available}"


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
        ("Max deviation", f"{min(draft.deviation_points, settings.max_deviation_points)} points"),
        ("Bid / Ask", f"{quote.bid:g} / {quote.ask:g}"),
        (
            "Spread",
            f"{quote.spread_points:.1f} points" if quote.spread_points is not None else "unknown",
        ),
        ("Quote age", f"{screen.quote_age_seconds:.1f}s"),
        ("Market state", market_state.value),
        ("Linked detection", detection.event_key if detection else "none"),
        ("Requested by", draft.requested_by),
        ("Confirmation expires in", f"{settings.confirmation_ttl_seconds:g}s"),
    ]

    # Warnings, not blocks: the human decides, and the guard still refuses at execution
    # time. Telling them now is the difference between an informed choice and a surprise.
    if market_state is not MarketState.OPEN:
        screen.warnings.append(
            f"Market is {market_state.value} — this will be refused at execution."
        )
    if quote.spread_points is not None and quote.spread_points > settings.max_spread_points:
        screen.warnings.append(
            f"Spread {quote.spread_points:.1f} exceeds the {settings.max_spread_points:g} "
            "point limit — this will be refused."
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


NO_REVIEW_YET = "no completed review yet"


def build_status(
    *,
    system_state: Any | None,
    heartbeats: dict[str, Any],
    settings: ExecutionSettings,
    open_trades: int = 0,
    pending_requests: int = 0,
    latest_review: Any | None = None,
    stale_after: float | None = None,
    now: datetime | None = None,
) -> StatusScreen:
    """Assemble the status screen from Firestore reads only (§59).

    Freshness is **computed here from ``updated_at``**, never read from a stored flag. A
    stored "live" would keep claiming liveness precisely when the writer had died -- the
    one moment the field matters.
    """
    moment = to_utc(now or utc_now())
    limit = stale_after if stale_after is not None else settings.status_stale_after_seconds

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

    screen = StatusScreen(
        overall=overall,
        services=services,
        symbols=symbols,
        trading_enabled=settings.trading_enabled,
        open_trades=open_trades,
        pending_requests=pending_requests,
        market_closed=market_closed,
    )

    # §61-§63: on a closed market, show the latest completed review. Until Phase 7 ships
    # there is nothing to show, and saying so plainly beats an empty panel.
    if market_closed:
        screen.review_summary = (
            summarise_review(latest_review) if latest_review is not None else NO_REVIEW_YET
        )
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
