"""The broker abstraction (§30-§36).

Every piece of money-moving logic depends on this interface and never on MetaTrader5
(CLAUDE.md). That is what makes the failure-injection suite possible at all: the same
executor runs against a real terminal and against ``FakeBroker``, so exactly-once can
be proven by deliberately crashing at each step rather than hoped for.

## The one rule that matters most

``send_market_order`` and ``send_pending_order`` may fail in three ways, and the third
is the dangerous one:

1. the broker **rejects** the order -- safe, nothing happened;
2. the broker **accepts** it and says so -- safe, we know what happened;
3. we **never learn** the outcome (timeout, crash, connection drop) -- the order may or
   may not exist.

Case 3 must never be retried. A retry is how one confirmed intention becomes two real
trades. The contract is: an unknown result leaves the request ``EXECUTING`` and
reconciliation goes and looks. Implementations must therefore make every send
*findable afterwards* -- which is what ``magic`` and the deterministic ``comment``
token are for (§34).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from aureon.models.broker import AccountInfo, BrokerDeal, BrokerOrder, BrokerPosition
from aureon.models.market import QuoteSnapshot, SymbolInfo
from aureon.models.trade import BrokerOrderRequest, BrokerOrderResult


class BrokerError(RuntimeError):
    """A broker call failed in a way that says nothing about the order's fate.

    Deliberately distinct from a rejection: a rejection is an answer, this is the
    absence of one, and only the latter requires reconciliation.
    """


class BrokerInterface(ABC):
    """Everything Aureon needs from a broker."""

    # ── Account and symbol ────────────────────────────────────────────────────

    @abstractmethod
    def account_info(self) -> AccountInfo: ...

    @abstractmethod
    def symbol_info(self, symbol: str) -> SymbolInfo: ...

    @abstractmethod
    def quote(self, symbol: str) -> QuoteSnapshot:
        """The current bid/ask.

        Called again at execution time even though the confirmation already carried a
        quote: the guard compares against a *fresh* price, never the one a human saw
        seconds ago (§41).
        """

    # ── Placing and cancelling ────────────────────────────────────────────────

    @abstractmethod
    def send_market_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        """Place a market order.

        Raises ``BrokerError`` when the outcome is unknown. Returns a result with
        ``ok=False`` when the broker rejected it -- those two are not interchangeable.
        """

    @abstractmethod
    def send_pending_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        """Place a pending (stop or limit) order. Same failure contract as above."""

    @abstractmethod
    def cancel_order(self, ticket: int) -> BrokerOrderResult:
        """Cancel a resting order.

        May legitimately fail because the order just filled; the caller reconciles
        rather than assuming the cancel worked (§46).
        """

    @abstractmethod
    def close_position(self, position_id: int, volume: float | None = None) -> BrokerOrderResult:
        """Close a position, wholly or partly.

        ``volume=None`` closes all of it. May fail because it already closed, which is
        reconciled rather than retried (§47).
        """

    # ── Reading state ─────────────────────────────────────────────────────────

    @abstractmethod
    def open_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    def pending_orders(self) -> list[BrokerOrder]: ...

    @abstractmethod
    def orders_history(self, from_utc: datetime, to_utc: datetime) -> list[BrokerOrder]: ...

    @abstractmethod
    def deals_history(self, from_utc: datetime, to_utc: datetime) -> list[BrokerDeal]:
        """Deals in a window. The evidence reconciliation searches (§35)."""

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Establish a connection. Nothing to do for a fake."""

    def close(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release resources."""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
