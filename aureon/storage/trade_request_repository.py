"""Trade request state, guarded by Firestore transactions (§25-§29, §60).

This is where exactly-once execution is actually enforced. Everything else in the
execution path is arrangement around four transactional operations:

* ``create``   -- a request appears, in ``REQUESTED``;
* ``confirm``  -- a human authorises it, once, and only the requester may;
* ``claim``    -- exactly one executor takes ownership, by taking a lease;
* ``resolve``  -- the lease holder records the outcome.

Each runs inside a single Firestore transaction that **reads the document, checks the
precondition, and writes** -- so two workers racing on the same request cannot both
succeed. The read is what makes it safe: Firestore aborts the transaction whose read
set changed underneath it, which turns "check then act" into one atomic step.

## Audit rows go in the same transaction

Not afterwards (§60). Writing the audit row separately leaves a window where a crash
loses the record of a state change that did happen -- and on the execution path that
record is the only account of what was attempted with real money.

## What is deliberately NOT here

No broker calls. This module moves state; ``ExecutionWorker`` moves money, and only
after a successful ``claim``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from aureon.models.audit import AuditRecord
from aureon.models.base import to_utc, utc_now
from aureon.models.enums import (
    TERMINAL_REQUEST_STATUSES,
    FailureCode,
    TradeRequestStatus,
    TransitionError,
    assert_trade_request_transition,
)
from aureon.models.market import QuoteSnapshot
from aureon.models.trade import TradeRequest
from aureon.storage import paths

log = logging.getLogger(__name__)


#: The only fields a terminal document may still take. Both are observational: they
#: record when something was last looked at, and assert nothing about what happened.
RECONCILIATION_ONLY_FIELDS: frozenset[str] = frozenset(
    {"last_reconciled_at", "last_synced_at"}
)


class TerminalWriteRejected(RuntimeError):
    """An attempt to write to a request or trade whose status is terminal."""


class ConfirmationRejected(RuntimeError):
    """A confirmation attempt was refused (wrong user, wrong state, expired)."""


class ClaimRejected(RuntimeError):
    """A claim attempt was refused: someone else holds it, or it is no longer CONFIRMED."""


class LeaseLost(RuntimeError):
    """A resolve was attempted without a live lease.

    Raised rather than silently allowed, because a worker whose lease expired may have
    been superseded -- and two workers resolving one request is how a double trade gets
    recorded.
    """


class TradeRequestRepository:
    """Transactional access to ``trade_requests``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ref(self, request_id: str):
        return self._client.document(paths.trade_request_path(request_id))

    def _audit_ref(self, audit_id: str):
        return self._client.document(paths.audit_path(audit_id))

    def _transaction(self):
        return self._client.transaction()

    @staticmethod
    def _run(transaction: Any, fn: Callable[..., Any], *args: Any) -> Any:
        """Run ``fn`` inside a Firestore transaction.

        ``google.cloud.firestore.transactional`` wraps the callable and retries it if
        the read set changed, which is exactly the contention behaviour the claim
        depends on.
        """
        from google.cloud import firestore

        return firestore.transactional(fn)(transaction, *args)

    def _write_audit(
        self,
        transaction: Any,
        *,
        actor: str,
        action: str,
        request_id: str,
        from_status: TradeRequestStatus | None,
        to_status: TradeRequestStatus | None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
        reconciliation: bool = False,
    ) -> None:
        """Queue an audit row inside the caller's transaction (§60)."""
        record = AuditRecord(
            audit_id=uuid.uuid4().hex,
            actor=actor,
            action=action,
            collection=paths.TRADE_REQUESTS,
            document_id=request_id,
            from_status=from_status.value if from_status else None,
            to_status=to_status.value if to_status else None,
            reason=reason,
            detail=detail or {},
            reconciliation=reconciliation,
        )
        transaction.set(self._audit_ref(record.audit_id), record.model_dump(mode="json"))

    # ── Reading ───────────────────────────────────────────────────────────────

    def get(self, request_id: str) -> TradeRequest | None:
        snapshot = self._ref(request_id).get()
        if not getattr(snapshot, "exists", False):
            return None
        return TradeRequest.model_validate(snapshot.to_dict())

    def list_by_status(
        self, status: TradeRequestStatus | list[TradeRequestStatus], *, limit: int = 100
    ) -> list[TradeRequest]:
        """Requests in one or more statuses.

        The executor polls ``CONFIRMED`` and reconciliation sweeps ``EXECUTING``.
        """
        statuses = [status] if isinstance(status, TradeRequestStatus) else list(status)
        found: list[TradeRequest] = []
        for one in statuses:
            query = self._client.collection(paths.TRADE_REQUESTS).limit(limit)
            query = _where(query, "status", "==", one.value)
            found.extend(
                TradeRequest.model_validate(doc.to_dict()) for doc in query.stream()
            )
        return found

    # ── create ────────────────────────────────────────────────────────────────

    def create(self, request: TradeRequest) -> TradeRequest:
        """Write a new request in ``REQUESTED``.

        Rejects a non-REQUESTED initial status: a request that appeared already
        confirmed would skip the human entirely.
        """
        if request.status is not TradeRequestStatus.REQUESTED:
            raise ValueError(
                f"a new request must start in REQUESTED, got {request.status}"
            )

        def txn(transaction: Any) -> TradeRequest:
            existing = self._ref(request.request_id).get(transaction=transaction)
            if getattr(existing, "exists", False):
                # Idempotent: the same id twice is a retry, not a second request.
                return TradeRequest.model_validate(existing.to_dict())
            transaction.set(
                self._ref(request.request_id), request.model_dump(mode="json")
            )
            self._write_audit(
                transaction,
                actor=request.requested_by,
                action="trade_request.create",
                request_id=request.request_id,
                from_status=None,
                to_status=TradeRequestStatus.REQUESTED,
                detail={"symbol": request.symbol, "volume": request.volume},
            )
            return request

        return self._run(self._transaction(), txn)

    # ── confirm ───────────────────────────────────────────────────────────────

    def confirm(
        self,
        request_id: str,
        user_id: str,
        quote: QuoteSnapshot,
        *,
        confirmation_ttl_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> TradeRequest:
        """REQUESTED → CONFIRMED, once, and only by the requester (§27, §28).

        Repeat calls are a **no-op returning the current document**, not an error and
        not a second transition. Discord can deliver the same button click twice, and a
        second CONFIRMED transition would let one authorisation arm two executions.
        """
        moment = to_utc(now or utc_now())

        def txn(transaction: Any) -> TradeRequest:
            snapshot = self._ref(request_id).get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                raise ConfirmationRejected(f"no such trade request {request_id}")
            current = TradeRequest.model_validate(snapshot.to_dict())

            if current.status is not TradeRequestStatus.REQUESTED:
                # Already past REQUESTED. If it is CONFIRMED this is the duplicate
                # click; anything else is a genuine conflict the caller must see.
                if current.status is TradeRequestStatus.CONFIRMED:
                    return current
                raise ConfirmationRejected(
                    f"{request_id} is {current.status.value}, not REQUESTED"
                )
            if current.requested_by != user_id:
                # §27: only the requester may confirm. Anyone else pressing the button
                # would be authorising someone else's money.
                raise ConfirmationRejected(
                    f"user {user_id} did not request {request_id} "
                    f"(requested by {current.requested_by})"
                )

            assert_trade_request_transition(current.status, TradeRequestStatus.CONFIRMED)
            updated = current.model_copy(
                update={
                    "status": TradeRequestStatus.CONFIRMED,
                    "confirmed_at": moment,
                    "confirmed_by": user_id,
                    "expires_at": moment + timedelta(seconds=confirmation_ttl_seconds),
                    "confirmation_version": current.confirmation_version + 1,
                    "quote": quote,
                }
            )
            transaction.set(self._ref(request_id), updated.model_dump(mode="json"))
            self._write_audit(
                transaction,
                actor=user_id,
                action="trade_request.confirm",
                request_id=request_id,
                from_status=current.status,
                to_status=TradeRequestStatus.CONFIRMED,
                detail={
                    "confirmation_version": updated.confirmation_version,
                    "bid": quote.bid,
                    "ask": quote.ask,
                },
            )
            return updated

        return self._run(self._transaction(), txn)

    # ── claim ─────────────────────────────────────────────────────────────────

    def claim(
        self,
        request_id: str,
        executor_id: str,
        *,
        lease_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> TradeRequest:
        """CONFIRMED → EXECUTING, for exactly one executor (§29).

        The gate on every trade. Two workers calling this concurrently: Firestore
        aborts and retries the loser, which then re-reads a document that is no longer
        CONFIRMED and is refused.

        A confirmation past its TTL is moved to ``FAILED_STALE`` **inside the same
        transaction** and the claim is refused -- so a request that sat too long cannot
        be picked up later at a price the human never saw.
        """
        moment = to_utc(now or utc_now())

        def txn(transaction: Any) -> TradeRequest:
            snapshot = self._ref(request_id).get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                raise ClaimRejected(f"no such trade request {request_id}")
            current = TradeRequest.model_validate(snapshot.to_dict())

            if current.status is not TradeRequestStatus.CONFIRMED:
                raise ClaimRejected(
                    f"{request_id} is {current.status.value}, not CONFIRMED"
                )

            if current.confirmation_expired(now=moment):
                # Write the FAILED_STALE transition and return a marker. Raising HERE
                # would abort the transaction and roll the write back, leaving the
                # request CONFIRMED -- claimable later at a price the human never saw.
                # The refusal is raised by the caller, after the commit.
                assert_trade_request_transition(
                    current.status, TradeRequestStatus.FAILED_STALE
                )
                stale = current.model_copy(
                    update={
                        "status": TradeRequestStatus.FAILED_STALE,
                        "failure_code": FailureCode.CONFIRMATION_EXPIRED,
                        "failure_message": (
                            f"confirmation expired at {current.expires_at}; "
                            f"claimed at {moment.isoformat()}"
                        ),
                    }
                )
                transaction.set(self._ref(request_id), stale.model_dump(mode="json"))
                self._write_audit(
                    transaction,
                    actor=executor_id,
                    action="trade_request.claim_stale",
                    request_id=request_id,
                    from_status=current.status,
                    to_status=TradeRequestStatus.FAILED_STALE,
                    reason="confirmation expired before an executor claimed it",
                )
                return stale

            assert_trade_request_transition(current.status, TradeRequestStatus.EXECUTING)
            claimed = current.model_copy(
                update={
                    "status": TradeRequestStatus.EXECUTING,
                    "executor_instance_id": executor_id,
                    "lease_expires_at": moment + timedelta(seconds=lease_seconds),
                    "execution_started_at": moment,
                }
            )
            transaction.set(self._ref(request_id), claimed.model_dump(mode="json"))
            self._write_audit(
                transaction,
                actor=executor_id,
                action="trade_request.claim",
                request_id=request_id,
                from_status=current.status,
                to_status=TradeRequestStatus.EXECUTING,
                detail={"lease_seconds": lease_seconds},
            )
            return claimed

        claimed = self._run(self._transaction(), txn)
        if claimed.status is TradeRequestStatus.FAILED_STALE:
            # Committed above, refused here -- see the comment inside the transaction.
            raise ClaimRejected(
                f"{request_id} confirmation expired; marked FAILED_STALE"
            )
        return claimed

    # ── lease renewal ─────────────────────────────────────────────────────────

    def renew_lease(
        self,
        request_id: str,
        executor_id: str,
        *,
        lease_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> TradeRequest:
        """Extend a live lease while a send is in flight.

        Refuses to renew an already-expired lease: another worker may have taken over,
        and reviving the old claim would put two workers on one request.
        """
        moment = to_utc(now or utc_now())

        def txn(transaction: Any) -> TradeRequest:
            snapshot = self._ref(request_id).get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                raise LeaseLost(f"no such trade request {request_id}")
            current = TradeRequest.model_validate(snapshot.to_dict())
            if not current.lease_held_by(executor_id, now=moment):
                raise LeaseLost(
                    f"{executor_id} does not hold a live lease on {request_id} "
                    f"(holder={current.executor_instance_id}, "
                    f"expires={current.lease_expires_at})"
                )
            renewed = current.model_copy(
                update={"lease_expires_at": moment + timedelta(seconds=lease_seconds)}
            )
            transaction.set(self._ref(request_id), renewed.model_dump(mode="json"))
            return renewed

        return self._run(self._transaction(), txn)

    # ── resolve ───────────────────────────────────────────────────────────────

    def resolve(
        self,
        request_id: str,
        executor_id: str,
        new_status: TradeRequestStatus,
        *,
        updates: dict[str, Any] | None = None,
        failure_code: FailureCode | None = None,
        failure_message: str | None = None,
        reconciliation: bool = False,
        now: datetime | None = None,
    ) -> TradeRequest:
        """Record an outcome, if the caller still holds the lease (§32).

        ``reconciliation=True`` bypasses the lease check, and only reconciliation may
        use it: repairing a request abandoned by a dead executor is precisely the case
        where no live lease exists, and requiring one would leave such requests stuck
        in ``EXECUTING`` forever.
        """
        moment = to_utc(now or utc_now())

        def txn(transaction: Any) -> TradeRequest:
            snapshot = self._ref(request_id).get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                raise LeaseLost(f"no such trade request {request_id}")
            current = TradeRequest.model_validate(snapshot.to_dict())

            if not reconciliation and not current.lease_held_by(executor_id, now=moment):
                raise LeaseLost(
                    f"{executor_id} cannot resolve {request_id}: lease held by "
                    f"{current.executor_instance_id} until {current.lease_expires_at}"
                )

            # Idempotent only when there is genuinely nothing to write. An earlier
            # version returned early on any unchanged status, which silently dropped the
            # updates that came with it -- including the comment_token the executor
            # stamps before sending, leaving reconciliation nothing to search for.
            # PARTIALLY_FILLED is additionally re-entrant as each further fill lands.
            payload_keys = set(updates or ())
            if failure_code is not None:
                payload_keys.add("failure_code")
            if failure_message is not None:
                payload_keys.add("failure_message")

            nothing_to_write = (
                current.status is new_status
                and new_status is not TradeRequestStatus.PARTIALLY_FILLED
                and not updates
                and failure_code is None
                and failure_message is None
            )
            if nothing_to_write:
                return current

            if current.status is not new_status:
                # A status CHANGE is the transition table's business, terminal or not,
                # so an illegal edge out of a terminal status keeps reporting itself as
                # an illegal edge.
                try:
                    assert_trade_request_transition(current.status, new_status)
                except TransitionError as exc:
                    raise LeaseLost(str(exc)) from exc
            elif current.status in TERMINAL_REQUEST_STATUSES:
                # The gap the table cannot see: a SAME-status write carrying `updates`.
                # A terminal document is history, and this path could rewrite a FILLED
                # request's volume or a FAILED one's reason. The only write it may still
                # take is reconciliation stamping when it last looked, which changes no
                # claim about what happened (§58).
                disallowed = sorted(set(payload_keys) - RECONCILIATION_ONLY_FIELDS)
                if disallowed:
                    raise TerminalWriteRejected(
                        f"{request_id} is {current.status.value}, which is terminal; "
                        f"refusing to write {disallowed}. Only "
                        f"{sorted(RECONCILIATION_ONLY_FIELDS)} may still be written."
                    )

            payload: dict[str, Any] = {"status": new_status, **(updates or {})}
            if failure_code is not None:
                payload["failure_code"] = failure_code
            if failure_message is not None:
                payload["failure_message"] = failure_message
            resolved = current.model_copy(update=payload)

            transaction.set(self._ref(request_id), resolved.model_dump(mode="json"))
            self._write_audit(
                transaction,
                actor="reconciliation" if reconciliation else executor_id,
                action="trade_request.resolve",
                request_id=request_id,
                from_status=current.status,
                to_status=new_status,
                reason=failure_message,
                detail={
                    "failure_code": failure_code.value if failure_code else None,
                    **{k: v for k, v in (updates or {}).items() if _auditable(v)},
                },
                reconciliation=reconciliation,
            )
            return resolved

        return self._run(self._transaction(), txn)


def _where(query: Any, field: str, op: str, value: Any) -> Any:
    """Apply a filter, preferring the modern ``FieldFilter`` form.

    Positional ``where()`` is deprecated in the Firestore SDK and warns on every call.
    Falls back to the positional form so an in-memory double used elsewhere in the
    tests, which implements only the simple signature, still works.
    """
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter

        return query.where(filter=FieldFilter(field, op, value))
    except (ImportError, TypeError):
        return query.where(field, op, value)


def _auditable(value: Any) -> bool:
    """Whether a value is simple enough to copy into an audit detail map."""
    return isinstance(value, (str, int, float, bool, type(None)))

    # ── Listeners ─────────────────────────────────────────────────────────────
    #
    # These live here because CLAUDE.md funnels Firestore access through the
    # repositories and a boundary test now enforces it. The callers used to build the
    # query themselves -- the executor by reaching into this class's private _client,
    # which put the repository's write discipline one attribute access from being
    # sidestepped (decision 107).

    def watch_document(self, request_id: str, on_change: Any) -> Any:
        """Watch one request for status changes. Returns the watch handle.

        Used by Discord to report the outcome of a request from the document rather than
        from a button's return value: every terminal status renders, failures included,
        because a human who authorised real money and heard nothing back has been failed
        worse than one who was told it was rejected (§8).
        """
        return self._ref(request_id).on_snapshot(on_change)

    def watch_confirmed(self, on_change: Any) -> Any:
        """Watch for CONFIRMED requests. Returns the watch handle.

        Best-effort: the executor's poll is the actual guarantee, and this only shortens
        the latency between a human confirming and the order going out.
        """
        query = _where(
            self._client.collection(paths.TRADE_REQUESTS),
            "status",
            "==",
            TradeRequestStatus.CONFIRMED.value,
        )
        return query.on_snapshot(on_change)
