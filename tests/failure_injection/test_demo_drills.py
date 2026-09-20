"""The nine drills, against the real emulator, before anyone runs them on a demo account.

This is the first of the two runs the drills exist to have, and the one the second depends
on: it proves the *drills* are right. A drill that is wrong about what should happen
produces a green table beside a broken guard, and the operator reading it is worse off than
if there were no drill at all.

It lives in the emulator suite rather than in `tests/unit` because every step of every
drill is a read-then-conditional-write through a repository. The exactly-once guarantee is
Firestore's transaction semantics and nothing else, so drill 3 -- two executors racing one
request -- cannot be run against a double at all without the double getting to define the
property under test. Against the emulator it runs for real.

Two things are asserted beyond "they pass":

* each drill **recorded observations**, because ``Expectations`` with nothing required is
  vacuously satisfied and a drill that checked nothing would otherwise read as green;
* the guard drills **fail when the guard does**, checked by breaking it.
"""

from __future__ import annotations

import pytest

from aureon.execution.drills import (
    CATALOGUE,
    DrillContext,
    drill_by_number,
    run_drill,
    run_drills,
)
from aureon.execution.fake_broker import FakeBroker
from aureon.services.checks import Status

pytestmark = pytest.mark.emulator

ISOLATION_DRILL = 3


@pytest.fixture
def context_factory(firestore_client, settings):
    """A fresh broker per drill, one emulator database for the run.

    A fresh broker because a leftover behaviour or resting order would make a verdict
    depend on the order the drills ran in. One database because every drill uses its own
    request ids, and a per-drill project would triple the run time for no isolation the
    ids do not already give.
    """

    def factory() -> DrillContext:
        return DrillContext(
            client=firestore_client,
            broker=FakeBroker(),
            settings=settings,
            transactions_isolated=True,
        )

    return factory


def test_every_drill_passes_against_the_fake_broker(context_factory) -> None:
    report, runs = run_drills(context_factory)

    failures = [
        f"{r.drill.number} {r.drill.name}: {r.result.detail}\n"
        + "\n".join(f"    {line}" for line in r.observations)
        for r in runs
        if r.result.status is not Status.PASS
    ]
    assert not failures, "drills did not pass:\n" + "\n".join(failures)
    assert report.exit_code == 0
    assert len(runs) == len(CATALOGUE) == 9


def test_the_racing_drill_really_races_here(context_factory) -> None:
    """The one drill a double cannot host: it must run, not skip, against the emulator."""
    run = run_drill(drill_by_number(ISOLATION_DRILL), context_factory)
    assert run.result.status is Status.PASS, run.result.detail
    assert any("exactly one order" in line for line in run.observations)
    assert all(line.startswith("ok") for line in run.observations), run.observations


def test_every_drill_recorded_what_it_observed(context_factory) -> None:
    """``Expectations`` with nothing required is satisfied, so a drill that asserted
    nothing would pass. Each one must have looked at something and said what it saw."""
    _report, runs = run_drills(context_factory)
    for run in runs:
        assert run.observations, f"{run.drill.name} observed nothing"
        assert len(run.observations) >= 3, f"{run.drill.name} barely looked"
        for line in run.observations:
            assert "actual:" in line


def test_the_drills_that_must_not_send_prove_the_broker_was_untouched(
    context_factory,
) -> None:
    _report, runs = run_drills(context_factory)
    for run in runs:
        if run.drill.places_orders or run.drill.number == 9:
            continue  # drill 9 is a storage guard and touches no broker at all
        empty = [line for line in run.observations if "NO order" in line]
        assert empty, f"{run.drill.name} does not assert an empty broker"
        assert all(line.startswith("ok") for line in empty), empty


def test_the_guard_drills_fail_when_the_executor_stops_refusing(
    context_factory, monkeypatch
) -> None:
    """Break the thing being guarded and watch the drills notice.

    ``process`` is replaced by one that resolves the request FILLED without consulting the
    guard or the broker, which is what a regression in the execution path would look like
    from the outside. Every drill that asserts a refusal must turn red; one that stayed
    green under this was asserting nothing.
    """
    from aureon.execution.execution_worker import ExecutionWorker
    from aureon.models.base import utc_now
    from aureon.models.enums import TradeRequestStatus

    def permissive(self, request_id: str, *, now=None):
        claimed = self.repository.claim(
            request_id,
            self.executor_id,
            lease_seconds=self.lease_seconds,
            now=now or utc_now(),
        )
        return self.repository.resolve(
            request_id,
            self.executor_id,
            TradeRequestStatus.FILLED,
            updates={"order_ticket": 1, "fill_price": claimed.quote.ask},
        )

    monkeypatch.setattr(ExecutionWorker, "process", permissive)

    for number in (2, 4, 5, 6):
        run = run_drill(drill_by_number(number), context_factory)
        assert run.result.status is Status.FAIL, (
            f"drill {number} ({run.drill.name}) passed while the executor refused nothing"
        )

    # A drill that FINISHED and found the expectation broken points at the invariant it
    # guards. Drill 4 is the exception and for an instructive reason: the real `process`
    # swallows the claim refusal as the ordinary outcome it is, while this stand-in does
    # not -- so drill 4 dies on the ClaimRejected rather than reporting a broken
    # expectation. Either way it is red, which is the property being tested.
    for number in (2, 5, 6):
        run = run_drill(drill_by_number(number), context_factory)
        assert run.drill.invariant in (run.result.remedy or ""), run.result
    stale = run_drill(drill_by_number(4), context_factory)
    assert "ClaimRejected" in stale.result.detail


def test_a_broken_reconciliation_fails_the_no_retry_drill(
    context_factory, monkeypatch
) -> None:
    """The invariant with the worst failure mode, broken the worst way: a retry.

    ``reconcile_all`` is replaced with one that sends the order again -- the single thing
    that must never happen after an unknown result. Drill 7 must catch it.
    """
    from aureon.execution.reconciliation_service import ReconciliationService
    from aureon.models.enums import TradeRequestStatus
    from aureon.models.trade import BrokerOrderRequest

    def resend(self, *, now=None):
        for request in self.repository.list_by_status([TradeRequestStatus.EXECUTING]):
            self.broker.send_market_order(
                BrokerOrderRequest(
                    symbol=request.symbol,
                    order_type=request.order_type,
                    volume=request.volume,
                    magic=self.magic,
                    comment=request.comment_token or "",
                )
            )
        return []

    monkeypatch.setattr(ReconciliationService, "reconcile_all", resend)
    run = run_drill(drill_by_number(7), context_factory)
    assert run.result.status is Status.FAIL
    assert "still exactly one order at the broker" in run.result.detail
