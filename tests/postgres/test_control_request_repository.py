"""Control requests, and the lease the Firestore version was missing (§46, §47, plan §17).

A close, a cancel, a kill switch. These move money, so they get the trade request's claim
and the trade request's lease check -- and the lease check is the point of this file. The
Firestore ``resolve`` had none, and its own docstring named that an asymmetry rather than a
decision: a cancel can LOSE a race to a fill, so a stale writer's "completed" landing on top
of the real "already filled" is how a human comes to believe an order is gone when it is a
live position.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aureon.models.enums import ControlRequestStatus, FailureCode, TransitionError
from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.control_requests import (
    COLLECTION,
    ControlClaimRejected,
    ControlLeaseLost,
    ControlRequestRepository,
)
from tests.postgres.contention import interleave
from tests.postgres.factories import CLOSE, a_control_request

pytestmark = pytest.mark.postgres

C = ControlRequestStatus


@pytest.fixture
def repo(schema: Database) -> ControlRequestRepository:
    return ControlRequestRepository(schema)


def test_a_control_request_survives_the_database_unchanged(
    repo: ControlRequestRepository,
) -> None:
    written = repo.create(a_control_request())
    assert repo.get("c1") == written


def test_a_new_request_may_not_arrive_already_executing(
    repo: ControlRequestRepository,
) -> None:
    with pytest.raises(ValueError):
        repo.create(a_control_request(status=C.EXECUTING))
    assert repo.get("c1") is None


def test_creating_twice_is_a_retry(repo: ControlRequestRepository) -> None:
    first = repo.create(a_control_request())
    again = repo.create(a_control_request(target="different"))
    assert again.target == first.target


def test_a_claim_takes_the_lease(repo: ControlRequestRepository) -> None:
    repo.create(a_control_request())
    claimed = repo.claim("c1", "executor-1", lease_seconds=45, now=CLOSE)
    assert claimed.status is C.EXECUTING
    assert claimed.executor_instance_id == "executor-1"
    assert claimed.lease_expires_at == CLOSE + timedelta(seconds=45)


def test_a_second_claim_is_refused(repo: ControlRequestRepository) -> None:
    repo.create(a_control_request())
    repo.claim("c1", "executor-1", now=CLOSE)
    with pytest.raises(ControlClaimRejected, match="not REQUESTED"):
        repo.claim("c1", "executor-2", now=CLOSE)


class _Signalling(ControlRequestRepository):
    """Announces when it holds its lock, still inside the transaction."""

    def __init__(self, database: Database, signal) -> None:  # type: ignore[no-untyped-def]
        super().__init__(database)
        self._signal = signal

    def _locked(self, control_id: str, connection):  # type: ignore[no-untyped-def]
        row = super()._locked(control_id, connection)
        self._signal()
        return row


def test_two_executors_claiming_one_control_request_produce_one_claim(
    schema: Database,
) -> None:
    """A close that ran twice would close a position and then close whatever replaced it.

    Overlapped for real (decision 352): the first holds its lock while the second tries.
    """
    ControlRequestRepository(schema).create(a_control_request())

    outcome = interleave(
        lambda signal: _Signalling(schema, signal).claim("c1", "executor-1", now=CLOSE),
        lambda: ControlRequestRepository(schema).claim("c1", "executor-2", now=CLOSE),
    )

    assert len(outcome.succeeded) == 1, outcome
    assert isinstance(next(iter(outcome.errors.values())), ControlClaimRejected)

    claims = [
        record
        for record in ControlRequestRepository(schema).audit.for_document(
            COLLECTION, "c1"
        )
        if record.action == "control_request.claim"
    ]
    assert len(claims) == 1, f"{len(claims)} claims were audited"


def test_the_scan_takes_the_oldest_first(repo: ControlRequestRepository) -> None:
    repo.create(a_control_request("older", requested_at=CLOSE))
    repo.create(a_control_request("newer", requested_at=CLOSE + timedelta(seconds=10)))
    claimed = repo.claim_next("executor-1", now=CLOSE + timedelta(seconds=20))
    assert claimed is not None and claimed.control_id == "older"


def test_the_scan_returns_none_when_there_is_nothing(repo: ControlRequestRepository) -> None:
    assert repo.claim_next("executor-1", now=CLOSE) is None


# ── The lease check the Firestore version lacked ──────────────────────────────


def test_the_lease_holder_can_resolve(repo: ControlRequestRepository) -> None:
    repo.create(a_control_request())
    repo.claim("c1", "executor-1", lease_seconds=60, now=CLOSE)
    done = repo.resolve("c1", "executor-1", C.COMPLETED, now=CLOSE)
    assert done.status is C.COMPLETED
    assert done.completed_at == CLOSE


def test_a_non_holder_cannot_resolve(repo: ControlRequestRepository) -> None:
    repo.create(a_control_request())
    repo.claim("c1", "executor-1", now=CLOSE)
    with pytest.raises(ControlLeaseLost):
        repo.resolve("c1", "executor-2", C.COMPLETED, now=CLOSE)


def test_an_expired_holder_cannot_resolve(repo: ControlRequestRepository) -> None:
    """The case that costs a position.

    An executor whose lease expired must not record the outcome of a cancel it no longer
    owns: its "completed" would land on top of the current holder's "already filled", and a
    human would believe an order is gone when it is a live position.
    """
    repo.create(a_control_request())
    repo.claim("c1", "executor-1", lease_seconds=60, now=CLOSE)
    with pytest.raises(ControlLeaseLost):
        repo.resolve("c1", "executor-1", C.COMPLETED, now=CLOSE + timedelta(seconds=61))
    stored = repo.get("c1")
    assert stored is not None and stored.status is C.EXECUTING


def test_reconciliation_may_resolve_without_a_lease(repo: ControlRequestRepository) -> None:
    """The same sanctioned exception the trade requests use: reconciliation holds no lease
    because it is repairing what a dead executor left."""
    repo.create(a_control_request())
    repo.claim("c1", "executor-1", lease_seconds=60, now=CLOSE)
    repaired = repo.resolve(
        "c1",
        "reconciler",
        C.FAILED,
        failure_code=FailureCode.CONNECTION_LOST,
        reconciliation=True,
        now=CLOSE + timedelta(seconds=300),
    )
    assert repaired.status is C.FAILED
    assert repaired.failure_code is FailureCode.CONNECTION_LOST


def test_an_illegal_outcome_is_refused(repo: ControlRequestRepository) -> None:
    repo.create(a_control_request())
    repo.claim("c1", "executor-1", now=CLOSE)
    repo.resolve("c1", "executor-1", C.COMPLETED, now=CLOSE)
    with pytest.raises(TransitionError):
        repo.resolve("c1", "executor-1", C.EXECUTING, reconciliation=True, now=CLOSE)


def test_abandoned_control_requests_are_findable(repo: ControlRequestRepository) -> None:
    repo.create(a_control_request())
    repo.claim("c1", "executor-1", lease_seconds=60, now=CLOSE)
    assert repo.expired_leases(now=CLOSE + timedelta(seconds=30)) == []
    assert [r.control_id for r in repo.expired_leases(now=CLOSE + timedelta(seconds=61))] == ["c1"]


def test_every_move_is_audited(repo: ControlRequestRepository) -> None:
    repo.create(a_control_request())
    repo.claim("c1", "executor-1", now=CLOSE)
    repo.resolve("c1", "executor-1", C.COMPLETED, now=CLOSE)
    actions = [r.action for r in repo.audit.for_document(COLLECTION, "c1")]
    assert actions == [
        "control_request.create",
        "control_request.claim",
        "control_request.resolve",
    ]
