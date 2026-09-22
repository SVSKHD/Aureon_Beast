"""The claim, against a real PostgreSQL (§25-§32, plan §14-§16).

The one file in this phase where a bug costs money rather than data. Everything here is
about a single question: can one human confirmation ever arm two executions?
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aureon.models.enums import FailureCode, TradeRequestStatus, TransitionError
from aureon.models.market import QuoteSnapshot
from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.trade_requests import (
    COLLECTION,
    ClaimRejected,
    ConfirmationRejected,
    LeaseLost,
    TradeRequestRepository,
)
from tests.postgres.contention import interleave
from tests.postgres.factories import CLOSE, a_confirmed_request, a_trade_request

pytestmark = pytest.mark.postgres

S = TradeRequestStatus


def a_quote() -> QuoteSnapshot:
    return QuoteSnapshot(symbol="XAUUSD", bid=2403.4, ask=2403.6, captured_at=CLOSE)


@pytest.fixture
def repo(schema: Database) -> TradeRequestRepository:
    return TradeRequestRepository(schema)


# ── create ────────────────────────────────────────────────────────────────────


def test_a_request_survives_the_database_unchanged(repo: TradeRequestRepository) -> None:
    written = repo.create(a_trade_request())
    assert repo.get("r1") == written


def test_a_new_request_may_not_arrive_already_confirmed(repo: TradeRequestRepository) -> None:
    """It would skip the human entirely, which is what the whole chain exists to prevent."""
    with pytest.raises(ValueError):
        repo.create(a_confirmed_request())
    assert repo.get("r1") is None


def test_creating_the_same_request_twice_is_a_retry(repo: TradeRequestRepository) -> None:
    first = repo.create(a_trade_request())
    again = repo.create(a_trade_request(volume=99.0))
    assert again.volume == first.volume, "the retry overwrote the original"


def test_creating_writes_an_audit_row(repo: TradeRequestRepository) -> None:
    """§71, and in the SAME transaction: an audit row committed separately can be lost when
    the change rolls back, or survive when it does, and either way stops being evidence."""
    repo.create(a_trade_request())
    trail = repo.audit.for_document(COLLECTION, "r1")
    assert [record.action for record in trail] == ["trade_request.create"]
    assert trail[0].to_status == "requested"


# ── confirm (§27, §28) ────────────────────────────────────────────────────────


def test_only_the_requester_may_confirm(repo: TradeRequestRepository) -> None:
    """§27. Anyone else pressing the button would be authorising someone else's money."""
    repo.create(a_trade_request())
    with pytest.raises(ConfirmationRejected, match="did not request"):
        repo.confirm("r1", "someone-else", a_quote())
    stored = repo.get("r1")
    assert stored is not None and stored.status is S.REQUESTED


def test_confirming_sets_the_ttl_and_the_quote(repo: TradeRequestRepository) -> None:
    repo.create(a_trade_request())
    confirmed = repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=30, now=CLOSE)
    assert confirmed.status is S.CONFIRMED
    assert confirmed.expires_at == CLOSE + timedelta(seconds=30)
    assert confirmed.quote is not None and confirmed.quote.bid == 2403.4
    assert confirmed.confirmation_version == 1


def test_a_duplicate_click_is_a_no_op_not_a_second_confirmation(
    repo: TradeRequestRepository,
) -> None:
    """Discord can deliver the same click twice, and a second CONFIRMED transition would
    let one authorisation arm two executions."""
    repo.create(a_trade_request())
    first = repo.confirm("r1", "trader", a_quote(), now=CLOSE)
    again = repo.confirm("r1", "trader", a_quote(), now=CLOSE + timedelta(seconds=5))
    assert again.confirmation_version == first.confirmation_version == 1
    assert again.expires_at == first.expires_at, "the second click extended the TTL"


def test_confirming_something_past_requested_is_a_conflict(
    repo: TradeRequestRepository,
) -> None:
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), now=CLOSE)
    repo.claim("r1", "executor-1", now=CLOSE)
    with pytest.raises(ConfirmationRejected, match="not REQUESTED"):
        repo.confirm("r1", "trader", a_quote(), now=CLOSE)


# ── claim (§29) ───────────────────────────────────────────────────────────────


def test_a_claim_takes_the_lease(repo: TradeRequestRepository) -> None:
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), now=CLOSE)
    claimed = repo.claim("r1", "executor-1", lease_seconds=45, now=CLOSE)
    assert claimed.status is S.EXECUTING
    assert claimed.executor_instance_id == "executor-1"
    assert claimed.lease_expires_at == CLOSE + timedelta(seconds=45)
    assert claimed.execution_started_at == CLOSE


def test_an_unconfirmed_request_cannot_be_claimed(repo: TradeRequestRepository) -> None:
    """CLAUDE.md: execution only from CONFIRMED. This is that rule, at the storage layer."""
    repo.create(a_trade_request())
    with pytest.raises(ClaimRejected, match="not CONFIRMED"):
        repo.claim("r1", "executor-1", now=CLOSE)


def test_a_second_claim_on_a_claimed_request_is_refused(
    repo: TradeRequestRepository,
) -> None:
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), now=CLOSE)
    repo.claim("r1", "executor-1", now=CLOSE)
    with pytest.raises(ClaimRejected, match="not CONFIRMED"):
        repo.claim("r1", "executor-2", now=CLOSE)


def test_claiming_something_that_does_not_exist_is_refused(
    repo: TradeRequestRepository,
) -> None:
    with pytest.raises(ClaimRejected, match="no such"):
        repo.claim("nope", "executor-1", now=CLOSE)


# ── The stale confirmation, and why its write must commit ──────────────────────


def test_an_expired_confirmation_is_marked_stale_and_the_claim_refused(
    repo: TradeRequestRepository,
) -> None:
    """Both halves, because only having both makes it safe.

    The marking must COMMIT and the claim must be REFUSED. A version that raised inside the
    transaction would roll the marking back and leave the request CONFIRMED -- claimable
    later, at a price the human never saw.
    """
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=30, now=CLOSE)

    too_late = CLOSE + timedelta(seconds=31)
    with pytest.raises(ClaimRejected, match="FAILED_STALE"):
        repo.claim("r1", "executor-1", now=too_late)

    stored = repo.get("r1")
    assert stored is not None
    assert stored.status is S.FAILED_STALE, "the stale marking was rolled back"
    assert stored.failure_code is FailureCode.CONFIRMATION_EXPIRED


def test_a_stale_request_cannot_be_claimed_afterwards(
    repo: TradeRequestRepository,
) -> None:
    """The consequence the commit buys: once stale, never claimable.

    If the marking had rolled back, this second claim would succeed -- and it would send an
    order at whatever the market was doing minutes after the human looked.
    """
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=30, now=CLOSE)
    with pytest.raises(ClaimRejected):
        repo.claim("r1", "executor-1", now=CLOSE + timedelta(seconds=31))
    with pytest.raises(ClaimRejected, match="not CONFIRMED"):
        repo.claim("r1", "executor-2", now=CLOSE + timedelta(seconds=32))


def test_the_stale_marking_is_audited(repo: TradeRequestRepository) -> None:
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=30, now=CLOSE)
    with pytest.raises(ClaimRejected):
        repo.claim("r1", "executor-1", now=CLOSE + timedelta(seconds=31))
    actions = [r.action for r in repo.audit.for_document(COLLECTION, "r1")]
    assert "trade_request.claim_stale" in actions


# ── Two executors (§15) ───────────────────────────────────────────────────────


class _Signalling(TradeRequestRepository):
    """Announces when it holds its lock, and is still inside the transaction.

    Subclassed rather than hooked into production code: the signal fires after
    ``super()._locked`` has issued the real ``SELECT ... FOR UPDATE``.
    """

    def __init__(self, database: Database, signal) -> None:  # type: ignore[no-untyped-def]
        super().__init__(database)
        self._signal = signal

    def _locked(self, request_id: str, connection):  # type: ignore[no-untyped-def]
        row = super()._locked(request_id, connection)
        self._signal()
        return row


class _SignallingScan(TradeRequestRepository):
    """The same, for the scan path -- which locks through its own SELECT, not ``_locked``."""

    def __init__(self, database: Database, signal) -> None:  # type: ignore[no-untyped-def]
        super().__init__(database)
        self._signal = signal

    def _apply_claim(self, connection, current, executor_id, lease_seconds, moment):  # type: ignore[no-untyped-def]
        # By here the scan's ``FOR UPDATE SKIP LOCKED`` has already taken the row.
        self._signal()
        return super()._apply_claim(connection, current, executor_id, lease_seconds, moment)


def test_two_executors_claiming_one_request_produce_one_claim(schema: Database) -> None:
    """The guarantee the whole phase turns on, with a race that really overlaps.

    The first executor holds its ``FOR UPDATE`` lock while the second tries. With the lock
    the second WAITS, then re-reads a request that is no longer CONFIRMED and is refused.
    Without it, both read CONFIRMED and one human confirmation becomes two positions.

    An earlier version of this test started two threads and joined them, which almost never
    produced an overlap -- it passed with ``FOR UPDATE`` removed entirely (decision 352).
    """
    seed = TradeRequestRepository(schema)
    seed.create(a_trade_request())
    seed.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)

    outcome = interleave(
        lambda signal: _Signalling(schema, signal).claim("r1", "executor-1", now=CLOSE),
        lambda: TradeRequestRepository(schema).claim("r1", "executor-2", now=CLOSE),
    )

    assert len(outcome.succeeded) == 1, outcome
    assert len(outcome.failed) == 1, outcome
    assert isinstance(next(iter(outcome.errors.values())), ClaimRejected)

    stored = TradeRequestRepository(schema).get("r1")
    assert stored is not None
    assert stored.status is S.EXECUTING
    winner = next(iter(outcome.results.values()))
    assert stored.executor_instance_id == winner.executor_instance_id

    # Exactly one claim in the audit trail. Two would mean two sends were authorised.
    claims = [
        record
        for record in TradeRequestRepository(schema).audit.for_document(COLLECTION, "r1")
        if record.action == "trade_request.claim"
    ]
    assert len(claims) == 1, f"{len(claims)} claims were audited"


def test_two_executors_scanning_take_different_requests(schema: Database) -> None:
    """``SKIP LOCKED``: any confirmed request will do, so a locked one is skipped rather than
    queued behind.

    With the overlap forced, the first executor holds ``r1`` while the second scans. It must
    come back with ``r2`` -- not block, and not take ``r1`` as well.
    """
    seed = TradeRequestRepository(schema)
    for index, name in enumerate(("r1", "r2")):
        seed.create(a_trade_request(name))
        seed.confirm(
            name,
            "trader",
            a_quote(),
            confirmation_ttl_seconds=600,
            now=CLOSE + timedelta(seconds=index),
        )

    outcome = interleave(
        lambda signal: _SignallingScan(schema, signal).claim_next("executor-1", now=CLOSE),
        lambda: TradeRequestRepository(schema).claim_next("executor-2", now=CLOSE),
    )

    assert outcome.failed == [], outcome
    taken = sorted(result.request_id for result in outcome.results.values() if result)
    assert taken == ["r1", "r2"], f"taken={taken} (a scan that does not skip takes one)"


# ── claim_next (plan §15) ─────────────────────────────────────────────────────


def test_the_scan_claims_the_oldest_confirmation_first(repo: TradeRequestRepository) -> None:
    """``ORDER BY confirmed_at`` makes the queue fair; without it a request could starve
    behind a stream of newer ones."""
    for index, name in enumerate(("newer", "older")):
        repo.create(a_trade_request(name))
        repo.confirm(
            name,
            "trader",
            a_quote(),
            confirmation_ttl_seconds=600,
            now=CLOSE + timedelta(seconds=10 - index * 10),
        )
    claimed = repo.claim_next("executor-1", now=CLOSE + timedelta(seconds=20))
    assert claimed is not None and claimed.request_id == "older"


def test_the_scan_returns_none_on_an_empty_queue(repo: TradeRequestRepository) -> None:
    """``None`` rather than an exception: the executor asks every poll and an empty queue is
    the normal case."""
    assert repo.claim_next("executor-1", now=CLOSE) is None


def test_the_scan_ignores_unconfirmed_requests(repo: TradeRequestRepository) -> None:
    repo.create(a_trade_request())
    assert repo.claim_next("executor-1", now=CLOSE) is None


def test_the_scan_can_be_limited_to_symbols(repo: TradeRequestRepository) -> None:
    """9A runs two instruments; an executor may be scoped to one."""
    repo.create(a_trade_request("gold", symbol="XAUUSD"))
    repo.create(a_trade_request("silver", symbol="XAGUSD"))
    for name in ("gold", "silver"):
        repo.confirm(name, "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    claimed = repo.claim_next("executor-1", symbols=["XAGUSD"], now=CLOSE)
    assert claimed is not None and claimed.request_id == "silver"


def test_the_scan_marks_a_stale_confirmation_and_returns_none(
    repo: TradeRequestRepository,
) -> None:
    """The executor did nothing wrong and has no decision to make, so it gets ``None``
    rather than an exception -- but the marking still commits, so the request cannot be
    picked up later."""
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=30, now=CLOSE)
    assert repo.claim_next("executor-1", now=CLOSE + timedelta(seconds=31)) is None
    stored = repo.get("r1")
    assert stored is not None and stored.status is S.FAILED_STALE


# ── The lease ─────────────────────────────────────────────────────────────────


def test_a_live_lease_can_be_renewed(repo: TradeRequestRepository) -> None:
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", lease_seconds=60, now=CLOSE)
    renewed = repo.renew_lease(
        "r1", "executor-1", lease_seconds=60, now=CLOSE + timedelta(seconds=30)
    )
    assert renewed.lease_expires_at == CLOSE + timedelta(seconds=90)


def test_an_expired_lease_cannot_be_revived(repo: TradeRequestRepository) -> None:
    """Another worker may have taken over, and reviving the old claim would put two workers
    on one request."""
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", lease_seconds=60, now=CLOSE)
    with pytest.raises(LeaseLost):
        repo.renew_lease("r1", "executor-1", now=CLOSE + timedelta(seconds=61))


def test_someone_elses_lease_cannot_be_renewed(repo: TradeRequestRepository) -> None:
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", now=CLOSE)
    with pytest.raises(LeaseLost):
        repo.renew_lease("r1", "executor-2", now=CLOSE)


def test_abandoned_requests_are_findable(repo: TradeRequestRepository) -> None:
    """§5's reconciliation input: an executor took the claim and did not come back.

    The answer for each is MT5's, never a resend -- which is what C-1's RECONCILING exists
    for.
    """
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", lease_seconds=60, now=CLOSE)
    assert repo.expired_leases(now=CLOSE + timedelta(seconds=30)) == []
    abandoned = repo.expired_leases(now=CLOSE + timedelta(seconds=61))
    assert [r.request_id for r in abandoned] == ["r1"]


# ── resolve (§32) ─────────────────────────────────────────────────────────────


def test_the_lease_holder_can_resolve(repo: TradeRequestRepository) -> None:
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", now=CLOSE)
    filled = repo.resolve(
        "r1", "executor-1", S.FILLED, updates={"fill_price": 2403.6}, now=CLOSE
    )
    assert filled.status is S.FILLED
    assert filled.fill_price == 2403.6


def test_a_non_holder_cannot_resolve(repo: TradeRequestRepository) -> None:
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", now=CLOSE)
    with pytest.raises(LeaseLost):
        repo.resolve("r1", "executor-2", S.FILLED, now=CLOSE)


def test_reconciliation_may_resolve_without_a_lease(repo: TradeRequestRepository) -> None:
    """Repairing a request abandoned by a dead executor is precisely the case where no live
    lease exists; requiring one would leave such requests stuck in EXECUTING for ever."""
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", lease_seconds=60, now=CLOSE)
    repaired = repo.resolve(
        "r1",
        "reconciler",
        S.FILLED,
        reconciliation=True,
        now=CLOSE + timedelta(seconds=300),
    )
    assert repaired.status is S.FILLED
    assert repaired.last_reconciled_at is not None


def test_an_illegal_outcome_is_refused(repo: TradeRequestRepository) -> None:
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", now=CLOSE)
    repo.resolve("r1", "executor-1", S.FILLED, now=CLOSE)
    with pytest.raises(TransitionError):
        repo.resolve("r1", "executor-1", S.PENDING, reconciliation=True, now=CLOSE)


def test_resolving_with_nothing_to_write_is_a_no_op(repo: TradeRequestRepository) -> None:
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", now=CLOSE)
    repo.resolve("r1", "executor-1", S.FILLED, now=CLOSE)
    before = len(repo.audit.for_document(COLLECTION, "r1"))
    repo.resolve("r1", "executor-1", S.FILLED, reconciliation=True, now=CLOSE)
    assert len(repo.audit.for_document(COLLECTION, "r1")) == before


def test_an_unchanged_status_still_writes_its_updates(repo: TradeRequestRepository) -> None:
    """The bug an earlier version had: returning early on any unchanged status silently
    dropped the updates that came with it -- including the ``comment_token`` the executor
    stamps before sending, leaving reconciliation nothing to search for."""
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", now=CLOSE)
    stamped = repo.resolve(
        "r1", "executor-1", S.EXECUTING, updates={"comment_token": "AUR-abc123"}, now=CLOSE
    )
    assert stamped.comment_token == "AUR-abc123"
    assert stamped.status is S.EXECUTING


def test_partially_filled_is_re_entrant(repo: TradeRequestRepository) -> None:
    """Volume arrives in several deals, so the state can be re-entered as each lands."""
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", now=CLOSE)
    repo.resolve("r1", "executor-1", S.PARTIALLY_FILLED, updates={"filled_volume": 0.04}, now=CLOSE)
    again = repo.resolve(
        "r1", "executor-1", S.PARTIALLY_FILLED, updates={"filled_volume": 0.08}, now=CLOSE
    )
    assert again.filled_volume == 0.08


# ── C-1: the uncertain send ───────────────────────────────────────────────────


def test_an_uncertain_send_can_be_recorded_as_reconciling(
    repo: TradeRequestRepository,
) -> None:
    """C-1. ``order_send`` raised or timed out: nobody knows whether an order exists.

    ``FAILED`` would be a claim that none does, and a failed request invites a resend --
    which is how one intent becomes two positions.
    """
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", now=CLOSE)
    uncertain = repo.resolve("r1", "executor-1", S.RECONCILING, now=CLOSE)
    assert uncertain.status is S.RECONCILING


def test_reconciling_can_be_resolved_from_the_broker(repo: TradeRequestRepository) -> None:
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", now=CLOSE)
    repo.resolve("r1", "executor-1", S.RECONCILING, now=CLOSE)
    found = repo.resolve(
        "r1", "reconciler", S.FILLED, reconciliation=True, updates={"order_ticket": 123456789012}
    )
    assert found.status is S.FILLED
    assert found.order_ticket == 123456789012, "a ten-digit ticket did not survive"


def test_reconciling_can_never_become_a_plain_failure(repo: TradeRequestRepository) -> None:
    """The distinction the state exists to keep: by this point a send has been ATTEMPTED."""
    repo.create(a_trade_request())
    repo.confirm("r1", "trader", a_quote(), confirmation_ttl_seconds=600, now=CLOSE)
    repo.claim("r1", "executor-1", now=CLOSE)
    repo.resolve("r1", "executor-1", S.RECONCILING, now=CLOSE)
    with pytest.raises(TransitionError):
        repo.resolve("r1", "reconciler", S.FAILED, reconciliation=True)


# ── C-2: sl and tp are the trader's ───────────────────────────────────────────


def test_stops_are_copied_through_and_never_invented(repo: TradeRequestRepository) -> None:
    """C-2. What the trader typed, or nothing at all.

    A request with no stops must reach the broker with no stops -- a default here would be
    a trading decision this system is not allowed to make.
    """
    repo.create(a_trade_request("with", sl=2395.0, tp=2420.0))
    repo.create(a_trade_request("without"))

    stored = repo.get("with")
    assert stored is not None and (stored.sl, stored.tp) == (2395.0, 2420.0)

    bare = repo.get("without")
    assert bare is not None and bare.sl is None and bare.tp is None


def test_the_repository_never_assigns_a_stop() -> None:
    """Structural, not behavioural: the module must contain no assignment to sl or tp.

    A behavioural test passes as long as the default happens to be ``None``; this fails the
    moment anybody writes ``sl = ...`` anywhere in the money path, which is the thing C-2
    actually forbids.
    """
    import ast
    import inspect

    from aureon.storage.postgres.repositories import trade_requests

    tree = ast.parse(inspect.getsource(trade_requests))
    assigned: list[str] = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id in {"sl", "tp"}:
                assigned.append(f"line {node.lineno}")
            if isinstance(target, ast.Attribute) and target.attr in {"sl", "tp"}:
                assigned.append(f"line {node.lineno}")
    assert not assigned, f"the money path assigns a stop: {assigned}"


# ── The audit trail is part of the transaction ────────────────────────────────


def test_a_rolled_back_change_leaves_no_audit_row(repo: TradeRequestRepository) -> None:
    """§71. The audit row must live or die with the change it describes.

    A row committed separately can survive a rollback, and then the log says a thing
    happened that did not -- which is worse than no log, because the log is what an
    investigation trusts. The confirm below fails on the wrong user AFTER the transaction has
    opened, so if the audit write were outside it, the row would remain.
    """
    repo.create(a_trade_request())
    before = len(repo.audit.for_document(COLLECTION, "r1"))

    with pytest.raises(ConfirmationRejected):
        repo.confirm("r1", "someone-else", a_quote(), now=CLOSE)

    assert len(repo.audit.for_document(COLLECTION, "r1")) == before


def test_an_audit_row_cannot_be_overwritten(repo: TradeRequestRepository) -> None:
    """Append-only, enforced by the insert rather than by convention.

    An audit id that already exists is not a retry to absorb but a bug or a replay, and
    overwriting the first row would destroy the only record of what actually happened first.
    """
    from sqlalchemy.exc import IntegrityError

    from aureon.models.audit import AuditRecord

    record = AuditRecord(audit_id="fixed", actor="trader", action="trade_request.create")
    repo.audit.append(record)
    with pytest.raises(IntegrityError):
        repo.audit.append(record.model_copy(update={"action": "something.else"}))

    stored = repo.audit.get("fixed")
    assert stored is not None and stored.action == "trade_request.create"
