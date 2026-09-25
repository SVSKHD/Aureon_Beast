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


class HistorySource(StrEnum):
    """Where a measured cohort's history came from (11C, F-9).

    A readout built on replayed fixture bars and one built on bars a broker served are the
    same arithmetic over incomparable data, and until 11C nothing on the stored assessment
    said which. A generated random walk has the distribution its generator was given, so a
    hit rate measured over one is a statement about ``scripts/gen_fixtures.py``.

    ``UNKNOWN`` exists for an assessment written before this field did, and is not the same
    as SYNTHETIC: "nobody recorded it" and "we know it was generated" are different, and
    collapsing them would quietly relabel old readouts.
    """

    SYNTHETIC = "synthetic"
    REAL = "real"
    MIXED = "mixed"
    UNKNOWN = "unknown"

    @property
    def is_evidence(self) -> bool:
        """Only a wholly real cohort is evidence about the instrument."""
        return self is HistorySource.REAL


class MtfAlignment(StrEnum):
    """Whether the higher timeframes agree with a detection's direction (11D).

    Three answers and no fourth. ``MIXED`` covers both "some agree and some do not" and
    "nobody has a view", deliberately: a timeframe with too few bars to seed an EMA is an
    absence of evidence, and giving that its own value would invite treating it as a weak
    ALIGNED. The reasoning lives in ``aureon/engine/mtf.py``.

    Recorded, never gated on. Whether alignment predicts anything is a question for the
    evaluation rules; building a filter on the assumption that it does would be a threshold
    nobody researched.
    """

    ALIGNED = "aligned"
    MIXED = "mixed"
    AGAINST = "against"


class SleepPhase(StrEnum):
    """Where a service is in the weekly sleep cycle (11B).

    Lives here rather than beside the state machine in ``aureon.services.sleep_cycle``
    because ``SystemState`` publishes it and models may not import services. The machine
    that produces it, and the reasoning behind each value, are in that module.
    """

    #: Normal operation.
    AWAKE = "awake"
    #: Every symbol reads CLOSED, but the confirmation window has not elapsed.
    CLOSING = "closing"
    #: Confirmed closed. Loops parked, heartbeat slow, process alive.
    ASLEEP = "asleep"
    #: Still closed, but the open is imminent: loops run again so the open finds us ready.
    WAKING = "waking"


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
    #: 13 C-1. The send's result is not known: the call raised, timed out, or the lease
    #: expired while EXECUTING. The request is NOT failed -- failing it would invite a
    #: resend and a second position for one intent. It is reconciled against MT5, which is
    #: broker truth, and only then does it reach a real outcome.
    #:
    #: Exactly ONE new status, and it is in this enum rather than in Discord: a state a
    #: renderer invented would be a state no transition table could check, and
    #: ``assert_transition`` is the gate every status write passes through (CLAUDE.md).
    RECONCILING = "reconciling"
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


class AccountMode(StrEnum):
    """Which kind of money the terminal is logged into (11A, F-3).

    MT5 reports this as ``account_info().trade_mode``: 0 demo, 1 contest, 2 real. It is
    read and carried as a NAME rather than compared as an integer at the call sites, because
    "2" appearing in a conditional is the least reviewable possible way to express "this is
    somebody's savings".

    ``UNKNOWN`` is a real answer and the one the guards must treat as dangerous: a terminal
    that will not say what it is logged into has not said it is a demo.
    """

    DEMO = "demo"
    CONTEST = "contest"
    REAL = "real"
    UNKNOWN = "unknown"

    @classmethod
    def from_trade_mode(cls, value: object) -> AccountMode:
        """Map MT5's integer, treating anything unrecognised as UNKNOWN.

        Deliberately not ``cls(value)`` with a default of DEMO: a broker that starts
        reporting 3 for something new would otherwise be read as a demo account, which is
        the one direction this mapping must never fail in.
        """
        mapping = {0: cls.DEMO, 1: cls.CONTEST, 2: cls.REAL}
        try:
            return mapping.get(int(value), cls.UNKNOWN)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return cls.UNKNOWN

    @property
    def is_real_money(self) -> bool:
        """True for anything that is not demonstrably a practice account.

        UNKNOWN counts as real money. The cost of being wrong in this direction is a
        refused order; the cost of being wrong in the other is an order on somebody's
        savings placed by a system whose own tests have never run against real fills.
        """
        return self in {AccountMode.REAL, AccountMode.UNKNOWN}


class FailureCode(StrEnum):
    """Why a request failed (§56, §57, §41, §78).

    Carried alongside ``FAILED`` rather than expanded into separate statuses
    (decision 2). Every guard rule in Phase 4 owns one of these.
    """

    # Operator / settings gates (§57)
    TRADING_DISABLED = "trading_disabled"
    NOT_AUTHORIZED = "not_authorized"
    #: The terminal is logged into a real-money account and nobody has said that is
    #: intended (11A, F-3). Separate from TRADING_DISABLED because the two need different
    #: remedies: one is a switch a human flips in Discord, the other is an environment
    #: variable on the box, and telling an operator to flip the wrong one wastes the
    #: minutes in which they could have noticed which terminal is open.
    LIVE_EXECUTION_NOT_ALLOWED = "live_execution_not_allowed"

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


class PriceAlertStatus(StrEnum):
    """Lifecycle of a ``/remind price`` alert (9C).

    ``FIRED`` is terminal on purpose: an alert answers "tell me when price reaches X" once.
    Re-arming is a new alert, so the record of what was asked for, and when it was answered,
    stays a single immutable fact rather than a counter nobody can reconstruct.
    """

    ARMED = "armed"
    FIRED = "fired"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class NotificationStatus(StrEnum):
    """Whether a notification reached Discord (9C).

    ``FAILED`` is recorded rather than retried. The document exists to make the send
    **exactly once**, and a retry loop over a channel that is rejecting messages would
    either duplicate the post or hide the outage; an operator reading `/status` should see
    the failure instead.
    """

    SENT = "sent"
    FAILED = "failed"


class NotificationKind(StrEnum):
    DETECTION = "detection"
    ALERT = "alert"
    #: 12 T-11. One document per SETUP, not per transition: the card is edited in place as the
    #: setup advances, so "have we said anything about this setup" is one question with one
    #: answer, and the document carries the message id that answer needs.
    SETUP = "setup"


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


class ThresholdUnit(StrEnum):
    """What a rule's thresholds are measured in (§21).

    ``POINTS`` is the broker's smallest price increment -- meaningful only once you
    know the symbol's ``point``. ``PRICE`` is the quote currency, which is what a human
    actually reasons in: "did it move $5?" is a question with a stable meaning, where
    "did it move 5 points?" silently means $0.05 on gold and $5 on an index.

    The distinction is not cosmetic. ``EMA_OUTCOME_V1`` specified 3-20 POINTS, which at
    ``point=0.01`` is $0.03-$0.20 -- smaller than a single XAUUSD M5 candle, so every
    threshold was reached almost always and the resulting 100% columns measured the
    scale rather than the strategy.
    """

    POINTS = "points"
    PRICE = "price"


class ReferencePrice(StrEnum):
    """Which price a horizon measures from (§21)."""

    CLOSE = "close"
    NEXT_OPEN = "next_open"


class TrendBias(StrEnum):
    """What the last N closed candles did, summarised (9D).

    ``SIDEWAYS`` is a real answer, not a fallback for "unsure": a window whose evidence
    points both ways is a market that is not trending, and a readout that rounded it to the
    nearer of bullish/bearish would be inventing a direction out of a tie.
    """

    BULLISH = "bullish"
    BEARISH = "bearish"
    SIDEWAYS = "sideways"


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
            # C-1. The send's outcome is uncertain, or the lease expired mid-flight.
            TradeRequestStatus.RECONCILING,
        }
    ),
    # C-1. What reconciliation against MT5 can conclude. FAILED is deliberately NOT here:
    # by this point a send has been attempted and its result is unknown, and "failed" is a
    # claim that no order exists -- which is precisely what nobody knows yet.
    # FAILED_RECONCILIATION is the honest terminal state for "MT5 could not tell us".
    TradeRequestStatus.RECONCILING: frozenset(
        {
            TradeRequestStatus.FILLED,
            TradeRequestStatus.PARTIALLY_FILLED,
            TradeRequestStatus.PENDING,
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


PRICE_ALERT_TRANSITIONS: Mapping[PriceAlertStatus, frozenset[PriceAlertStatus]] = {
    # An armed alert can be answered, withdrawn, or time out. Nothing leaves the other
    # three: an alert that has fired is a fact about a moment, not a switch.
    PriceAlertStatus.ARMED: frozenset(
        {
            PriceAlertStatus.FIRED,
            PriceAlertStatus.CANCELLED,
            PriceAlertStatus.EXPIRED,
        }
    ),
    PriceAlertStatus.FIRED: frozenset(),
    PriceAlertStatus.CANCELLED: frozenset(),
    PriceAlertStatus.EXPIRED: frozenset(),
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


def assert_price_alert_transition(
    current: PriceAlertStatus, new: PriceAlertStatus
) -> None:
    """Gate a ``/remind`` alert's status change (9C)."""
    assert_transition(current, new, PRICE_ALERT_TRANSITIONS, label="price_alert")


def is_terminal(status: object, allowed: Mapping) -> bool:
    """Whether ``status`` has no outgoing edges."""
    return not allowed.get(status, frozenset())


TERMINAL_REQUEST_STATUSES: frozenset[TradeRequestStatus] = frozenset(
    s for s in TradeRequestStatus if is_terminal(s, TRADE_REQUEST_TRANSITIONS)
)

#: Derived from the table rather than listed by hand, so adding a terminal state
#: protects it automatically instead of waiting for someone to remember this set.
TERMINAL_TRADE_STATUSES: frozenset[TradeStatus] = frozenset(
    s for s in TradeStatus if is_terminal(s, TRADE_TRANSITIONS)
)

TERMINAL_CONTROL_STATUSES: frozenset[ControlRequestStatus] = frozenset(
    s for s in ControlRequestStatus if is_terminal(s, CONTROL_REQUEST_TRANSITIONS)
)

TERMINAL_ALERT_STATUSES: frozenset[PriceAlertStatus] = frozenset(
    s for s in PriceAlertStatus if is_terminal(s, PRICE_ALERT_TRANSITIONS)
)


# ── Setups (12, T-6) ──────────────────────────────────────────────────────────


class SetupFamily(StrEnum):
    """The four shapes a setup can be, and no fifth (12, T-7).

    A closed set, for the reason the ops register's names are closed: an open vocabulary grows
    near-duplicates that no runbook can document and no review can group by. Each family has its
    own lifecycle, its own invalidation rule and its own expiry, all of them in
    ``aureon/config/symbol_tuning.py`` and versioned.

    The names describe STRUCTURE, not a direction and not an instruction. ``direction_context``
    carries the bias separately, and nothing in the system turns either into an order.
    """

    #: Proximity to a tracked level, a sweep of it, a reclaim, then confirmation.
    LIQUIDITY_REVERSAL = "liquidity_reversal"
    #: A break of a session or day extreme, or of a value-area edge, that is then accepted.
    BREAKOUT_ACCEPTANCE = "breakout_acceptance"
    #: An established trend pulling back into its EMA zone and continuing, or not.
    TREND_PULLBACK = "trend_pullback"
    #: The EMA pair narrowing, the fast slope turning, then a cross.
    MOMENTUM_TRANSITION = "momentum_transition"


class DirectionContext(StrEnum):
    """Which way a setup is biased, phrased as CONTEXT and never as an order.

    BULLISH/BEARISH/NEUTRAL, deliberately not BUY/SELL. The difference is not cosmetic: a card
    that says BUY has told a human what to do, and the one thing this system must never do is
    decide. ``NEUTRAL`` is a real answer for a setup whose structure is present and whose
    direction is not yet resolved -- a level being tested from both sides, for instance.

    A test asserts BUY and SELL appear nowhere in the setup models or their renderers.
    """

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class SetupState(StrEnum):
    """Where a setup is in its life (12, T-6).

    Read in order: a setup is OBSERVING when its preconditions hold, WATCH when something has
    happened at the level, DEVELOPING while the reaction builds, CONFIRMED when the confirming
    event lands, and then PULLBACK/CONTINUATION as it either holds or does not.

    ``FAKEOUT_RISK`` is not a failure state. It is "the confirming move has been given back and
    we do not yet know whether that is noise", reachable from CONFIRMED and PULLBACK and leading
    either back to CONFIRMED or on to INVALIDATED. Collapsing it into INVALIDATED would throw
    away the distinction between a setup that failed and one that wobbled, which is exactly the
    distinction a review wants to measure.
    """

    OBSERVING = "observing"
    WATCH = "watch"
    DEVELOPING = "developing"
    CONFIRMED = "confirmed"
    PULLBACK = "pullback"
    CONTINUATION = "continuation"
    FAKEOUT_RISK = "fakeout_risk"
    COMPLETED = "completed"
    INVALIDATED = "invalidated"


class SetupEventType(StrEnum):
    """Why a setup moved, or what was seen while it did not (12, T-6, T-8).

    Two kinds in one enum, on purpose. The ``WATCH_*`` values are descriptive: they record that
    something happened near a level and do NOT change the state, so a setup can accumulate a
    dozen of them while sitting in WATCH. The rest are the lifecycle's own transitions.

    Keeping them together means one sub-collection holds the whole story of a setup in time
    order, which is what a card and a review both want to read. Splitting them would mean
    interleaving two queries to answer "what happened to this setup".
    """

    # ── lifecycle ─────────────────────────────────────────────────────────────
    OPENED = "opened"
    WATCH_STARTED = "watch_started"
    DEVELOPING = "developing"
    CONFIRMED = "confirmed"
    PULLBACK_STARTED = "pullback_started"
    PULLBACK_TO_EMA20 = "pullback_to_ema20"
    PULLBACK_TO_EMA_ZONE = "pullback_to_ema_zone"
    EMA20_REJECTION = "ema20_rejection"
    EMA50_TEST = "ema50_test"
    CONTINUATION = "continuation"
    FAKEOUT_RISK = "fakeout_risk"
    RECLAIMED = "reclaimed"
    COMPLETED = "completed"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"

    # ── descriptive, context only (T-8) ───────────────────────────────────────
    PROXIMITY_LIQUIDITY_LEVEL = "proximity_liquidity_level"
    PROXIMITY_SESSION_EXTREME = "proximity_session_extreme"
    PROXIMITY_PREV_DAY_EXTREME = "proximity_prev_day_extreme"
    PROXIMITY_POC = "proximity_poc"
    PROXIMITY_VAH = "proximity_vah"
    PROXIMITY_VAL = "proximity_val"
    EMA_GAP_NARROWING = "ema_gap_narrowing"
    EMA_FAST_SLOPE_CHANGE = "ema_fast_slope_change"
    RSI_MOMENTUM_TURN = "rsi_momentum_turn"
    TICK_VOLUME_EXPANSION = "tick_volume_expansion"
    REPEATED_LEVEL_TEST = "repeated_level_test"
    HIGH_TICK_VOLUME_SWEEP = "high_tick_volume_sweep"
    HIGH_TICK_VOLUME_REJECTION = "high_tick_volume_rejection"
    VOLUME_EXPANSION_AT_LEVEL = "volume_expansion_at_level"
    PROFILE_RECLAIM = "profile_reclaim"
    PROFILE_REJECTION = "profile_rejection"
    BREAKOUT_PRESSURE = "breakout_pressure"
    FAVOURABLE_MOVE_6_TRACKING = "favourable_move_6_tracking"
    FAVOURABLE_MOVE_6_REACHED = "favourable_move_6_reached"
    FAVOURABLE_MOVE_20_REACHED = "favourable_move_20_reached"
    FAVOURABLE_MOVE_40_REACHED = "favourable_move_40_reached"

    @property
    def is_watch_event(self) -> bool:
        """True for the descriptive events, which never change a setup's state."""
        return self in WATCH_EVENT_TYPES


#: The descriptive events (T-8). Derived from the name prefix would have been shorter and
#: wrong: ``WATCH_STARTED`` is a lifecycle transition whose name begins the same way, and
#: ``PROFILE_RECLAIM`` is descriptive with no prefix at all. An explicit set is the only
#: version of this that cannot be broken by renaming a value.
WATCH_EVENT_TYPES: frozenset[SetupEventType] = frozenset(
    {
        SetupEventType.PROXIMITY_LIQUIDITY_LEVEL,
        SetupEventType.PROXIMITY_SESSION_EXTREME,
        SetupEventType.PROXIMITY_PREV_DAY_EXTREME,
        SetupEventType.PROXIMITY_POC,
        SetupEventType.PROXIMITY_VAH,
        SetupEventType.PROXIMITY_VAL,
        SetupEventType.EMA_GAP_NARROWING,
        SetupEventType.EMA_FAST_SLOPE_CHANGE,
        SetupEventType.RSI_MOMENTUM_TURN,
        SetupEventType.TICK_VOLUME_EXPANSION,
        SetupEventType.REPEATED_LEVEL_TEST,
        SetupEventType.HIGH_TICK_VOLUME_SWEEP,
        SetupEventType.HIGH_TICK_VOLUME_REJECTION,
        SetupEventType.VOLUME_EXPANSION_AT_LEVEL,
        SetupEventType.PROFILE_RECLAIM,
        SetupEventType.PROFILE_REJECTION,
        SetupEventType.BREAKOUT_PRESSURE,
        SetupEventType.FAVOURABLE_MOVE_6_TRACKING,
        SetupEventType.FAVOURABLE_MOVE_6_REACHED,
        SetupEventType.FAVOURABLE_MOVE_20_REACHED,
        SetupEventType.FAVOURABLE_MOVE_40_REACHED,
    }
)


class SetupAnchorKind(StrEnum):
    """What a setup is anchored TO (12, T-6).

    The anchor is half the identity: two setups at the same level on the same day in the same
    direction are the same setup, and a new anchor opens a new one. So the kinds are a closed
    set, and a price alone is never the anchor -- "the level at 2412.5" and "the session high,
    which happens to be at 2412.5" become different things the moment the session high moves.
    """

    LIQUIDITY_LEVEL = "liquidity_level"
    SESSION_EXTREME = "session_extreme"
    PREV_DAY_EXTREME = "prev_day_extreme"
    VALUE_AREA_EDGE = "value_area_edge"
    POC = "poc"
    EMA_ZONE = "ema_zone"


#: The setup lifecycle (12, T-6).
#:
#: The shape in one sentence: forwards through the sequence, INVALIDATED reachable from
#: anything that is not already terminal, and FAKEOUT_RISK a two-way door off CONFIRMED and
#: PULLBACK.
#:
#: Three edges are worth their own note, because each one was a decision:
#:
#: * ``CONFIRMED -> COMPLETED`` exists without a PULLBACK in between. A setup that runs straight
#:   to its measured objective never pulls back, and a machine that required the step would have
#:   to invent one.
#: * ``CONTINUATION -> PULLBACK`` exists. A trend that continues, pulls back and continues again
#:   is one setup, not three; forcing a new setup per leg would fragment the population that a
#:   review has to count.
#: * ``OBSERVING -> INVALIDATED`` exists. A setup whose preconditions stop holding before
#:   anything happened at the level is invalidated, not deleted: the fact that it was there and
#:   came to nothing is data, and expiry is recorded the same way with reason ``expired``.
SETUP_TRANSITIONS: dict[SetupState, frozenset[SetupState]] = {
    SetupState.OBSERVING: frozenset(
        {SetupState.WATCH, SetupState.DEVELOPING, SetupState.INVALIDATED}
    ),
    SetupState.WATCH: frozenset(
        {SetupState.DEVELOPING, SetupState.INVALIDATED}
    ),
    SetupState.DEVELOPING: frozenset(
        {SetupState.CONFIRMED, SetupState.INVALIDATED}
    ),
    SetupState.CONFIRMED: frozenset(
        {
            SetupState.PULLBACK,
            SetupState.CONTINUATION,
            SetupState.FAKEOUT_RISK,
            SetupState.COMPLETED,
            SetupState.INVALIDATED,
        }
    ),
    SetupState.PULLBACK: frozenset(
        {
            SetupState.CONTINUATION,
            SetupState.FAKEOUT_RISK,
            SetupState.COMPLETED,
            SetupState.INVALIDATED,
        }
    ),
    SetupState.CONTINUATION: frozenset(
        {SetupState.PULLBACK, SetupState.COMPLETED, SetupState.INVALIDATED}
    ),
    SetupState.FAKEOUT_RISK: frozenset(
        {SetupState.CONFIRMED, SetupState.INVALIDATED}
    ),
    SetupState.COMPLETED: frozenset(),
    SetupState.INVALIDATED: frozenset(),
}

TERMINAL_SETUP_STATES: frozenset[SetupState] = frozenset(
    s for s in SetupState if is_terminal(s, SETUP_TRANSITIONS)
)


def assert_setup_transition(current: SetupState, new: SetupState) -> None:
    """Gate a ``setups`` state change (12, T-6)."""
    assert_transition(current, new, SETUP_TRANSITIONS, label="setup")
