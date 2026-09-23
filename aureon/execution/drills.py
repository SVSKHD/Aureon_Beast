"""Nine drills that exercise the money-moving invariants end to end (P-4).

Phases 4, 5 and 6 each have a demo-account leg: a human on a demo account, watching real
orders, confirming that the things that must not happen do not happen. That leg has been
outstanding since those phases were written, and the reason is that "try to make it place
two orders" is not a procedure anybody can repeat identically.

So these are the procedure. Each drill sets up one dangerous situation, drives the real
``ExecutionWorker`` through it, and states in advance exactly what must be true
afterwards -- including, for five of the nine, that **no order reached the broker at all**.

## They run twice, and the first run is not optional

1. Against ``FakeBroker`` and the Firestore emulator. This proves the drills themselves
   are right, and it is free. A drill that is wrong about what should happen is worse than
   no drill: it produces a green table beside a broken guard.
2. Against a real demo account and a real MT5 terminal, where four of them place real
   orders. This is the leg the phase gates ask for.

``docs/DEMO_EXECUTION_CHECKLIST.md`` is the human half.

## Why they live in ``aureon/execution`` and not beside the other operator tools

Because ``aureon/services`` is on the observer side of the architecture guard: nothing
there may import ``aureon.execution`` or so much as name ``BrokerInterface``. That rule is
what makes "a detection never creates a trade" structural rather than remembered, and the
drills are precisely the code that must hold a broker. So they sit inside the package they
exercise, where somebody looking for the execution safety proofs will find them.

## What a drill may and may not assume

* It may set up its own preconditions -- settings, quotes, scripted broker behaviour --
  because a drill that depended on ambient state would pass or fail for reasons its output
  does not record.
* It may **not** assert on internals. Every expectation is something an operator could
  check by hand: a request's status, a failure code, the number of orders at the broker,
  the comment on one of them.
* One drill needs real transaction isolation (two executors racing one request). Against a
  client that cannot provide it, that drill reports **SKIP**, never PASS: a fake that
  defines its own concurrency semantics would be testing a model of Firestore rather than
  Firestore.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aureon.models.base import utc_now
from aureon.models.enums import (
    ControlRequestKind,
    ControlRequestStatus,
    FailureCode,
    OrderType,
    TradeRequestStatus,
    TradeStatus,
)
from aureon.models.identity import comment_token
from aureon.models.market import QuoteSnapshot
from aureon.models.settings import ExecutionSettings
from aureon.models.trade import TradeRequest
from aureon.services.checks import CheckReport, CheckResult, Status

DEFAULT_SYMBOL = "XAUUSD"
DEFAULT_VOLUME = 0.10
DEFAULT_MAGIC = 770177
OPERATOR = "drill-operator"


# ── Expectations ─────────────────────────────────────────────────────────────


@dataclass
class Expectations:
    """What a drill required, and what actually happened.

    Every requirement is recorded with its observed value whether it held or not, so the
    evidence says what the system did rather than only whether a drill was happy. A table
    of "ok" with no values is not evidence.
    """

    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def require(self, what: str, held: bool, actual: object) -> None:
        self.checks.append((what, bool(held), str(actual)))

    @property
    def satisfied(self) -> bool:
        return all(held for _what, held, _actual in self.checks)

    @property
    def failures(self) -> list[str]:
        return [what for what, held, _actual in self.checks if not held]

    def summary(self) -> str:
        held = sum(1 for _w, ok, _a in self.checks if ok)
        if self.satisfied:
            return f"{held}/{len(self.checks)} expectations held"
        return (
            f"{held}/{len(self.checks)} held; failed: " + "; ".join(self.failures)
        )

    def lines(self) -> list[str]:
        width = max((len(what) for what, _ok, _a in self.checks), default=10)
        return [
            f"{'ok  ' if ok else 'FAIL'}  {what:<{width}}  actual: {actual}"
            for what, ok, actual in self.checks
        ]


# ── Context ──────────────────────────────────────────────────────────────────


@dataclass
class DrillContext:
    """Everything a drill needs, and nothing it should reach around.

    ``settings`` is a plain attribute the drills replace: the worker reads it through a
    provider on every request, which is the same path ``/trading disable`` takes, so a
    drill changing it is exercising the real mechanism rather than a test hook.
    """

    client: Any
    broker: Any
    settings: ExecutionSettings
    magic: int = DEFAULT_MAGIC
    symbol: str = DEFAULT_SYMBOL
    volume: float = DEFAULT_VOLUME
    #: False for a client whose transactions do not really isolate. The racing drill
    #: reports SKIP rather than a verdict it cannot support.
    transactions_isolated: bool = True
    now: Callable[[], datetime] = utc_now

    def __post_init__(self) -> None:
        if hasattr(self.client, "trade_requests"):
            self.requests = self.client.trade_requests
            self.controls = self.client.controls
            self.trades = self.client.trades
        else:
            # Legacy unit doubles can still supply the old repository-shaped client.
            from aureon.storage.control_request_repository import ControlRequestRepository
            from aureon.storage.trade_repository import TradeRepository
            from aureon.storage.trade_request_repository import TradeRequestRepository

            self.requests = TradeRequestRepository(self.client)
            self.controls = ControlRequestRepository(self.client)
            self.trades = TradeRepository(self.client)
        # Where this drill's own broker calls start. A real terminal cannot be handed to
        # each drill fresh, so "the broker was never asked to send" has to mean "not by
        # THIS drill" -- measured from here rather than from an empty log.
        self._calls_at_start = len(getattr(self.broker, "calls", []))

    @property
    def broker_is_scriptable(self) -> bool:
        """Whether the broker can be made to misbehave on demand.

        Three drills need it: one widens the spread, one drops the connection mid-send,
        one fills a resting order to order. A real terminal does none of those on request,
        so against one those drills SKIP -- they do not quietly test something weaker.
        """
        return all(
            hasattr(self.broker, name)
            for name in ("script", "set_spread_points", "fill_pending")
        )

    # ── Builders ──────────────────────────────────────────────────────────────

    def worker(self, *, executor_id: str | None = None, **kwargs: Any):
        from aureon.execution.execution_worker import ExecutionWorker

        return ExecutionWorker(
            self.requests,
            self.broker,
            magic=self.magic,
            settings_provider=lambda: self.settings,
            executor_id=executor_id,
            **kwargs,
        )

    def reconciler(self, *, grace_seconds: float = 0.0):
        from aureon.execution.reconciliation_service import ReconciliationService

        return ReconciliationService(
            self.requests, self.broker, magic=self.magic, grace_seconds=grace_seconds
        )

    def control_worker(self, *, executor_id: str = "drill-control"):
        from aureon.execution.control_worker import ControlWorker

        return ControlWorker(self.controls, self.broker, executor_id=executor_id)

    def quote(self) -> QuoteSnapshot:
        live = self.broker.quote(self.symbol)
        return QuoteSnapshot(
            symbol=self.symbol,
            bid=live.bid,
            ask=live.ask,
            point=live.point,
            captured_at=self.now(),
        )

    def confirmed(
        self,
        *,
        order_type: OrderType = OrderType.MARKET_BUY,
        volume: float | None = None,
        price: float | None = None,
        confirmation_ttl_seconds: float = 300.0,
        at: datetime | None = None,
    ) -> TradeRequest:
        """A request taken through the real REQUESTED → CONFIRMED path."""
        moment = at or self.now()
        request = TradeRequest(
            request_id=f"drill-{uuid.uuid4().hex[:12]}",
            symbol=self.symbol,
            order_type=order_type,
            volume=volume if volume is not None else self.volume,
            price=price,
            requested_by=OPERATOR,
            quote=self.quote(),
            deviation_points=20,
            requested_at=moment,
        )
        self.requests.create(request)
        return self.requests.confirm(
            request.request_id,
            OPERATOR,
            request.quote,
            confirmation_ttl_seconds=confirmation_ttl_seconds,
            now=moment,
        )

    def orders_for(self, request_id: str) -> list[Any]:
        """Everything the broker accepted under this request's comment token."""
        return self.broker.executions_for(comment_token(request_id))

    def sends(self) -> list[str]:
        """Send calls made since this context was built, not since the process started."""
        calls = list(getattr(self.broker, "calls", []))[self._calls_at_start :]
        return [c for c in calls if c.startswith("send_")]


# ── The drills ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Drill:
    number: int
    name: str
    proves: str
    invariant: str
    #: True when a real broker would receive a real order. The four that do are the
    #: reason the demo run needs a demo account rather than a live one.
    places_orders: bool
    run: Callable[[DrillContext], Expectations]
    needs_isolation: bool = False
    #: True when the drill has to make the broker misbehave -- widen a spread, drop a
    #: connection, fill a resting order. A real terminal will not, so against one these
    #: report SKIP with the manual procedure as the remedy.
    needs_scriptable_broker: bool = False


def drill_1_a_confirmed_request_fills_once(ctx: DrillContext) -> Expectations:
    e = Expectations()
    request = ctx.confirmed()
    final = ctx.worker().process(request.request_id)

    e.require("status is FILLED", final is not None and final.status is TradeRequestStatus.FILLED,
              final.status.value if final else "no result")
    orders = ctx.orders_for(request.request_id)
    e.require("exactly one order at the broker", len(orders) == 1, len(orders))
    if orders:
        e.require("the order carries this request's comment token",
                  orders[0].comment == comment_token(request.request_id), orders[0].comment)
        e.require("the order carries Aureon's magic number",
                  orders[0].magic == ctx.magic, orders[0].magic)
    if final is not None:
        e.require("a ticket was recorded on the request",
                  final.order_ticket is not None, final.order_ticket)
        e.require("a fill price was recorded",
                  final.fill_price is not None, final.fill_price)
    return e


def drill_2_the_kill_switch_stops_a_confirmed_request(ctx: DrillContext) -> Expectations:
    e = Expectations()
    ctx.settings = replace_settings(ctx.settings, trading_enabled=False)
    request = ctx.confirmed()
    final = ctx.worker().process(request.request_id)

    e.require("status is FAILED", final is not None and final.status is TradeRequestStatus.FAILED,
              final.status.value if final else "no result")
    e.require("failure_code is trading_disabled",
              final is not None and final.failure_code is FailureCode.TRADING_DISABLED,
              final.failure_code.value if final and final.failure_code else "none")
    e.require("NO order reached the broker", not ctx.orders_for(request.request_id),
              len(ctx.orders_for(request.request_id)))
    e.require("the broker was never asked to send", not ctx.sends(), ctx.sends())
    return e


def drill_3_two_executors_place_one_order(ctx: DrillContext) -> Expectations:
    """The exactly-once property, raced rather than reasoned about.

    Two workers, one request, started together. Firestore's read-then-conditional-write
    must let exactly one claim it; the loser's ``process`` returns ``None``, which is a
    normal outcome and not an error.
    """
    e = Expectations()
    request = ctx.confirmed()
    workers = [ctx.worker(executor_id="drill-a"), ctx.worker(executor_id="drill-b")]
    results: list[Any] = [None, None]
    barrier = threading.Barrier(len(workers))

    def attempt(index: int) -> None:
        barrier.wait(timeout=10)
        results[index] = workers[index].process(request.request_id)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(len(workers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    orders = ctx.orders_for(request.request_id)
    winners = [r for r in results if r is not None]
    e.require("exactly one order at the broker", len(orders) == 1, len(orders))
    e.require("exactly one executor resolved it", len(winners) == 1, len(winners))
    e.require("the survivor is FILLED",
              bool(winners) and winners[0].status is TradeRequestStatus.FILLED,
              winners[0].status.value if winners else "none")
    final = ctx.requests.get(request.request_id)
    e.require("one executor owns the request",
              final is not None and final.executor_instance_id in {"drill-a", "drill-b"},
              final.executor_instance_id if final else "missing")
    return e


def drill_4_an_expired_confirmation_never_sends(ctx: DrillContext) -> Expectations:
    """A stale confirmation is refused at the CLAIM, not by the guard.

    Worth knowing which, because they are different strengths of the same promise. The
    guard runs after a request has been claimed; this one never gets claimed at all. The
    FAILED_STALE transition is written **inside the claim's own transaction**, so the
    request cannot be picked up seconds later at a price the human never saw -- and
    ``process`` treats the refusal as the ordinary outcome it is and returns nothing.
    """
    e = Expectations()
    confirmed_at = ctx.now()
    request = ctx.confirmed(confirmation_ttl_seconds=60.0, at=confirmed_at)
    # Ten minutes later: the human who pressed confirm is long gone, and the price they
    # saw is not the price we would trade at.
    late = confirmed_at + timedelta(minutes=10)
    outcome = ctx.worker().process(request.request_id, now=late)

    e.require("the executor claimed nothing", outcome is None, outcome)
    final = ctx.requests.get(request.request_id)
    e.require("status is FAILED_STALE",
              final is not None and final.status is TradeRequestStatus.FAILED_STALE,
              final.status.value if final else "missing")
    e.require("failure_code is confirmation_expired",
              final is not None and final.failure_code is FailureCode.CONFIRMATION_EXPIRED,
              final.failure_code.value if final and final.failure_code else "none")
    e.require("NO order reached the broker", not ctx.orders_for(request.request_id),
              len(ctx.orders_for(request.request_id)))
    return e


def drill_5_a_wide_spread_is_refused(ctx: DrillContext) -> Expectations:
    e = Expectations()
    ctx.settings = replace_settings(ctx.settings, max_spread_points=50.0)
    request = ctx.confirmed()
    # Widened AFTER the confirmation, which is the real shape of this failure: the human
    # confirmed on a normal spread and the market moved before the executor looked.
    ctx.broker.set_spread_points(400.0)
    final = ctx.worker().process(request.request_id)

    e.require("status is FAILED", final is not None and final.status is TradeRequestStatus.FAILED,
              final.status.value if final else "no result")
    e.require("failure_code is spread_limit",
              final is not None and final.failure_code is FailureCode.SPREAD_LIMIT,
              final.failure_code.value if final and final.failure_code else "none")
    e.require("NO order reached the broker", not ctx.orders_for(request.request_id),
              len(ctx.orders_for(request.request_id)))
    return e


def drill_6_a_lot_over_the_limit_is_refused(ctx: DrillContext) -> Expectations:
    e = Expectations()
    ctx.settings = replace_settings(ctx.settings, max_lot=1.0)
    request = ctx.confirmed(volume=2.0)
    final = ctx.worker().process(request.request_id)

    e.require("status is FAILED", final is not None and final.status is TradeRequestStatus.FAILED,
              final.status.value if final else "no result")
    e.require("failure_code is max_lot_exceeded",
              final is not None and final.failure_code is FailureCode.MAX_LOT_EXCEEDED,
              final.failure_code.value if final and final.failure_code else "none")
    e.require("NO order reached the broker", not ctx.orders_for(request.request_id),
              len(ctx.orders_for(request.request_id)))
    return e


def drill_7_an_unknown_outcome_reconciles_and_never_retries(
    ctx: DrillContext,
) -> Expectations:
    """The invariant with the worst failure mode: a retry after an unknown result.

    The broker accepts the order and the connection dies before the result arrives. The
    order exists. A retry would double the position, and the position would be real.
    """
    from aureon.execution.fake_broker import Behaviour

    e = Expectations()
    ctx.broker.script(Behaviour(crash_after_send=True))
    request = ctx.confirmed()
    worker = ctx.worker()
    outcome = worker.process(request.request_id)

    e.require("the executor returned no resolution", outcome is None, outcome)
    mid = ctx.requests.get(request.request_id)
    e.require("the request is left EXECUTING",
              mid is not None and mid.status is TradeRequestStatus.EXECUTING,
              mid.status.value if mid else "missing")
    e.require("it carries the comment token reconciliation searches for",
              mid is not None and mid.comment_token == comment_token(request.request_id),
              mid.comment_token if mid else "none")
    orders = ctx.orders_for(request.request_id)
    e.require("the order DOES exist at the broker", len(orders) == 1, len(orders))

    # A second pass by the same worker must not send again, even before reconciliation.
    worker.process(request.request_id)
    e.require("a second pass sent nothing more",
              len(ctx.orders_for(request.request_id)) == 1,
              len(ctx.orders_for(request.request_id)))

    outcomes = ctx.reconciler().reconcile_all()
    final = ctx.requests.get(request.request_id)
    e.require("reconciliation adopted the existing order",
              final is not None
              and final.status
              in {TradeRequestStatus.FILLED, TradeRequestStatus.PARTIALLY_FILLED},
              final.status.value if final else "missing")
    e.require("still exactly one order at the broker",
              len(ctx.orders_for(request.request_id)) == 1,
              len(ctx.orders_for(request.request_id)))
    e.require("reconciliation reported one action", len(outcomes) >= 1,
              [o.action for o in outcomes])
    return e


def drill_8_a_cancel_that_loses_to_a_fill_says_so(ctx: DrillContext) -> Expectations:
    """§46's ugly race: a cancel arriving after the order already filled.

    What must never happen is a ``completed`` cancel over a live position, because a human
    reading that believes the order is gone when they are holding one.
    """
    e = Expectations()
    quote = ctx.broker.quote(ctx.symbol)
    # A buy limit below the market: a legitimate resting order.
    request = ctx.confirmed(order_type=OrderType.BUY_LIMIT, price=round(quote.bid - 5.0, 2))
    placed = ctx.worker().process(request.request_id)
    e.require("the pending order is PENDING",
              placed is not None and placed.status is TradeRequestStatus.PENDING,
              placed.status.value if placed else "no result")
    ticket = placed.order_ticket if placed else None
    e.require("it has a ticket", ticket is not None, ticket)
    if ticket is None:
        return e

    # It fills before the cancel is performed.
    ctx.broker.fill_pending(int(ticket))

    from aureon.models.control import ControlRequest

    control = ControlRequest(
        control_id=f"drill-cancel-{uuid.uuid4().hex[:8]}",
        kind=ControlRequestKind.CANCEL,
        target=str(ticket),
        symbol=ctx.symbol,
        requested_by=OPERATOR,
    )
    ctx.controls.create(control)
    resolved = ctx.control_worker().process(control.control_id)

    e.require("the cancel is FAILED, not COMPLETED",
              resolved is not None and resolved.status is ControlRequestStatus.FAILED,
              resolved.status.value if resolved else "no result")
    e.require("and it says the order was not resting",
              resolved is not None and "resting" in (resolved.failure_message or ""),
              (resolved.failure_message if resolved else "none"))
    positions = ctx.broker.open_positions()
    e.require("the position the fill created is still open", len(positions) == 1,
              len(positions))
    return e


def drill_9_a_closed_trade_refuses_to_be_rewritten(ctx: DrillContext) -> Expectations:
    """§58: history is not editable, and the observational stamps still are.

    The only drill that needs no broker. It belongs here because it is the last link in
    the same chain: an execution nobody can later rewrite is what makes a review of it
    falsifiable.
    """
    from aureon.models.base import MarketTime
    from aureon.models.enums import Direction, TradeSource
    from aureon.models.trade import Trade
    from aureon.storage.trade_repository import TerminalWriteRejected

    e = Expectations()
    moment = ctx.now()
    trade = Trade(
        trade_id=f"drill-trade-{uuid.uuid4().hex[:8]}",
        mt5_position_id=int(uuid.uuid4().int % 1_000_000),
        symbol=ctx.symbol,
        direction=Direction.BUY,
        volume=ctx.volume,
        open_price=2400.0,
        open_time=MarketTime.from_utc(moment, "Europe/Athens"),
        magic=ctx.magic,
        source=TradeSource.AUREON,
    )
    ctx.trades.upsert_open(trade)
    ctx.trades.transition(
        trade.trade_id,
        TradeStatus.CLOSED,
        updates={
            "close_price": 2405.0,
            "close_time": MarketTime.from_utc(moment, "Europe/Athens"),
            "realized_pnl": 50.0,
            "closed_volume": ctx.volume,
            "close_reason": "manual",
        },
    )
    closed = ctx.trades.get(trade.trade_id)
    e.require("the trade is CLOSED",
              closed is not None and closed.status is TradeStatus.CLOSED,
              closed.status.value if closed else "missing")
    e.require("its realized P&L is recorded",
              closed is not None and closed.realized_pnl == 50.0,
              closed.realized_pnl if closed else "missing")

    refused = False
    try:
        ctx.trades.transition(
            trade.trade_id, TradeStatus.CLOSED, updates={"realized_pnl": 9_999.0}
        )
    except TerminalWriteRejected as exc:
        refused = True
        detail = str(exc)
    else:
        detail = "the write was ACCEPTED"
    e.require("rewriting realized_pnl is refused", refused, detail)

    stamped = ctx.trades.transition(
        trade.trade_id, TradeStatus.CLOSED, updates={"last_reconciled_at": ctx.now()}
    )
    e.require("last_reconciled_at is still writable",
              stamped.last_reconciled_at is not None, stamped.last_reconciled_at)
    after = ctx.trades.get(trade.trade_id)
    e.require("and the P&L is untouched",
              after is not None and after.realized_pnl == 50.0,
              after.realized_pnl if after else "missing")
    return e


def replace_settings(settings: ExecutionSettings, **updates: Any) -> ExecutionSettings:
    """``model_copy`` under a name that says what it is for.

    Settings are a frozen-ish document; a drill changing one produces a new one rather
    than mutating what another drill already read.
    """
    return settings.model_copy(update=updates)


CATALOGUE: tuple[Drill, ...] = (
    Drill(
        number=1,
        name="confirmed_request_fills_once",
        proves="A CONFIRMED market request reaches the broker exactly once, carrying "
        "Aureon's comment token and magic number, and the fill is recorded.",
        invariant="§29-§35",
        places_orders=True,
        run=drill_1_a_confirmed_request_fills_once,
    ),
    Drill(
        number=2,
        name="kill_switch_stops_execution",
        proves="With trading_enabled false, a CONFIRMED request FAILS with "
        "trading_disabled and the broker is never asked to send.",
        invariant="§57, decision 11",
        places_orders=False,
        run=drill_2_the_kill_switch_stops_a_confirmed_request,
    ),
    Drill(
        number=3,
        name="two_executors_place_one_order",
        proves="Two executors racing one request produce exactly one order.",
        invariant="§32 exactly-once",
        places_orders=True,
        run=drill_3_two_executors_place_one_order,
        needs_isolation=True,
    ),
    Drill(
        number=4,
        name="expired_confirmation_never_sends",
        proves="A confirmation past its TTL is never claimed: FAILED_STALE with "
        "confirmation_expired, written in the claim's own transaction, nothing sent.",
        invariant="§42, §56",
        places_orders=False,
        run=drill_4_an_expired_confirmation_never_sends,
    ),
    Drill(
        number=5,
        name="wide_spread_is_refused",
        proves="A spread that widened after the confirmation is refused with "
        "spread_limit, against the price we would trade at rather than the one a human "
        "saw.",
        invariant="§41, §56",
        places_orders=False,
        run=drill_5_a_wide_spread_is_refused,
        needs_scriptable_broker=True,
    ),
    Drill(
        number=6,
        name="lot_over_the_limit_is_refused",
        proves="A volume above max_lot is refused with max_lot_exceeded.",
        invariant="§56",
        places_orders=False,
        run=drill_6_a_lot_over_the_limit_is_refused,
    ),
    Drill(
        number=7,
        name="unknown_outcome_reconciles_never_retries",
        proves="A connection lost after the send leaves the request EXECUTING with its "
        "comment token, sends nothing more, and is repaired by reconciliation finding "
        "the order that already exists.",
        invariant="CLAUDE.md: unknown broker result → reconcile, never retry (§35)",
        places_orders=True,
        run=drill_7_an_unknown_outcome_reconciles_and_never_retries,
        needs_scriptable_broker=True,
    ),
    Drill(
        number=8,
        name="cancel_that_loses_to_a_fill",
        proves="A cancel arriving after the order filled is recorded as FAILED saying "
        "the order was not resting -- never as a completed cancel over a live position.",
        invariant="§46, §47, decision 110",
        places_orders=True,
        run=drill_8_a_cancel_that_loses_to_a_fill_says_so,
        needs_scriptable_broker=True,
    ),
    Drill(
        number=9,
        name="closed_trade_refuses_to_be_rewritten",
        proves="A CLOSED trade refuses a write to realized_pnl and still accepts "
        "last_reconciled_at.",
        invariant="§58, decision 108",
        places_orders=False,
        run=drill_9_a_closed_trade_refuses_to_be_rewritten,
    ),
)


def drill_by_number(number: int) -> Drill:
    for drill in CATALOGUE:
        if drill.number == number:
            return drill
    raise KeyError(
        f"no drill {number}; the catalogue is 1-{len(CATALOGUE)} "
        f"({', '.join(f'{d.number}:{d.name}' for d in CATALOGUE)})"
    )


# ── Running them ─────────────────────────────────────────────────────────────


@dataclass
class DrillRun:
    drill: Drill
    result: CheckResult
    observations: list[str]


def run_drill(drill: Drill, context_factory: Callable[[], DrillContext]) -> DrillRun:
    """Run one drill in a fresh context.

    Fresh per drill because a drill inherits nothing: leftover settings, a consumed
    broker behaviour or another drill's resting order would make a verdict depend on the
    order the drills happened to run in.
    """
    context = context_factory()
    if drill.needs_scriptable_broker and not context.broker_is_scriptable:
        return DrillRun(
            drill,
            CheckResult(
                drill.name,
                Status.SKIP,
                "not run: this broker cannot be made to misbehave on demand",
                remedy=(
                    "The drill needs to widen a spread, drop a connection mid-send or "
                    "fill a resting order, and a real terminal does none of those on "
                    "request. Run it against FakeBroker; the manual equivalent for a "
                    "demo account is in docs/DEMO_EXECUTION_CHECKLIST.md."
                ),
            ),
            [],
        )
    if drill.needs_isolation and not context.transactions_isolated:
        return DrillRun(
            drill,
            CheckResult(
                drill.name,
                Status.SKIP,
                "not run: this store's transactions do not isolate",
                remedy=(
                    "This drill proves exactly-once execution, which rests entirely on "
                    "Firestore's transaction semantics. Run it against the emulator or a "
                    "real project; a double would get to define the thing under test."
                ),
            ),
            [],
        )
    try:
        expectations = drill.run(context)
    except Exception as exc:  # a crash is a failed drill, with its cause recorded
        return DrillRun(
            drill,
            CheckResult(
                drill.name,
                Status.FAIL,
                f"the drill raised {type(exc).__name__}: {exc}",
                remedy="It did not finish, so nothing about the invariant was shown.",
            ),
            [],
        )
    status = Status.PASS if expectations.satisfied else Status.FAIL
    return DrillRun(
        drill,
        CheckResult(
            drill.name,
            status,
            expectations.summary(),
            remedy=None if expectations.satisfied else drill.invariant,
        ),
        expectations.lines(),
    )


def run_drills(
    context_factory: Callable[[], DrillContext],
    *,
    numbers: list[int] | None = None,
) -> tuple[CheckReport, list[DrillRun]]:
    """Run the catalogue, or the numbers given, in order."""
    chosen = (
        CATALOGUE
        if numbers is None
        else tuple(drill_by_number(n) for n in numbers)
    )
    runs = [run_drill(drill, context_factory) for drill in chosen]
    return CheckReport(results=[r.result for r in runs]), runs


def catalogue_markdown() -> list[str]:
    """The catalogue as a table, for the checklist and the evidence file."""
    lines = [
        "| # | drill | proves | invariant | places orders |",
        "|---|---|---|---|---|",
    ]
    for drill in CATALOGUE:
        lines.append(
            f"| {drill.number} | `{drill.name}` | {drill.proves} | {drill.invariant} | "
            f"{'yes' if drill.places_orders else 'no'} |"
        )
    return lines
