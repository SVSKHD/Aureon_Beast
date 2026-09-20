"""Enumerations and the status-transition state machine.

CLAUDE.md: *"All state changes go through aureon.models.enums.assert_transition.
Never write a status field without it."* This module is therefore the only place
that knows which status edges exist. A repository that wants to move a document
calls the matching ``assert_*_transition`` inside its Firestore transaction; an
illegal edge raises before anything is written.

The edges come from §25 plus decision 1, which supplies the ones §25 leaves
implicit. Two design points worth stating, because both are load-bearing:

* **Failures are a status plus a ``FailureCode``, not a status each** (§41,
  decision 2). ``FAILED_SPREAD_LIMIT`` is ``status=FAILED`` +
  ``failure_code=SPREAD_LIMIT``. This keeps the graph small enough to audit by
  eye while the enum carries the detail a Discord embed needs.
* **Nothing returns to ``REQUESTED`` or ``CONFIRMED``** (decision 1). A
  confirmation is a one-way door; re-confirming is a repository-level no-op that
  returns the current document, never a transition back.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class TransitionError(ValueError):
    """An attempted status change is not a legal edge of the state machine."""


# ── Market and direction ──────────────────────────────────────────────────────


class Direction(StrEnum):
    """Which way a detection points, or a position faces."""

    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Direction:
        return Direction.SELL if self is Direction.BUY else Direction.BUY

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL.

        Lets excursion and threshold maths be written once and stay
        direction-agnostic, instead of branching at every comparison.
        """
        return 1 if self is Direction.BUY else -1


class OrderType(StrEnum):
    """Order kinds Aureon can place (§37, §42)."""

    MARKET_BUY = "market_buy"
    MARKET_SELL = "market_sell"
    BUY_STOP = "buy_stop"
    SELL_STOP = "sell_stop"
    BUY_LIMIT = "buy_limit"
    SELL_LIMIT = "sell_limit"

    @property
    def is_market(self) -> bool:
        return self in {OrderType.MARKET_BUY, OrderType.MARKET_SELL}

    @property
    def is_pending(self) -> bool:
        return not self.is_market

    @property
    def direction(self) -> Direction:
        return Direction.BUY if "buy" in self.value else Direction.SELL


class FillingMode(StrEnum):
    """Broker filling policy. Offered only when symbol_info allows it (§39)."""

    FOK = "fok"
    IOC = "ioc"
    RETURN = "return"



class Timeframe(StrEnum):
    """Candle timeframes, with the arithmetic the engines need (§7).

    ``minutes`` is what lets candle-boundary alignment, gap detection ("a gap
    larger than 2 timeframes", §22) and candle-count horizons be written once
    instead of re-deriving a minute count at each call site.
    """

    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def minutes(self) -> int:
        return _TIMEFRAME_MINUTES[self]

    @property
    def seconds(self) -> int:
        return self.minutes * 60


_TIMEFRAME_MINUTES: Mapping[Timeframe, int] = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
    Timeframe.H4: 240,
    Timeframe.D1: 1440,
}


class MarketState(StrEnum):
    """Tradability of a symbol right now (§10)."""

    OPEN = "open"
    CLOSED = "closed"
    PREOPEN = "preopen"
    STALE = "stale"
    UNKNOWN = "unknown"


class SessionName(StrEnum):
    """Trading sessions (§18). Boundaries live in aureon/config/sessions.py."""

    ASIA = "asia"
    LONDON = "london"
    NEW_YORK = "new_york"
    OFF = "off"


class Freshness(StrEnum):
    """How current a service's last write is (§59, §61-§63)."""

    LIVE = "live"
    STALE = "stale"
    OFFLINE = "offline"


# ── Trade requests ────────────────────────────────────────────────────────────


class TradeRequestStatus(StrEnum):
    """Lifecycle of a human-initiated trade request (§25).

    ``EXECUTING`` is the critical state: it means a send is in flight or its
    result is unknown. An unknown broker result must LEAVE the request in
    ``EXECUTING`` for reconciliation, never move it to ``FAILED`` -- failing it
    would invite a resend and a double trade.
    """

    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    FAILED_STALE = "failed_stale"
    FAILED_RECONCILIATION = "failed_reconciliation"


class TradeStatus(StrEnum):
    """Lifecycle of an actual position. MT5 is the truth (§49-§53)."""

    OPEN = "open"
    PARTIALLY_CLOSED = "partially_closed"
    CLOSED = "closed"


class TradeSource(StrEnum):
    """Where a trade came from. External trades are imported, never managed (§52)."""

    AUREON = "aureon"
    EXTERNAL_MT5 = "external_mt5"


class LinkType(StrEnum):
    """How a trade came to be associated with a detection (§50).

    ``INFERRED`` is legal only on a review document. Decision 8: a
    ``TradeRequest`` or ``Trade`` that claims an inferred link is rejected,
    because a guess must never harden into a stored fact about a real trade.
    """

    EXPLICIT = "explicit"
    INFERRED = "inferred"


class FailureCode(StrEnum):
    """Why a request failed (§56, §57, §41, §78).

    Carried alongside ``FAILED`` rather than expanded into separate statuses
    (decision 2). Every guard rule in Phase 4 owns one of these.
    """

    # Operator / settings gates (§57)
    TRADING_DISABLED = "trading_disabled"
    NOT_AUTHORIZED = "not_authorized"

    # Market condition gates (§41, §56)
    MARKET_CLOSED = "market_closed"
    SPREAD_LIMIT = "spread_limit"
    STALE_QUOTE = "stale_quote"
    DEVIATION_EXCEEDED = "deviation_exceeded"

    # Request validity (§42, §56)
    CONFIRMATION_EXPIRED = "confirmation_expired"
    SYMBOL_NOT_FOUND = "symbol_not_found"
    SYMBOL_NOT_TRADEABLE = "symbol_not_tradeable"
    VOLUME_INVALID = "volume_invalid"
    MAX_LOT_EXCEEDED = "max_lot_exceeded"
    STOPS_TOO_CLOSE = "stops_too_close"
    INVALID_STOPS = "invalid_stops"
    FILLING_MODE_UNSUPPORTED = "filling_mode_unsupported"

    # Account / exposure gates (§56)
    INSUFFICIENT_MARGIN = "insufficient_margin"
    MAX_OPEN_POSITIONS = "max_open_positions"
    MAX_DAILY_TRADES = "max_daily_trades"

    # Broker outcomes
    BROKER_REJECTED = "broker_rejected"
    REQUOTE = "requote"
    CONNECTION_LOST = "connection_lost"

    # Lease / reconciliation
    LEASE_EXPIRED = "lease_expired"
    RECONCILIATION_NOT_FOUND = "reconciliation_not_found"
    RECONCILIATION_AMBIGUOUS = "reconciliation_ambiguous"

    UNKNOWN = "unknown"


class ControlRequestKind(StrEnum):
    """A Discord-initiated action on something already live (§46, §47)."""

    CANCEL = "cancel"
    CLOSE = "close"


class ControlRequestStatus(StrEnum):
    """Lifecycle of a control request. Mirrors the claim/lease pattern."""

    REQUESTED = "requested"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_STALE = "failed_stale"


# ── Evaluation (§21-§23) ──────────────────────────────────────────────────────


class HorizonStatus(StrEnum):
    """Whether a horizon's answer is known yet.

    The distinction that keeps hindsight out of the reviews: a ``PENDING``
    horizon is *unknown*, not *failed*, and must be excluded from any
    reached-N count rather than counted as a miss.
    """

    PENDING = "pending"
    COMPLETE = "complete"
    INVALID = "invalid"


class HorizonKind(StrEnum):
    """How a horizon's end is defined (§21)."""

    CANDLES = "candles"
    MINUTES = "minutes"
    SESSION_CLOSE = "session_close"
    DAY_CLOSE = "day_close"
    OPPOSITE_CROSS = "opposite_cross"


class ReferencePrice(StrEnum):
    """Which price a horizon measures from (§21)."""

    CLOSE = "close"
    NEXT_OPEN = "next_open"


class PathClassification(StrEnum):
    """Whether adversity or the target came first (§23).

    ``MAE_FIRST`` means the adverse threshold was crossed before the favourable
    side reached its first threshold -- the difference between a detection a
    human could have held and one that would have stopped them out on the way.
    """

    MFE_FIRST = "mfe_first"
    MAE_FIRST = "mae_first"
    NONE = "none"



class ExecutionClassification(StrEnum):
    """How a human's execution compared with what the machine observed (§64).

    The comparison Phase 7 exists to make. Two of these six are the interesting ones:
    ``MISSED_AND_REACHED`` is a signal the machine found and the human did not take that
    then worked, and ``TAKEN_AND_NOT_REACHED`` is the reverse. Everything else is either
    agreement or absence.

    ``UNKNOWN`` is load-bearing and must never be folded into a "did not work" bucket. A
    detection whose horizons are still PENDING has no outcome yet; counting it as a miss
    would make every comparison here pessimistic, which is exactly the error Phase 3's
    COMPLETE-only rule exists to prevent.
    """

    #: Linked to a trade, and the evaluated outcome reached the threshold.
    TAKEN_AND_REACHED = "taken_and_reached"
    #: Linked to a trade, and the outcome did not reach it.
    TAKEN_AND_NOT_REACHED = "taken_and_not_reached"
    #: No trade, but the outcome reached the threshold -- an opportunity not taken.
    MISSED_AND_REACHED = "missed_and_reached"
    #: No trade, and the outcome did not reach it -- correctly skipped.
    MISSED_AND_NOT_REACHED = "missed_and_not_reached"
    #: A trade with no detection behind it -- the human's own idea.
    DISCRETIONARY = "discretionary"
    #: The outcome is not known yet. NOT a miss.
    UNKNOWN = "unknown"

    @property
    def was_taken(self) -> bool:
        return self in {
            ExecutionClassification.TAKEN_AND_REACHED,
            ExecutionClassification.TAKEN_AND_NOT_REACHED,
            ExecutionClassification.DISCRETIONARY,
        }

    @property
    def is_conclusive(self) -> bool:
        """Whether this classification says anything about an outcome."""
        return self is not ExecutionClassification.UNKNOWN


class ExcursionSource(StrEnum):
    """Whether excursions were watched live or rebuilt afterwards (§45)."""

    LIVE_TICKS = "live_ticks"
    RECONSTRUCTED = "reconstructed"


class DealEntry(StrEnum):
    """MT5 deal entry type, used to match deals to positions (§36)."""

    IN = "in"
    OUT = "out"
    INOUT = "inout"


# ── The state machine ─────────────────────────────────────────────────────────

# §25 + decision 1. A status absent from the keys, or mapping to an empty set,
# is terminal.
TRADE_REQUEST_TRANSITIONS: Mapping[TradeRequestStatus, frozenset[TradeRequestStatus]] = {
    TradeRequestStatus.REQUESTED: frozenset(
        {
            TradeRequestStatus.CONFIRMED,
            # Decision 1: the wizard was abandoned, or the confirmation TTL ran out.
            TradeRequestStatus.CANCELLED,
            TradeRequestStatus.EXPIRED,
        }
    ),
    TradeRequestStatus.CONFIRMED: frozenset(
        {
            TradeRequestStatus.EXECUTING,
            # Decision 1: the requester cancels before any executor claims it.
            TradeRequestStatus.CANCELLED,
            # Claimed too late: the confirmation is stale (§28).
            TradeRequestStatus.FAILED_STALE,
            # A guard can reject between claim and send (§56).
            TradeRequestStatus.FAILED,
        }
    ),
    TradeRequestStatus.EXECUTING: frozenset(
        {
            TradeRequestStatus.FILLED,
            TradeRequestStatus.PARTIALLY_FILLED,
            TradeRequestStatus.PENDING,
            TradeRequestStatus.FAILED,
            TradeRequestStatus.FAILED_RECONCILIATION,
        }
    ),
    TradeRequestStatus.PENDING: frozenset(
        {
            TradeRequestStatus.FILLED,
            TradeRequestStatus.PARTIALLY_FILLED,
            TradeRequestStatus.CANCELLED,
            TradeRequestStatus.EXPIRED,
            TradeRequestStatus.FAILED_RECONCILIATION,
        }
    ),
    TradeRequestStatus.PARTIALLY_FILLED: frozenset(
        {
            TradeRequestStatus.FILLED,
            # Volume can arrive in several deals, so this state can be re-entered
            # as each additional fill lands (Phase 5 partial fills).
            TradeRequestStatus.PARTIALLY_FILLED,
            TradeRequestStatus.CANCELLED,
            TradeRequestStatus.EXPIRED,
            TradeRequestStatus.FAILED_RECONCILIATION,
        }
    ),
    # Terminal. Decision 1: FAILED_RECONCILIATION included -- manual repair is an
    # operator action outside the state machine, audited separately.
    TradeRequestStatus.FILLED: frozenset(),
    TradeRequestStatus.CANCELLED: frozenset(),
    TradeRequestStatus.EXPIRED: frozenset(),
    TradeRequestStatus.FAILED: frozenset(),
    TradeRequestStatus.FAILED_STALE: frozenset(),
    TradeRequestStatus.FAILED_RECONCILIATION: frozenset(),
}

TRADE_TRANSITIONS: Mapping[TradeStatus, frozenset[TradeStatus]] = {
    TradeStatus.OPEN: frozenset({TradeStatus.PARTIALLY_CLOSED, TradeStatus.CLOSED}),
    # Re-entrant: 0.25 -> 0.10 -> 0.05 is three partial closes, not one.
    TradeStatus.PARTIALLY_CLOSED: frozenset(
        {TradeStatus.PARTIALLY_CLOSED, TradeStatus.CLOSED}
    ),
    TradeStatus.CLOSED: frozenset(),
}

HORIZON_TRANSITIONS: Mapping[HorizonStatus, frozenset[HorizonStatus]] = {
    HorizonStatus.PENDING: frozenset({HorizonStatus.COMPLETE, HorizonStatus.INVALID}),
    # A COMPLETE horizon is frozen: re-running the tracker must not change it.
    HorizonStatus.COMPLETE: frozenset(),
    HorizonStatus.INVALID: frozenset(),
}

CONTROL_REQUEST_TRANSITIONS: Mapping[ControlRequestStatus, frozenset[ControlRequestStatus]] = {
    ControlRequestStatus.REQUESTED: frozenset(
        {ControlRequestStatus.EXECUTING, ControlRequestStatus.FAILED_STALE}
    ),
    ControlRequestStatus.EXECUTING: frozenset(
        {ControlRequestStatus.COMPLETED, ControlRequestStatus.FAILED}
    ),
    ControlRequestStatus.COMPLETED: frozenset(),
    ControlRequestStatus.FAILED: frozenset(),
    ControlRequestStatus.FAILED_STALE: frozenset(),
}


def assert_transition(
    current: object,
    new: object,
    allowed: Mapping,
    *,
    label: str,
) -> None:
    """Raise ``TransitionError`` unless ``current -> new`` is a legal edge.

    The single gate every status write passes through. Deliberately a plain
    function over an explicit table: the whole state machine can be read in one
    screen and diffed in review, which matters more here than flexibility.
    """
    if current not in allowed:
        raise TransitionError(
            f"{label}: unknown current status {current!r}; "
            f"known: {sorted(str(s) for s in allowed)}"
        )
    permitted = allowed[current]
    if new not in permitted:
        if not permitted:
            raise TransitionError(
                f"{label}: {current} is terminal and cannot move to {new}"
            )
        raise TransitionError(
            f"{label}: illegal transition {current} -> {new}; "
            f"allowed from {current}: {sorted(str(s) for s in permitted)}"
        )


def assert_trade_request_transition(
    current: TradeRequestStatus, new: TradeRequestStatus
) -> None:
    """Gate a ``trade_requests`` status change (§25)."""
    assert_transition(current, new, TRADE_REQUEST_TRANSITIONS, label="trade_request")


def assert_trade_transition(current: TradeStatus, new: TradeStatus) -> None:
    """Gate a ``trades`` status change (§49-§53)."""
    assert_transition(current, new, TRADE_TRANSITIONS, label="trade")


def assert_horizon_transition(current: HorizonStatus, new: HorizonStatus) -> None:
    """Gate an evaluation horizon status change (§22)."""
    assert_transition(current, new, HORIZON_TRANSITIONS, label="horizon")


def assert_control_request_transition(
    current: ControlRequestStatus, new: ControlRequestStatus
) -> None:
    """Gate a ``control_requests`` status change (§46, §47)."""
    assert_transition(current, new, CONTROL_REQUEST_TRANSITIONS, label="control_request")


def is_terminal(status: object, allowed: Mapping) -> bool:
    """Whether ``status`` has no outgoing edges."""
    return not allowed.get(status, frozenset())


TERMINAL_REQUEST_STATUSES: frozenset[TradeRequestStatus] = frozenset(
    s for s in TradeRequestStatus if is_terminal(s, TRADE_REQUEST_TRANSITIONS)
)
