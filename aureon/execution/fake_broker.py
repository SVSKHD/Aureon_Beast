"""A scriptable broker for failure injection (§80, §81).

The point of this class is to make the dangerous cases *reachable*. On a real broker
you cannot arrange to crash between sending an order and learning its result, so the
one failure that can produce a double trade is exactly the one you can never test. Here
it is a flag.

## The ledger is the verdict

``ledger`` records every execution the broker actually accepted, and it deliberately
lives **outside** the executor's lifetime. A "restart" in the tests builds a new
executor around the *same* FakeBroker, so the ledger survives -- which is what lets a
test assert "exactly one execution across two process lifetimes". If the ledger were
reset on restart, every crash test would trivially pass.

## Crash hooks

* ``crash_after_send``   -- the order IS accepted, then the call raises. This is the
  dangerous one: the broker has the order, the executor never learned it. Anything that
  retries here produces two trades.
* ``crash_before_result`` -- the call raises *before* anything is accepted. Safe, but
  indistinguishable from the above without reconciliation, which is the whole point.
* ``duplicate_result``   -- the broker reports success twice.
* ``partial_fill``       -- only part of the volume fills.
* ``delay_result``       -- the result arrives after the lease would have expired.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aureon.execution.broker_interface import BrokerError, BrokerInterface
from aureon.models.base import to_utc, utc_now
from aureon.models.broker import AccountInfo, BrokerDeal, BrokerOrder, BrokerPosition
from aureon.models.enums import DealEntry, Direction, FailureCode, FillingMode, OrderType
from aureon.models.market import QuoteSnapshot, SymbolInfo
from aureon.models.trade import BrokerOrderRequest, BrokerOrderResult

DEFAULT_SYMBOL_INFO = SymbolInfo(
    symbol="XAUUSD",
    point=0.01,
    digits=2,
    volume_min=0.01,
    volume_max=50.0,
    volume_step=0.01,
    stops_level=50,
    filling_modes=(FillingMode.FOK, FillingMode.IOC),
    trade_mode="full",
    spread=30,
)


@dataclass
class LedgerEntry:
    """One execution the broker actually accepted."""

    kind: str  # "market" | "pending" | "cancel" | "close"
    symbol: str
    order_type: OrderType | None
    volume: float
    comment: str
    magic: int
    order_ticket: int | None = None
    position_id: int | None = None
    price: float | None = None
    at: datetime = field(default_factory=utc_now)


@dataclass
class Behaviour:
    """What the broker should do on the next send. Consumed once."""

    reject: FailureCode | None = None
    crash_after_send: bool = False
    crash_before_result: bool = False
    duplicate_result: bool = False
    delay_result_seconds: float = 0.0
    partial_fill_volume: float | None = None
    requote: bool = False


class FakeBroker(BrokerInterface):
    """An in-memory broker with a scriptable behaviour queue and a durable ledger."""

    def __init__(
        self,
        *,
        symbol_info: SymbolInfo | None = None,
        bid: float = 2400.00,
        ask: float = 2400.30,
        balance: float = 100_000.0,
        margin_free: float = 100_000.0,
    ) -> None:
        self._info = symbol_info or DEFAULT_SYMBOL_INFO
        self.bid = bid
        self.ask = ask
        self.balance = balance
        self.margin_free = margin_free

        # Survives a simulated restart: the tests keep the broker and rebuild the
        # executor around it.
        self.ledger: list[LedgerEntry] = []

        self._behaviours: list[Behaviour] = []
        self._positions: dict[int, BrokerPosition] = {}
        self._orders: dict[int, BrokerOrder] = {}
        self._deals: list[BrokerDeal] = []
        self._tickets = itertools.count(10_001)
        self._positions_seq = itertools.count(50_001)
        self._deals_seq = itertools.count(90_001)
        self._lock = threading.RLock()
        self.calls: list[str] = []

    # ── Scripting ─────────────────────────────────────────────────────────────

    def script(self, *behaviours: Behaviour) -> FakeBroker:
        """Queue behaviours, applied one per send in order."""
        self._behaviours.extend(behaviours)
        return self

    def _next_behaviour(self) -> Behaviour:
        with self._lock:
            return self._behaviours.pop(0) if self._behaviours else Behaviour()

    def set_quote(self, *, bid: float, ask: float) -> None:
        self.bid, self.ask = bid, ask

    def set_spread_points(self, points: float) -> None:
        """Widen the spread around the mid, for the spread guard (§41)."""
        mid = (self.bid + self.ask) / 2
        half = points * self._info.point / 2
        self.bid, self.ask = mid - half, mid + half

    # ── Account and symbol ────────────────────────────────────────────────────

    def account_info(self) -> AccountInfo:
        self.calls.append("account_info")
        return AccountInfo(
            login=5_000_001,
            balance=self.balance,
            equity=self.balance,
            margin_free=self.margin_free,
            server="FakeBroker-Demo",
        )

    def symbol_info(self, symbol: str) -> SymbolInfo:
        self.calls.append("symbol_info")
        return self._info.model_copy(update={"symbol": symbol})

    def quote(self, symbol: str) -> QuoteSnapshot:
        self.calls.append("quote")
        return QuoteSnapshot(
            symbol=symbol, bid=self.bid, ask=self.ask, point=self._info.point
        )

    # ── Sending ───────────────────────────────────────────────────────────────

    def send_market_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        self.calls.append("send_market_order")
        return self._send(request, kind="market")

    def send_pending_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        self.calls.append("send_pending_order")
        return self._send(request, kind="pending")

    def _send(self, request: BrokerOrderRequest, *, kind: str) -> BrokerOrderResult:
        behaviour = self._next_behaviour()

        if behaviour.crash_before_result:
            # Nothing accepted. Indistinguishable from crash_after_send to the caller,
            # which is exactly why reconciliation must look rather than assume.
            raise BrokerError("connection lost before the order was placed")

        if behaviour.reject is not None:
            return BrokerOrderResult(
                ok=False,
                retcode=10_004 if behaviour.requote else 10_006,
                retcode_name=(
                    "TRADE_RETCODE_REQUOTE" if behaviour.requote else "TRADE_RETCODE_REJECT"
                ),
                failure_code=behaviour.reject,
                message=f"rejected: {behaviour.reject.value}",
                raw={"scripted": True},
            )

        with self._lock:
            ticket = next(self._tickets)
            filled = behaviour.partial_fill_volume or request.volume
            price = request.price if kind == "pending" else (
                self.ask if request.direction is Direction.BUY else self.bid
            )

            if kind == "pending":
                self._orders[ticket] = BrokerOrder(
                    order_ticket=ticket,
                    symbol=request.symbol,
                    order_type=request.order_type,
                    volume=request.volume,
                    price=request.price or price,
                    sl=request.sl,
                    tp=request.tp,
                    placed_at=utc_now(),
                    magic=request.magic,
                    comment=request.comment,
                )
                position_id = None
            else:
                position_id = next(self._positions_seq)
                self._positions[position_id] = BrokerPosition(
                    position_id=position_id,
                    symbol=request.symbol,
                    direction=request.direction,
                    volume=filled,
                    open_price=price,
                    open_time=utc_now(),
                    sl=request.sl,
                    tp=request.tp,
                    magic=request.magic,
                    comment=request.comment,
                )
                self._deals.append(
                    BrokerDeal(
                        deal_id=next(self._deals_seq),
                        order_ticket=ticket,
                        position_id=position_id,
                        symbol=request.symbol,
                        direction=request.direction,
                        entry=DealEntry.IN,
                        volume=filled,
                        price=price,
                        executed_at=utc_now(),
                        magic=request.magic,
                        comment=request.comment,
                        reason="expert",
                    )
                )

            # Recorded BEFORE any crash hook: the broker has accepted it, so the ledger
            # must say so even if the caller never finds out.
            self.ledger.append(
                LedgerEntry(
                    kind=kind,
                    symbol=request.symbol,
                    order_type=request.order_type,
                    volume=filled,
                    comment=request.comment,
                    magic=request.magic,
                    order_ticket=ticket,
                    position_id=position_id,
                    price=price,
                )
            )
            if behaviour.duplicate_result:
                self.ledger.append(self.ledger[-1])

        if behaviour.crash_after_send:
            # THE dangerous case: accepted above, never reported. A retry here is how
            # one confirmed intention becomes two real trades.
            raise BrokerError("connection lost after the order was placed")

        if behaviour.delay_result_seconds:
            # No sleeping: the tests move a clock instead. The delay is reported so a
            # caller can assert the lease would have expired.
            pass

        return BrokerOrderResult(
            ok=True,
            retcode=10_009,
            retcode_name="TRADE_RETCODE_DONE",
            order_ticket=ticket,
            deal_ids=tuple(d.deal_id for d in self._deals if d.order_ticket == ticket),
            position_id=position_id,
            fill_price=price,
            filled_volume=filled,
            raw={"scripted": True, "delayed": behaviour.delay_result_seconds or None},
        )

    # ── Cancel and close ──────────────────────────────────────────────────────

    def cancel_order(self, ticket: int) -> BrokerOrderResult:
        self.calls.append("cancel_order")
        behaviour = self._next_behaviour()
        if behaviour.crash_before_result:
            raise BrokerError("connection lost during cancel")
        with self._lock:
            order = self._orders.pop(ticket, None)
        if order is None:
            # Already gone -- usually because it filled. Not an error to retry; the
            # caller reconciles (§46, scenario G).
            return BrokerOrderResult(
                ok=False,
                retcode=10_013,
                retcode_name="TRADE_RETCODE_INVALID",
                failure_code=FailureCode.BROKER_REJECTED,
                message=f"order {ticket} is not resting (filled or already cancelled)",
            )
        self.ledger.append(
            LedgerEntry(
                kind="cancel",
                symbol=order.symbol,
                order_type=order.order_type,
                volume=order.volume,
                comment=order.comment or "",
                magic=order.magic or 0,
                order_ticket=ticket,
            )
        )
        return BrokerOrderResult(ok=True, retcode=10_009, order_ticket=ticket)

    def close_position(
        self, position_id: int, volume: float | None = None
    ) -> BrokerOrderResult:
        self.calls.append("close_position")
        behaviour = self._next_behaviour()
        if behaviour.crash_before_result:
            raise BrokerError("connection lost during close")
        with self._lock:
            position = self._positions.get(position_id)
            if position is None:
                # Already closed. Retrying must not open anything (scenario H).
                return BrokerOrderResult(
                    ok=False,
                    retcode=10_013,
                    retcode_name="TRADE_RETCODE_INVALID",
                    failure_code=FailureCode.BROKER_REJECTED,
                    message=f"position {position_id} is not open",
                )
            closing = min(volume or position.volume, position.volume)
            exit_price = self.bid if position.direction is Direction.BUY else self.ask
            remaining = round(position.volume - closing, 8)
            if remaining <= 0:
                del self._positions[position_id]
            else:
                self._positions[position_id] = position.model_copy(
                    update={"volume": remaining}
                )
            deal = BrokerDeal(
                deal_id=next(self._deals_seq),
                order_ticket=next(self._tickets),
                position_id=position_id,
                symbol=position.symbol,
                direction=(
                    Direction.SELL if position.direction is Direction.BUY else Direction.BUY
                ),
                entry=DealEntry.OUT,
                volume=closing,
                price=exit_price,
                executed_at=utc_now(),
                profit=(exit_price - position.open_price)
                * closing
                * position.direction.sign,
                magic=position.magic,
                reason="client",
            )
            self._deals.append(deal)
            self.ledger.append(
                LedgerEntry(
                    kind="close",
                    symbol=position.symbol,
                    order_type=None,
                    volume=closing,
                    comment=position.comment or "",
                    magic=position.magic or 0,
                    position_id=position_id,
                    price=exit_price,
                )
            )
        return BrokerOrderResult(
            ok=True,
            retcode=10_009,
            position_id=position_id,
            deal_ids=(deal.deal_id,),
            fill_price=exit_price,
            filled_volume=closing,
        )

    # ── Reading ───────────────────────────────────────────────────────────────

    def open_positions(self) -> list[BrokerPosition]:
        self.calls.append("open_positions")
        with self._lock:
            return list(self._positions.values())

    def pending_orders(self) -> list[BrokerOrder]:
        self.calls.append("pending_orders")
        with self._lock:
            return list(self._orders.values())

    def orders_history(self, from_utc: datetime, to_utc_: datetime) -> list[BrokerOrder]:
        self.calls.append("orders_history")
        start, end = to_utc(from_utc), to_utc(to_utc_)
        with self._lock:
            return [
                o
                for o in self._orders.values()
                if o.placed_at is not None and start <= o.placed_at <= end
            ]

    def deals_history(self, from_utc: datetime, to_utc_: datetime) -> list[BrokerDeal]:
        self.calls.append("deals_history")
        start, end = to_utc(from_utc), to_utc(to_utc_)
        with self._lock:
            return [d for d in self._deals if start <= d.executed_at <= end]

    # ── Test helpers ──────────────────────────────────────────────────────────

    def fill_pending(self, ticket: int) -> BrokerDeal:
        """Fill a resting order, as the market would (scenario G)."""
        with self._lock:
            order = self._orders.pop(ticket)
            position_id = next(self._positions_seq)
            self._positions[position_id] = BrokerPosition(
                position_id=position_id,
                symbol=order.symbol,
                direction=order.order_type.direction,
                volume=order.volume,
                open_price=order.price,
                open_time=utc_now(),
                sl=order.sl,
                tp=order.tp,
                magic=order.magic,
                comment=order.comment,
            )
            deal = BrokerDeal(
                deal_id=next(self._deals_seq),
                order_ticket=ticket,
                position_id=position_id,
                symbol=order.symbol,
                direction=order.order_type.direction,
                entry=DealEntry.IN,
                volume=order.volume,
                price=order.price,
                executed_at=utc_now(),
                magic=order.magic,
                comment=order.comment,
                reason="expert",
            )
            self._deals.append(deal)
            return deal

    def executions_for(self, comment: str) -> list[LedgerEntry]:
        """Ledger entries that placed an order carrying this comment token.

        Cancels and closes are excluded: the exactly-once claim is about how many times
        a request *opened* something.
        """
        return [
            entry
            for entry in self.ledger
            if entry.comment == comment and entry.kind in {"market", "pending"}
        ]

    def advance_quote(self, points: float) -> None:
        shift = points * self._info.point
        self.bid += shift
        self.ask += shift

    def age_deals(self, seconds: float) -> None:
        """Shift every deal back in time, to test reconciliation windows."""
        with self._lock:
            self._deals = [
                d.model_copy(
                    update={"executed_at": d.executed_at - timedelta(seconds=seconds)}
                )
                for d in self._deals
            ]
