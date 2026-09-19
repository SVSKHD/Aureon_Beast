"""The §80 failure-injection scenarios, A through H.

Each one arranges a specific way for execution to go wrong and asserts that **at most
one order reached the broker**. The ledger inside ``FakeBroker`` is the verdict, and it
survives a simulated restart, so "exactly one execution across two process lifetimes" is
a claim these tests can actually make.

Run against the real Firestore emulator -- the guarantee is Firestore's transaction
semantics, so faking them would be assuming the answer.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from aureon.execution.fake_broker import Behaviour, FakeBroker
from aureon.models.base import utc_now
from aureon.models.enums import FailureCode, OrderType, TradeRequestStatus
from aureon.models.identity import comment_token
from aureon.storage.trade_request_repository import (
    ClaimRejected,
    ConfirmationRejected,
    TradeRequestRepository,
)
from tests.failure_injection.conftest import USER, audit_actions, make_request

pytestmark = pytest.mark.emulator


def executions(broker: FakeBroker, request_id: str) -> int:
    return len(broker.executions_for(comment_token(request_id)))


# ── A: crash after send, before the result was written ────────────────────────


def test_scenario_a_crash_after_send_reconciles_to_one_execution(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed, make_worker, make_reconciler
) -> None:
    """A. The broker accepted the order; we never found out. Restart must NOT re-send.

    This is the scenario the whole phase exists for. The order exists. A retry would
    make two real trades out of one authorisation.
    """
    request = confirmed()
    broker.script(Behaviour(crash_after_send=True))

    first = make_worker(broker, executor_id="exec-1")
    assert first.process(request.request_id) is None
    assert first.left_executing == 1

    # The request is left EXECUTING -- deliberately NOT failed.
    stored = repository.get(request.request_id)
    assert stored.status is TradeRequestStatus.EXECUTING
    assert stored.comment_token == comment_token(request.request_id)
    assert stored.execution_attempt_id

    # The broker really does have the order.
    assert executions(broker, request.request_id) == 1

    # Restart: a NEW executor around the SAME broker, so the ledger persists.
    make_worker(broker, executor_id="exec-2")
    outcomes = make_reconciler(broker).reconcile_all()

    repaired = repository.get(request.request_id)
    assert repaired.status is TradeRequestStatus.FILLED
    assert repaired.position_id is not None
    assert [o.action for o in outcomes] == ["repaired"]

    # THE assertion: exactly one, across both lifetimes.
    assert executions(broker, request.request_id) == 1


def test_scenario_a_variant_crash_before_send_fails_cleanly(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed, make_worker, make_reconciler
) -> None:
    """A′. Nothing was ever placed. Indistinguishable to the caller -- only looking tells.

    Reconciliation finds nothing and, past the grace period, fails the request. Zero
    executions, and still no re-send.
    """
    request = confirmed()
    broker.script(Behaviour(crash_before_result=True))

    make_worker(broker, executor_id="exec-1").process(request.request_id)
    assert repository.get(request.request_id).status is TradeRequestStatus.EXECUTING
    assert executions(broker, request.request_id) == 0

    make_reconciler(broker, grace_seconds=0.0).reconcile_all()
    failed = repository.get(request.request_id)
    assert failed.status is TradeRequestStatus.FAILED_RECONCILIATION
    assert failed.failure_code is FailureCode.RECONCILIATION_NOT_FOUND
    assert executions(broker, request.request_id) == 0


# ── B: two workers, one CONFIRMED request ─────────────────────────────────────


def test_scenario_b_two_workers_produce_one_claim_and_one_execution(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed, make_worker
) -> None:
    """B. Two executors race. Firestore's transaction lets exactly one through."""
    request = confirmed()

    first = make_worker(broker, executor_id="exec-1")
    second = make_worker(broker, executor_id="exec-2")

    results = [first.process(request.request_id), second.process(request.request_id)]
    won = [r for r in results if r is not None]

    assert len(won) == 1, "both workers claimed the same request"
    assert executions(broker, request.request_id) == 1
    assert repository.get(request.request_id).status is TradeRequestStatus.FILLED


def test_scenario_b_concurrent_claims_from_threads(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed, make_worker
) -> None:
    """B′. The same race, genuinely concurrent rather than sequential.

    Sequential calls can pass by accident -- the second simply reads a document that has
    already moved on. Real threads make the transaction do the work.
    """
    request = confirmed()
    workers = [make_worker(broker, executor_id=f"exec-{i}") for i in range(5)]
    claimed: list[object] = []
    barrier = threading.Barrier(len(workers))

    def attempt(worker) -> None:
        barrier.wait()
        try:
            if worker.process(request.request_id) is not None:
                claimed.append(worker.executor_id)
        except ClaimRejected:
            pass

    threads = [threading.Thread(target=attempt, args=(w,)) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(claimed) == 1, f"{len(claimed)} workers claimed one request: {claimed}"
    assert executions(broker, request.request_id) == 1


# ── C: the human presses confirm five times ───────────────────────────────────


def test_scenario_c_five_confirms_produce_one_transition_and_one_execution(
    repository: TradeRequestRepository, firestore_client, broker: FakeBroker, make_worker
) -> None:
    """C. Discord can deliver a click more than once. One authorisation, one trade."""
    request = make_request()
    repository.create(request)

    for _ in range(5):
        repository.confirm(request.request_id, USER, request.quote)

    stored = repository.get(request.request_id)
    assert stored.status is TradeRequestStatus.CONFIRMED
    # The version proves only one transition happened, not five.
    assert stored.confirmation_version == 1

    actions = audit_actions(firestore_client, request.request_id)
    assert actions.count("trade_request.confirm") == 1

    make_worker(broker).process(request.request_id)
    assert executions(broker, request.request_id) == 1


def test_scenario_c_variant_only_the_requester_may_confirm(
    repository: TradeRequestRepository, broker: FakeBroker, make_worker
) -> None:
    """C′. Anyone else pressing the button would authorise someone else's money (§27)."""
    request = make_request()
    repository.create(request)

    with pytest.raises(ConfirmationRejected, match="did not request"):
        repository.confirm(request.request_id, "someone-else", request.quote)

    assert repository.get(request.request_id).status is TradeRequestStatus.REQUESTED
    # Never confirmed, so never executable.
    assert make_worker(broker).process(request.request_id) is None
    assert executions(broker, request.request_id) == 0


# ── D: the executor was offline past the confirmation TTL ─────────────────────


def test_scenario_d_a_stale_confirmation_fails_and_never_executes(
    repository: TradeRequestRepository, broker: FakeBroker, make_worker
) -> None:
    """D. A request that sat too long must not be filled at a price nobody saw."""
    request = make_request()
    repository.create(request)
    past = utc_now() - timedelta(minutes=10)
    repository.confirm(
        request.request_id, USER, request.quote, confirmation_ttl_seconds=30.0, now=past
    )

    worker = make_worker(broker, executor_id="exec-late")
    assert worker.process(request.request_id) is None

    stale = repository.get(request.request_id)
    assert stale.status is TradeRequestStatus.FAILED_STALE
    assert stale.failure_code is FailureCode.CONFIRMATION_EXPIRED
    assert executions(broker, request.request_id) == 0


# ── E: the listener is dead; the poll must still work ─────────────────────────


def test_scenario_e_polling_picks_up_what_the_listener_missed(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed, make_worker
) -> None:
    """E. A dropped listener must not mean a human watching a spinner forever (§29)."""
    request = confirmed()
    worker = make_worker(broker, executor_id="exec-poll")
    # No listener started at all -- this is the poll path alone.
    assert worker.poll_once() == 1
    assert repository.get(request.request_id).status is TradeRequestStatus.FILLED
    assert executions(broker, request.request_id) == 1

    # A second poll must not act again.
    assert worker.poll_once() == 0
    assert executions(broker, request.request_id) == 1


# ── F: a crash during reconciliation ──────────────────────────────────────────


def test_scenario_f_crash_mid_reconciliation_still_settles_on_one(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed, make_worker, make_reconciler
) -> None:
    """F. Reconciliation itself dies. A second attempt must finish the job, not redo it."""
    request = confirmed()
    broker.script(Behaviour(crash_after_send=True))
    make_worker(broker, executor_id="exec-1").process(request.request_id)
    assert executions(broker, request.request_id) == 1

    # First reconciliation attempt dies before it can write.
    reconciler = make_reconciler(broker)
    original = reconciler.repository.resolve

    def explode(*args, **kwargs):
        raise RuntimeError("reconciliation process died")

    reconciler.repository.resolve = explode  # type: ignore[method-assign]
    reconciler.reconcile_all()  # swallowed per-request; nothing written
    assert repository.get(request.request_id).status is TradeRequestStatus.EXECUTING

    # Second attempt, healthy.
    reconciler.repository.resolve = original  # type: ignore[method-assign]
    make_reconciler(broker).reconcile_all()

    assert repository.get(request.request_id).status is TradeRequestStatus.FILLED
    assert executions(broker, request.request_id) == 1


def test_scenario_f_variant_reconciling_twice_is_idempotent(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed, make_worker, make_reconciler
) -> None:
    """Running reconciliation again after it succeeded must change nothing."""
    request = confirmed()
    broker.script(Behaviour(crash_after_send=True))
    make_worker(broker, executor_id="exec-1").process(request.request_id)

    make_reconciler(broker).reconcile_all()
    first = repository.get(request.request_id)
    make_reconciler(broker).reconcile_all()
    second = repository.get(request.request_id)

    assert first.status is second.status is TradeRequestStatus.FILLED
    assert first.position_id == second.position_id
    assert executions(broker, request.request_id) == 1


# ── G: a pending order fills while we are cancelling it ───────────────────────


def test_scenario_g_a_pending_order_that_fills_during_cancel_is_filled_not_cancelled(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed, make_worker, make_reconciler
) -> None:
    """G. The market wins the race. Recording CANCELLED would deny a live position."""
    request = confirmed(order_type=OrderType.BUY_STOP, price=2405.00)
    worker = make_worker(broker, executor_id="exec-1")
    worker.process(request.request_id)

    resting = repository.get(request.request_id)
    assert resting.status is TradeRequestStatus.PENDING
    ticket = resting.order_ticket
    assert ticket is not None

    # It fills before the cancel lands.
    broker.fill_pending(ticket)
    cancel = broker.cancel_order(ticket)
    assert cancel.ok is False, "cancelling a filled order must not report success"

    make_reconciler(broker).reconcile_all()
    settled = repository.get(request.request_id)
    assert settled.status is TradeRequestStatus.FILLED
    assert settled.position_id is not None
    assert executions(broker, request.request_id) == 1


# ── H: a position closes while we are closing it ──────────────────────────────


def test_scenario_h_closing_an_already_closed_position_opens_nothing(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed, make_worker
) -> None:
    """H. A second close must be refused, and must certainly not open anything."""
    request = confirmed()
    make_worker(broker, executor_id="exec-1").process(request.request_id)
    filled = repository.get(request.request_id)
    position_id = filled.position_id
    assert position_id is not None

    first = broker.close_position(position_id)
    assert first.ok is True
    assert broker.open_positions() == []

    second = broker.close_position(position_id)
    assert second.ok is False
    assert second.failure_code is FailureCode.BROKER_REJECTED

    # Still exactly one OPENING execution: a refused close cannot have opened a trade.
    assert executions(broker, request.request_id) == 1
    exits = [e for e in broker.ledger if e.kind == "close"]
    assert len(exits) == 1, "a second close was recorded"


# ── Cross-cutting: the guard, and the audit trail ─────────────────────────────


def test_a_guard_refusal_never_reaches_the_broker(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed, make_worker, settings
) -> None:
    """§57: `/trading disable` must block the very next attempt."""
    request = confirmed()
    settings.trading_enabled = False

    worker = make_worker(broker, executor_id="exec-1")
    worker.process(request.request_id)

    failed = repository.get(request.request_id)
    assert failed.status is TradeRequestStatus.FAILED
    assert failed.failure_code is FailureCode.TRADING_DISABLED
    assert executions(broker, request.request_id) == 0
    assert "send_market_order" not in broker.calls


def test_a_broker_rejection_resolves_cleanly_without_reconciliation(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed, make_worker
) -> None:
    """A rejection is an ANSWER: nothing exists, so FAILED is correct and final."""
    request = confirmed()
    broker.script(Behaviour(reject=FailureCode.INSUFFICIENT_MARGIN))

    make_worker(broker, executor_id="exec-1").process(request.request_id)
    failed = repository.get(request.request_id)
    assert failed.status is TradeRequestStatus.FAILED
    assert failed.failure_code is FailureCode.INSUFFICIENT_MARGIN
    assert executions(broker, request.request_id) == 0


def test_a_partial_fill_is_recorded_as_partial(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed, make_worker
) -> None:
    request = confirmed(volume=0.10)
    broker.script(Behaviour(partial_fill_volume=0.04))

    make_worker(broker, executor_id="exec-1").process(request.request_id)
    partial = repository.get(request.request_id)
    assert partial.status is TradeRequestStatus.PARTIALLY_FILLED
    assert partial.filled_volume == pytest.approx(0.04)
    assert executions(broker, request.request_id) == 1


def test_every_transition_is_audited_in_the_same_transaction(
    repository: TradeRequestRepository, firestore_client, broker: FakeBroker, confirmed, make_worker
) -> None:
    """§60. The audit row is the only account of what was attempted with real money."""
    request = confirmed()
    make_worker(broker, executor_id="exec-1").process(request.request_id)

    actions = audit_actions(firestore_client, request.request_id)
    assert "trade_request.create" in actions
    assert "trade_request.confirm" in actions
    assert "trade_request.claim" in actions
    assert "trade_request.resolve" in actions


def test_a_worker_cannot_resolve_without_its_lease(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed, make_worker
) -> None:
    """Two workers resolving one request is how a double trade gets recorded."""
    from aureon.storage.trade_request_repository import LeaseLost

    request = confirmed()
    repository.claim(request.request_id, "exec-owner", lease_seconds=60.0)

    with pytest.raises(LeaseLost):
        repository.resolve(
            request.request_id, "exec-impostor", TradeRequestStatus.FILLED
        )
    assert repository.get(request.request_id).status is TradeRequestStatus.EXECUTING


def test_reconciliation_may_resolve_without_a_lease(
    repository: TradeRequestRepository, broker: FakeBroker, confirmed
) -> None:
    """The one sanctioned exception: repairing a request whose executor died.

    Requiring a live lease here would leave such requests stuck in EXECUTING forever.
    """
    request = confirmed()
    repository.claim(request.request_id, "exec-dead", lease_seconds=60.0)

    repaired = repository.resolve(
        request.request_id,
        "reconciliation",
        TradeRequestStatus.FAILED_RECONCILIATION,
        failure_code=FailureCode.RECONCILIATION_NOT_FOUND,
        failure_message="order never found",
        reconciliation=True,
    )
    assert repaired.status is TradeRequestStatus.FAILED_RECONCILIATION


def test_an_ambiguous_match_refuses_to_guess(
    repository: TradeRequestRepository, confirmed, make_worker, make_reconciler
) -> None:
    """Two matching orders means something already went wrong (§35).

    Guessing between them could attach the request to the wrong position, so
    reconciliation refuses and names the candidates for a human.

    The ambiguity has to be real: two *positions* carrying the same token, not merely
    two ledger rows. An earlier version of this test duplicated only the ledger entry,
    which reconciliation quite correctly resolved as one position.
    """
    from aureon.models.trade import BrokerOrderRequest

    broker = FakeBroker()
    request = confirmed()
    token = comment_token(request.request_id)

    broker.script(Behaviour(crash_after_send=True))
    make_worker(broker, executor_id="exec-1").process(request.request_id)
    assert len(broker.executions_for(token)) == 1

    # A second order really does exist at the broker under the same token -- the state a
    # buggy retry elsewhere, or a broker-side duplicate, would leave behind.
    broker.send_market_order(
        BrokerOrderRequest(
            symbol=request.symbol,
            order_type=request.order_type,
            volume=request.volume,
            magic=770177,
            comment=token,
        )
    )
    assert len(broker.open_positions()) == 2

    make_reconciler(broker).reconcile_all()

    failed = repository.get(request.request_id)
    assert failed.status is TradeRequestStatus.FAILED_RECONCILIATION
    assert failed.failure_code is FailureCode.RECONCILIATION_AMBIGUOUS
    assert "2 broker records match" in (failed.failure_message or "")
    # And it certainly did not send a third.
    assert len(broker.executions_for(token)) == 2
