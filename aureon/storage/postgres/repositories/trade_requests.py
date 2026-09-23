"""Trade requests, and the claim that makes execution exactly-once (§25-§32, plan §14-§16).

The whole execution path is arrangement around four transactional operations:

* ``create``  -- a request appears, in REQUESTED;
* ``confirm`` -- a human authorises it, once, and only the requester may;
* ``claim``   -- exactly one executor takes ownership, by taking a lease;
* ``resolve`` -- the lease holder records the outcome.

**§15's claim, and why two shapes of it exist.** ``claim_next`` is the plan's scan:

    SELECT ... FROM trade_requests
     WHERE status = 'CONFIRMED' AND (lease is free or expired)
     ORDER BY confirmed_at
       FOR UPDATE SKIP LOCKED
     LIMIT 1

``SKIP LOCKED`` buys THROUGHPUT, not correctness, and that was measured rather than
assumed (decision 353). Under READ COMMITTED a plain ``FOR UPDATE`` scan also ends up taking
a different row: the second scanner blocks, PostgreSQL re-evaluates the qualification once
the first commits, finds the row no longer CONFIRMED and moves on. So the guarantee here is
that an executor never WAITS behind another's claim -- which matters when a send can take
seconds and the poll interval is two -- and the exactly-once guarantee comes from the
status check inside ``_apply_claim``, not from the lock mode.

It is still exactly the wrong mode for a setup (see ``setups.py``), where the two writers are
about the same row and skipping would make the loser conclude there was nothing to do.

``ORDER BY confirmed_at`` makes the queue fair, so a request cannot starve behind a stream of
newer ones.

``claim(request_id, ...)`` claims a NAMED request, for the path where Discord has just
confirmed one and the executor is told which. Both go through the same locking read and the
same guards, because two claim implementations would be two chances to get exactly-once
wrong.

**The stale-confirmation subtlety is load-bearing and easy to destroy.** A confirmation past
its TTL is moved to ``FAILED_STALE`` and that write must COMMIT; the refusal is raised
afterwards, by the caller. Raising inside the transaction would roll the marking back and
leave the request CONFIRMED -- claimable later, at a price the human never saw. This is
inherited verbatim from the Firestore version, comment and all, because it is the kind of
correctness that is invisible until it costs money.

**C-2: ``sl`` and ``tp`` are never written here.** They arrive on the model from the Discord
request handler, carrying values a trader typed. Nothing in this repository computes,
defaults or adjusts them, and a boundary test enforces that no module outside
``aureon/discord/commands/`` assigns them. A stop this system chose would be a trading
decision taken by a detection, which is the one thing it must never do.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from aureon.models.audit import AuditRecord
from aureon.models.base import to_utc, utc_now
from aureon.models.enums import (
    FailureCode,
    TradeRequestStatus,
    assert_trade_request_transition,
)
from aureon.models.market import QuoteSnapshot
from aureon.models.trade import TradeRequest
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.audit import AuditRepository
from aureon.storage.postgres.repositories.base import PostgresRepository

#: The label written into ``audit_logs.collection``, derived from the table rather
#: than spelled as a literal. Two reasons: the label can never drift from the table it
#: names, and §83's guard against bare collection literals keeps covering this module
#: (decision 356).
COLLECTION = tables.TradeRequest.__tablename__


class ClaimRejected(RuntimeError):
    """This executor may not claim this request."""


class ConfirmationRejected(RuntimeError):
    """This user may not confirm this request, or it is past confirming."""


class LeaseLost(RuntimeError):
    """The caller does not hold a live lease on this request."""


class TradeRequestRepository(PostgresRepository):
    """The money path's storage. Every method here is one transaction."""

    table = tables.TradeRequest.__table__

    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self.audit = AuditRepository(database)

    # ── create ────────────────────────────────────────────────────────────────

    def create(self, request: TradeRequest) -> TradeRequest:
        """Write a new request in REQUESTED.

        Refuses a non-REQUESTED initial status: a request that appeared already confirmed
        would skip the human entirely, which is the one thing the whole chain exists to
        prevent.
        """
        if request.status is not TradeRequestStatus.REQUESTED:
            raise ValueError(f"a new request must start in REQUESTED, got {request.status}")

        with self._db.transaction() as connection:
            existing = self._locked(request.request_id, connection)
            if existing is not None:
                # Idempotent: the same id twice is a retry, not a second request.
                return TradeRequest.model_validate(self._to_model_dict(existing))
            self._upsert(self._to_row(request), connection=connection)
            self._audit(
                connection,
                actor=request.requested_by,
                action="trade_request.create",
                request_id=request.request_id,
                from_status=None,
                to_status=TradeRequestStatus.REQUESTED,
                detail={"symbol": request.symbol, "volume": request.volume},
            )
        return request

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

        A repeat call is a no-op returning the current row, not an error and not a second
        transition: Discord can deliver the same button click twice, and a second CONFIRMED
        transition would let one authorisation arm two executions.
        """
        moment = to_utc(now or utc_now())

        with self._db.transaction() as connection:
            row = self._locked(request_id, connection)
            if row is None:
                raise ConfirmationRejected(f"no such trade request {request_id}")
            current = TradeRequest.model_validate(self._to_model_dict(row))

            if current.status is not TradeRequestStatus.REQUESTED:
                if current.status is TradeRequestStatus.CONFIRMED:
                    return current  # the duplicate click
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
            self._upsert(self._to_row(updated), connection=connection)
            self._audit(
                connection,
                actor=user_id,
                action="trade_request.confirm",
                request_id=request_id,
                from_status=current.status,
                to_status=TradeRequestStatus.CONFIRMED,
                detail={"confirmation_version": updated.confirmation_version},
            )
            return updated

    # ── claim (§15) ───────────────────────────────────────────────────────────

    def claim(
        self,
        request_id: str,
        executor_id: str,
        *,
        lease_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> TradeRequest:
        """CONFIRMED → EXECUTING, for exactly one executor (§29).

        The gate on every trade. Two executors calling this concurrently both block on the
        same ``FOR UPDATE`` row; the loser then re-reads a request that is no longer
        CONFIRMED and is refused.
        """
        moment = to_utc(now or utc_now())
        stale: TradeRequest | None = None

        with self._db.transaction() as connection:
            row = self._locked(request_id, connection)
            if row is None:
                raise ClaimRejected(f"no such trade request {request_id}")
            current = TradeRequest.model_validate(self._to_model_dict(row))
            claimed, stale = self._apply_claim(
                connection, current, executor_id, lease_seconds, moment
            )

        if stale is not None:
            # Committed above, refused here -- see ``_apply_claim``.
            raise ClaimRejected(f"{request_id} confirmation expired; marked FAILED_STALE")
        return claimed  # type: ignore[return-value]

    def claim_next(
        self,
        executor_id: str,
        *,
        lease_seconds: float = 60.0,
        symbols: list[str] | None = None,
        now: datetime | None = None,
    ) -> TradeRequest | None:
        """Claim the oldest claimable CONFIRMED request, or return ``None`` (plan §15).

        ``None`` rather than an exception for "nothing to do": the executor's loop asks
        this every poll, and an empty queue is the normal case.

        A stale confirmation found here is marked FAILED_STALE and the method returns
        ``None`` rather than raising -- the executor did nothing wrong and has no decision
        to make, and the next poll will pick up the next request. The marking still commits,
        for the same reason as in ``claim``.
        """
        moment = to_utc(now or utc_now())

        with self._db.transaction() as connection:
            statement = (
                select(self.table)
                .where(self.table.c.status == TradeRequestStatus.CONFIRMED.value)
                .where(
                    or_(
                        self.table.c.lease_expires_at.is_(None),
                        self.table.c.lease_expires_at <= moment,
                    )
                )
                .order_by(self.table.c.confirmed_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if symbols:
                statement = statement.where(self.table.c.symbol.in_(symbols))
            row = connection.execute(statement).mappings().first()
            if row is None:
                return None
            current = TradeRequest.model_validate(self._to_model_dict(row))
            claimed, stale = self._apply_claim(
                connection, current, executor_id, lease_seconds, moment
            )
        return None if stale is not None else claimed

    def _apply_claim(
        self,
        connection: Any,
        current: TradeRequest,
        executor_id: str,
        lease_seconds: float,
        moment: datetime,
    ) -> tuple[TradeRequest | None, TradeRequest | None]:
        """The guards and the writes both claim paths share. ``(claimed, stale)``.

        Exactly one of the two is not ``None``. Shared rather than duplicated because two
        implementations of exactly-once are two chances to get it wrong.
        """
        if current.status is not TradeRequestStatus.CONFIRMED:
            raise ClaimRejected(f"{current.request_id} is {current.status.value}, not CONFIRMED")

        if current.confirmation_expired(now=moment):
            # Write the FAILED_STALE transition and RETURN it. Raising here would abort the
            # transaction and roll this write back, leaving the request CONFIRMED --
            # claimable later at a price the human never saw. The caller raises, after the
            # commit.
            assert_trade_request_transition(current.status, TradeRequestStatus.FAILED_STALE)
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
            self._upsert(self._to_row(stale), connection=connection)
            self._audit(
                connection,
                actor=executor_id,
                action="trade_request.claim_stale",
                request_id=current.request_id,
                from_status=current.status,
                to_status=TradeRequestStatus.FAILED_STALE,
                reason="confirmation expired before an executor claimed it",
            )
            return None, stale

        assert_trade_request_transition(current.status, TradeRequestStatus.EXECUTING)
        claimed = current.model_copy(
            update={
                "status": TradeRequestStatus.EXECUTING,
                "executor_instance_id": executor_id,
                "lease_expires_at": moment + timedelta(seconds=lease_seconds),
                "execution_started_at": moment,
            }
        )
        self._upsert(self._to_row(claimed), connection=connection)
        self._audit(
            connection,
            actor=executor_id,
            action="trade_request.claim",
            request_id=current.request_id,
            from_status=current.status,
            to_status=TradeRequestStatus.EXECUTING,
            detail={"lease_seconds": lease_seconds},
        )
        return claimed, None

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

        Refuses an already-expired lease: another worker may have taken over, and reviving
        the old claim would put two workers on one request.
        """
        moment = to_utc(now or utc_now())
        with self._db.transaction() as connection:
            row = self._locked(request_id, connection)
            if row is None:
                raise LeaseLost(f"no such trade request {request_id}")
            current = TradeRequest.model_validate(self._to_model_dict(row))
            if not current.lease_held_by(executor_id, now=moment):
                raise LeaseLost(
                    f"{executor_id} does not hold a live lease on {request_id} "
                    f"(holder={current.executor_instance_id}, "
                    f"expires={current.lease_expires_at})"
                )
            renewed = current.model_copy(
                update={"lease_expires_at": moment + timedelta(seconds=lease_seconds)}
            )
            self._upsert(self._to_row(renewed), connection=connection)
            return renewed

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

        ``reconciliation=True`` bypasses the lease check, and ONLY reconciliation may use
        it: repairing a request abandoned by a dead executor is precisely the case where no
        live lease exists, and requiring one would leave such requests stuck in EXECUTING
        for ever.
        """
        moment = to_utc(now or utc_now())

        with self._db.transaction() as connection:
            row = self._locked(request_id, connection)
            if row is None:
                raise LeaseLost(f"no such trade request {request_id}")
            current = TradeRequest.model_validate(self._to_model_dict(row))

            if not reconciliation and not current.lease_held_by(executor_id, now=moment):
                raise LeaseLost(
                    f"{executor_id} cannot resolve {request_id}: lease held by "
                    f"{current.executor_instance_id} until {current.lease_expires_at}"
                )

            # Idempotent only when there is genuinely nothing to write. An earlier version
            # returned early on any unchanged status, which silently dropped the updates
            # that came with it -- including the comment_token the executor stamps before
            # sending, leaving reconciliation nothing to search for. PARTIALLY_FILLED is
            # additionally re-entrant, as each further fill lands.
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
                assert_trade_request_transition(current.status, new_status)

            payload: dict[str, Any] = {"status": new_status, **(updates or {})}
            if failure_code is not None:
                payload["failure_code"] = failure_code
            if failure_message is not None:
                payload["failure_message"] = failure_message
            if reconciliation:
                payload["last_reconciled_at"] = moment
            resolved = current.model_copy(update=payload)

            self._upsert(self._to_row(resolved), connection=connection)
            self._audit(
                connection,
                actor=executor_id,
                action="trade_request.resolve",
                request_id=request_id,
                from_status=current.status,
                to_status=new_status,
                reconciliation=reconciliation,
                detail={"failure_code": failure_code.value if failure_code else None},
            )
            return resolved

    # ── reading ───────────────────────────────────────────────────────────────

    def get(self, request_id: str) -> TradeRequest | None:
        row = self._row(request_id)
        return None if row is None else TradeRequest.model_validate(self._to_model_dict(row))

    def list_by_status(
        self,
        status: TradeRequestStatus | list[TradeRequestStatus],
        *,
        limit: int = 100,
    ) -> list[TradeRequest]:
        """Compatibility read used by executor, monitor and reconciliation.

        The legacy Firestore repository exposed list_by_status and accepted either one
        status or a list. Service code depends on that repository contract, so the local
        SQL backend implements the same shape rather than making services storage-aware.
        """
        statuses = [status] if isinstance(status, TradeRequestStatus) else list(status)
        if not statuses:
            return []
        statement = (
            select(self.table)
            .where(self.table.c.status.in_([one.value for one in statuses]))
            .order_by(self.table.c.requested_at)
            .limit(limit)
        )
        return self._parse_all(self._rows(statement), TradeRequest, what="trade_request")

    def with_status(self, status: TradeRequestStatus) -> list[TradeRequest]:
        """Single-status convenience retained for SQL-native callers."""
        return self.list_by_status(status)

    def expired_leases(self, *, now: datetime | None = None) -> list[TradeRequest]:
        """EXECUTING requests whose lease has run out (§5's reconciliation input).

        These are the abandoned ones: an executor took the claim and did not come back. The
        answer for each is MT5's, never a resend -- which is why C-1's RECONCILING exists.
        """
        moment = to_utc(now or utc_now())
        statement = (
            select(self.table)
            .where(self.table.c.status == TradeRequestStatus.EXECUTING.value)
            .where(self.table.c.lease_expires_at.isnot(None))
            .where(self.table.c.lease_expires_at <= moment)
            .order_by(self.table.c.lease_expires_at)
        )
        return self._parse_all(self._rows(statement), TradeRequest, what="trade_request")

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _locked(self, request_id: str, connection: Any) -> Any | None:
        """``SELECT ... FOR UPDATE``. No ``SKIP LOCKED``: a named request must be waited for."""
        statement = (
            select(self.table)
            .where(self.table.c.request_id == request_id)
            .with_for_update()
        )
        return connection.execute(statement).mappings().first()

    def _audit(
        self,
        connection: Any,
        *,
        actor: str,
        action: str,
        request_id: str,
        from_status: TradeRequestStatus | None,
        to_status: TradeRequestStatus | None,
        reason: str | None = None,
        reconciliation: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append an audit row in the SAME transaction as the change it describes (§71)."""
        self.audit.append(
            AuditRecord(
                audit_id=uuid.uuid4().hex,
                actor=actor,
                action=action,
                collection=COLLECTION,
                document_id=request_id,
                from_status=from_status.value if from_status else None,
                to_status=to_status.value if to_status else None,
                reason=reason,
                reconciliation=reconciliation,
                detail=detail or {},
            ),
            connection=connection,
        )

    # ── mapping ───────────────────────────────────────────────────────────────

    @staticmethod
    def _to_row(request: TradeRequest) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        return {
            "request_id": request.request_id,
            "schema_version": request.schema_version,
            "status": request.status.value,
            "symbol": request.symbol,
            "order_type": request.order_type.value,
            "volume": request.volume,
            "price": request.price,
            # C-2. Copied through, never computed. See the module docstring.
            "sl": request.sl,
            "tp": request.tp,
            "deviation_points": request.deviation_points,
            "filling_mode": request.filling_mode.value if request.filling_mode else None,
            "requested_by": request.requested_by,
            "requested_at": request.requested_at,
            "confirmed_at": request.confirmed_at,
            "confirmed_by": request.confirmed_by,
            "expires_at": request.expires_at,
            "confirmation_version": request.confirmation_version,
            "detection_id": request.detection_id,
            "link_type": request.link_type.value if request.link_type else None,
            "executor_instance_id": request.executor_instance_id,
            "lease_expires_at": request.lease_expires_at,
            "execution_started_at": request.execution_started_at,
            "execution_attempt_id": request.execution_attempt_id,
            "comment_token": request.comment_token,
            "magic": request.magic,
            "order_ticket": request.order_ticket,
            "position_id": request.position_id,
            "deal_ids": {"items": list(request.deal_ids)},
            "fill_price": request.fill_price,
            "filled_volume": request.filled_volume,
            "failure_code": request.failure_code.value if request.failure_code else None,
            "failure_message": request.failure_message,
            "quote": payload.get("quote"),
            "last_reconciled_at": request.last_reconciled_at,
            "last_synced_at": request.last_synced_at,
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["deal_ids"] = data["deal_ids"]["items"]
        return data
