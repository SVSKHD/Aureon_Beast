"""Control requests, on PostgreSQL (§46, §47, plan §17).

Close, close-all, flatten, and the kill switch. These move money too, so they get the same
claim mechanism as a trade request and for the same reason: an operation that could run
twice because it used a weaker guard than the one beside it is the kind of asymmetry nobody
notices until it closes a position twice.

That symmetry was not always there. The Firestore version's ``resolve`` had no lease check
while the trade-request equivalent always did, and its own docstring calls that "an
asymmetry, not a decision" -- because a cancel can LOSE a race to a fill, so a stale
writer's "completed" landing on top of the real "already filled" is how a human comes to
believe an order is gone when it is a live position. The check is here from the start.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from aureon.models.audit import AuditRecord
from aureon.models.base import to_utc, utc_now
from aureon.models.control import ControlRequest
from aureon.models.enums import (
    ControlRequestStatus,
    FailureCode,
    assert_control_request_transition,
)
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.audit import AuditRepository
from aureon.storage.postgres.repositories.base import PostgresRepository

#: The label written into ``audit_logs.collection``, derived from the table rather
#: than spelled as a literal. Two reasons: the label can never drift from the table it
#: names, and §83's guard against bare collection literals keeps covering this module
#: (decision 356).
COLLECTION = tables.ControlRequest.__tablename__


class ControlClaimRejected(RuntimeError):
    """This executor may not claim this control request."""


class ControlLeaseLost(RuntimeError):
    """The caller does not hold a live lease on this control request."""


class ControlRequestRepository(PostgresRepository):
    """Creates, claims and resolves ``control_requests``."""

    table = tables.ControlRequest.__table__

    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self.audit = AuditRepository(database)

    # ── create ────────────────────────────────────────────────────────────────

    def create(self, request: ControlRequest) -> ControlRequest:
        """Write a new control request in REQUESTED. Idempotent on its id."""
        if request.status is not ControlRequestStatus.REQUESTED:
            raise ValueError(f"a new control request must start in REQUESTED, got {request.status}")
        with self._db.transaction() as connection:
            existing = self._locked(request.control_id, connection)
            if existing is not None:
                return ControlRequest.model_validate(self._to_model_dict(existing))
            self._upsert(self._to_row(request), connection=connection)
            self._audit(
                connection,
                actor=request.requested_by,
                action="control_request.create",
                control_id=request.control_id,
                from_status=None,
                to_status=ControlRequestStatus.REQUESTED,
                detail={"kind": request.kind.value, "target": request.target},
            )
        return request

    # ── claim (§17) ───────────────────────────────────────────────────────────

    def claim(
        self,
        control_id: str,
        executor_id: str,
        *,
        lease_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> ControlRequest:
        """REQUESTED → EXECUTING, for exactly one executor (§46, §47)."""
        moment = to_utc(now or utc_now())
        with self._db.transaction() as connection:
            row = self._locked(control_id, connection)
            if row is None:
                raise ControlClaimRejected(f"no such control request {control_id}")
            current = ControlRequest.model_validate(self._to_model_dict(row))
            if current.status is not ControlRequestStatus.REQUESTED:
                raise ControlClaimRejected(
                    f"{control_id} is {current.status.value}, not REQUESTED"
                )
            return self._apply_claim(connection, current, executor_id, lease_seconds, moment)

    def claim_next(
        self,
        executor_id: str,
        *,
        lease_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> ControlRequest | None:
        """The same scan as §15's, over control requests. ``None`` when the queue is empty."""
        moment = to_utc(now or utc_now())
        with self._db.transaction() as connection:
            statement = (
                select(self.table)
                .where(self.table.c.status == ControlRequestStatus.REQUESTED.value)
                .where(
                    or_(
                        self.table.c.lease_expires_at.is_(None),
                        self.table.c.lease_expires_at <= moment,
                    )
                )
                .order_by(self.table.c.requested_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            row = connection.execute(statement).mappings().first()
            if row is None:
                return None
            current = ControlRequest.model_validate(self._to_model_dict(row))
            return self._apply_claim(connection, current, executor_id, lease_seconds, moment)

    def _apply_claim(
        self,
        connection: Any,
        current: ControlRequest,
        executor_id: str,
        lease_seconds: float,
        moment: datetime,
    ) -> ControlRequest:
        assert_control_request_transition(current.status, ControlRequestStatus.EXECUTING)
        claimed = current.model_copy(
            update={
                "status": ControlRequestStatus.EXECUTING,
                "executor_instance_id": executor_id,
                "lease_expires_at": moment + timedelta(seconds=lease_seconds),
            }
        )
        self._upsert(self._to_row(claimed), connection=connection)
        self._audit(
            connection,
            actor=executor_id,
            action="control_request.claim",
            control_id=current.control_id,
            from_status=current.status,
            to_status=ControlRequestStatus.EXECUTING,
        )
        return claimed

    # ── resolve ───────────────────────────────────────────────────────────────

    def resolve(
        self,
        control_id: str,
        executor_id: str,
        status: ControlRequestStatus,
        *,
        failure_code: FailureCode | None = None,
        failure_message: str | None = None,
        reconciliation: bool = False,
        now: datetime | None = None,
    ) -> ControlRequest:
        """Record the outcome, if this executor still holds the lease.

        ``reconciliation=True`` is the same sanctioned exception the trade requests use:
        reconciliation holds no lease, because it is repairing what a dead executor left.
        """
        moment = to_utc(now or utc_now())
        with self._db.transaction() as connection:
            row = self._locked(control_id, connection)
            if row is None:
                raise ControlLeaseLost(f"no such control request {control_id}")
            current = ControlRequest.model_validate(self._to_model_dict(row))

            if not reconciliation and not self._holds_lease(current, executor_id, moment):
                raise ControlLeaseLost(
                    f"{executor_id} cannot resolve {control_id}: lease held by "
                    f"{current.executor_instance_id} until {current.lease_expires_at}"
                )

            if current.status is not status:
                assert_control_request_transition(current.status, status)

            resolved = current.model_copy(
                update={
                    "status": status,
                    "completed_at": moment,
                    "failure_code": failure_code,
                    "failure_message": failure_message,
                }
            )
            self._upsert(self._to_row(resolved), connection=connection)
            self._audit(
                connection,
                actor=executor_id,
                action="control_request.resolve",
                control_id=control_id,
                from_status=current.status,
                to_status=status,
                reconciliation=reconciliation,
            )
            return resolved

    @staticmethod
    def _holds_lease(current: ControlRequest, executor_id: str, moment: datetime) -> bool:
        """Whether this executor's lease is still live.

        Spelled out here rather than on the model because ``ControlRequest`` has no
        ``lease_held_by`` -- and adding one would be a model change in a storage step.
        """
        if current.executor_instance_id != executor_id:
            return False
        if current.lease_expires_at is None:
            return False
        return to_utc(current.lease_expires_at) > moment

    # ── reading ───────────────────────────────────────────────────────────────

    def get(self, control_id: str) -> ControlRequest | None:
        row = self._row(control_id)
        return None if row is None else ControlRequest.model_validate(self._to_model_dict(row))

    def with_status(self, status: ControlRequestStatus) -> list[ControlRequest]:
        statement = (
            select(self.table)
            .where(self.table.c.status == status.value)
            .order_by(self.table.c.requested_at)
        )
        return self._parse_all(self._rows(statement), ControlRequest, what="control_request")

    def expired_leases(self, *, now: datetime | None = None) -> list[ControlRequest]:
        moment = to_utc(now or utc_now())
        statement = (
            select(self.table)
            .where(self.table.c.status == ControlRequestStatus.EXECUTING.value)
            .where(self.table.c.lease_expires_at.isnot(None))
            .where(self.table.c.lease_expires_at <= moment)
            .order_by(self.table.c.lease_expires_at)
        )
        return self._parse_all(self._rows(statement), ControlRequest, what="control_request")

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _locked(self, control_id: str, connection: Any) -> Any | None:
        statement = (
            select(self.table)
            .where(self.table.c.control_id == control_id)
            .with_for_update()
        )
        return connection.execute(statement).mappings().first()

    def _audit(
        self,
        connection: Any,
        *,
        actor: str,
        action: str,
        control_id: str,
        from_status: ControlRequestStatus | None,
        to_status: ControlRequestStatus | None,
        reconciliation: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.audit.append(
            AuditRecord(
                audit_id=uuid.uuid4().hex,
                actor=actor,
                action=action,
                collection=COLLECTION,
                document_id=control_id,
                from_status=from_status.value if from_status else None,
                to_status=to_status.value if to_status else None,
                reconciliation=reconciliation,
                detail=detail or {},
            ),
            connection=connection,
        )

    # ── mapping ───────────────────────────────────────────────────────────────

    @staticmethod
    def _to_row(request: ControlRequest) -> dict[str, Any]:
        return {
            "control_id": request.control_id,
            "schema_version": request.schema_version,
            "kind": request.kind.value,
            "status": request.status.value,
            "target": request.target,
            "symbol": request.symbol,
            "volume": request.volume,
            "requested_by": request.requested_by,
            "requested_at": request.requested_at,
            "completed_at": request.completed_at,
            "executor_instance_id": request.executor_instance_id,
            "lease_expires_at": request.lease_expires_at,
            "failure_code": request.failure_code.value if request.failure_code else None,
            "failure_message": request.failure_message,
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        return dict(row)
