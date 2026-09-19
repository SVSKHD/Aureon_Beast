"""The §81 acceptance test: exactly-once under randomised failure.

Scenarios A–H each probe one known failure. This probes *combinations* -- 50 requests
per run, each independently assigned a failure mode from a seeded RNG, across 20 seeds.
1000 requests in total, every one of which must end with **at most one order at the
broker**, whatever went wrong.

Two claims, per §81:

* ``ledger <= N`` **always** -- no combination of crashes, races and restarts may ever
  produce a second order for one authorisation;
* ``ledger == N`` when every request is valid and accepted -- the system must not be
  safe by simply refusing to trade. A guard that blocked everything would satisfy the
  first claim and be useless.

The seeds are fixed so a failure is reproducible: the printed seed replays exactly.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from aureon.execution.fake_broker import Behaviour, FakeBroker
from aureon.models.enums import FailureCode, OrderType, TradeRequestStatus
from aureon.models.identity import comment_token
from aureon.storage.trade_request_repository import TradeRequestRepository
from tests.failure_injection.conftest import make_request

pytestmark = pytest.mark.emulator

N_REQUESTS = 50
SEEDS = tuple(range(20))

# States in which an order definitely exists at the broker.
OPENED = {
    TradeRequestStatus.FILLED,
    TradeRequestStatus.PARTIALLY_FILLED,
    TradeRequestStatus.PENDING,
}


@dataclass(frozen=True)
class Scenario:
    """One way for a request's execution to go wrong."""

    name: str
    behaviour: Behaviour | None = None
    pending: bool = False
    second_worker: bool = False
    expect_order: bool = True


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("clean"),
    Scenario("clean_pending", pending=True),
    Scenario("crash_after_send", Behaviour(crash_after_send=True)),
    Scenario("crash_before_result", Behaviour(crash_before_result=True), expect_order=False),
    Scenario("rejected", Behaviour(reject=FailureCode.INSUFFICIENT_MARGIN), expect_order=False),
    Scenario("requote", Behaviour(reject=FailureCode.REQUOTE, requote=True), expect_order=False),
    Scenario("partial", Behaviour(partial_fill_volume=0.03)),
    Scenario("race", second_worker=True),
    Scenario("crash_after_send_pending", Behaviour(crash_after_send=True), pending=True),
)


def run_one(
    repository: TradeRequestRepository,
    broker: FakeBroker,
    make_worker,
    scenario: Scenario,
    *,
    index: int,
) -> str:
    """Create, confirm and execute one request under its assigned scenario."""
    request = make_request(
        request_id=f"acc-{index:04d}",
        order_type=OrderType.BUY_STOP if scenario.pending else OrderType.MARKET_BUY,
        price=2405.00 if scenario.pending else None,
        volume=0.10,
    )
    repository.create(request)
    repository.confirm(
        request.request_id, request.requested_by, request.quote, confirmation_ttl_seconds=600.0
    )

    if scenario.behaviour is not None:
        broker.script(scenario.behaviour)

    primary = make_worker(broker, executor_id=f"exec-a-{index}")
    if scenario.second_worker:
        # Two workers on one request; exactly one must win.
        rival = make_worker(broker, executor_id=f"exec-b-{index}")
        primary.process(request.request_id)
        rival.process(request.request_id)
    else:
        primary.process(request.request_id)
    return request.request_id


@pytest.mark.parametrize("seed", SEEDS)
def test_exactly_once_under_randomised_failure(
    seed: int,
    repository: TradeRequestRepository,
    broker: FakeBroker,
    make_worker,
    make_reconciler,
) -> None:
    """``ledger <= N`` for every request, under any mixture of failures (§81)."""
    rng = random.Random(seed)
    assignments = [rng.choice(SCENARIOS) for _ in range(N_REQUESTS)]

    request_ids = [
        run_one(repository, broker, make_worker, scenario, index=i)
        for i, scenario in enumerate(assignments)
    ]

    # A restart: reconciliation resolves everything left ambiguous.
    make_reconciler(broker, grace_seconds=0.0).reconcile_all()

    total_executions = 0
    for request_id, scenario in zip(request_ids, assignments, strict=True):
        count = len(broker.executions_for(comment_token(request_id)))
        total_executions += count

        assert count <= 1, (
            f"seed {seed}: {request_id} ({scenario.name}) produced {count} orders. "
            "One authorisation must never become two trades."
        )

        stored = repository.get(request_id)
        assert stored is not None

        # Nothing may be left unresolved after reconciliation: a request stuck in
        # EXECUTING is an order nobody is accounting for.
        assert stored.status is not TradeRequestStatus.EXECUTING, (
            f"seed {seed}: {request_id} ({scenario.name}) is still EXECUTING"
        )

        # A request the system says opened something really must have.
        if stored.status in OPENED:
            assert count == 1, (
                f"seed {seed}: {request_id} is {stored.status.value} but the broker has "
                f"{count} orders for it"
            )

    assert total_executions <= N_REQUESTS, (
        f"seed {seed}: {total_executions} orders for {N_REQUESTS} requests"
    )


@pytest.mark.parametrize("seed", SEEDS[:5])
def test_every_valid_request_executes_exactly_once(
    seed: int,
    repository: TradeRequestRepository,
    broker: FakeBroker,
    make_worker,
    make_reconciler,
) -> None:
    """``ledger == N`` when nothing goes wrong (§81).

    The other half of the acceptance criterion. A system that refused every trade would
    satisfy "never twice" perfectly and be worthless, so this pins that the safety
    machinery does not cost correctness on the happy path.
    """
    rng = random.Random(seed)
    # Only scenarios that SHOULD result in an order, so the expected count is exact.
    clean = [
        rng.choice([s for s in SCENARIOS if s.expect_order and not s.second_worker])
        for _ in range(N_REQUESTS)
    ]

    request_ids = [
        run_one(repository, broker, make_worker, scenario, index=i)
        for i, scenario in enumerate(clean)
    ]
    make_reconciler(broker, grace_seconds=0.0).reconcile_all()

    total = sum(len(broker.executions_for(comment_token(r))) for r in request_ids)
    assert total == N_REQUESTS, (
        f"seed {seed}: {total} orders for {N_REQUESTS} valid requests -- the guard or a "
        "crash path is losing trades that should have executed"
    )

    for request_id in request_ids:
        stored = repository.get(request_id)
        assert stored.status in OPENED, (
            f"{request_id} ended {stored.status.value}, expected an opened state"
        )


def test_no_request_is_ever_left_executing_after_reconciliation(
    repository: TradeRequestRepository, broker: FakeBroker, make_worker, make_reconciler
) -> None:
    """Every unknown outcome must become a known one.

    An order left in EXECUTING forever is an order nobody is accounting for -- the
    failure mode that the "never retry" rule would otherwise create.
    """
    for index in range(12):
        run_one(
            repository,
            broker,
            make_worker,
            SCENARIOS[index % len(SCENARIOS)],
            index=index,
        )

    make_reconciler(broker, grace_seconds=0.0).reconcile_all()

    stuck = [
        r.request_id
        for r in repository.list_by_status(TradeRequestStatus.EXECUTING, limit=200)
    ]
    assert stuck == [], f"left EXECUTING: {stuck}"


def test_the_reconciler_never_places_an_order(
    repository: TradeRequestRepository, broker: FakeBroker, make_worker, make_reconciler
) -> None:
    """The executor refuses to retry BECAUSE reconciliation exists.

    If reconciliation could send, the guarantee would be circular -- so it must never
    call a send method, no matter what it finds or fails to find.
    """
    for index in range(8):
        run_one(
            repository, broker, make_worker, SCENARIOS[index % len(SCENARIOS)], index=index
        )
    before = len(broker.ledger)
    broker.calls.clear()

    make_reconciler(broker, grace_seconds=0.0).reconcile_all()

    assert "send_market_order" not in broker.calls
    assert "send_pending_order" not in broker.calls
    assert len(broker.ledger) == before, "reconciliation changed the broker's ledger"
