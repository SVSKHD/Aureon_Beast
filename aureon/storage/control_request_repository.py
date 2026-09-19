"""Control request state (§46, §47).

A cancel or a close, written by Discord and performed by the executor. It carries the
**same claim/lease discipline as a trade request**, for the same reason: two processes
both acting on "close position 555" could close it twice, and the second close of a
position that has already gone is at best noise and at worst an accidental reversal.

Discord writes these and stops. It never reports the outcome from its own return -- the
monitor records what actually happened (§46, §47).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from aureon.models.audit import AuditRecord
from aureon.models.base import to_utc, utc_now
from aureon.models.control import ControlRequest
from aureon.models.enums import (
    ControlRequestStatus,
    FailureCode,
    TransitionError,
    assert_control_request_transition,
)
from aureon.storage import paths
from aureon.storage.trade_request_repository import _where

log = logging.getLogger(__name__)


class ControlClaimRejected(RuntimeError):
    """Another executor holds this control request, or it is no longer REQUESTED."""


class ControlRequestRepository:
    """Transactional access to ``control_requests``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def _ref(self, control_id: str):
        return self._client.document(paths.control_request_path(control_id))

    @staticmethod
    def _run(transaction: Any, fn: Callable[..., Any], *args: Any) -> Any:
        from google.cloud import firestore

        return firestore.transactional(fn)(transaction, *args)

    def _write_audit(
        self,
        transaction: Any,
        *,
        actor: str,
        action: str,
        control_id: str,
        from_status: ControlRequestStatus | None,
        to_status: ControlRequestStatus,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        record = AuditRecord(
            audit_id=uuid.uuid4().hex,
            actor=actor,
            action=action,
            collection=paths.CONTROL_REQUESTS,
            document_id=control_id,
            from_status=from_status.value if from_status else None,
            to_status=to_status.value,
            reason=reason,
            detail=detail or {},
        )
        transaction.set(
            self._client.document(paths.audit_path(record.audit_id)),
            record.model_dump(mode="json"),
        )

    # ── Reading ───────────────────────────────────────────────────────────────

    def get(self, control_id: str) -> ControlRequest | None:
        snapshot = self._ref(control_id).get()
        if not getattr(snapshot, "exists", False):
            return None
        return ControlRequest.model_validate(snapshot.to_dict())

    def list_by_status(
        self, status: ControlRequestStatus, *, limit: int = 100
    ) -> list[ControlRequest]:
        query = _where(
            self._client.collection(paths.CONTROL_REQUESTS).limit(limit),
            "status",
            "==",
            status.value,
        )
        return [ControlRequest.model_validate(doc.to_dict()) for doc in query.stream()]

    # ── Writing ───────────────────────────────────────────────────────────────

    def create(self, request: ControlRequest) -> ControlRequest:
        """Write a new control request in ``REQUESTED``. Idempotent on its id."""
        if request.status is not ControlRequestStatus.REQUESTED:
            raise ValueError(f"a new control request must be REQUESTED, got {request.status}")

        def txn(transaction: Any) -> ControlRequest:
            existing = self._ref(request.control_id).get(transaction=transaction)
            if getattr(existing, "exists", False):
                return ControlRequest.model_validate(existing.to_dict())
            transaction.set(
                self._ref(request.control_id), request.model_dump(mode="json")
            )
            self._write_audit(
                transaction,
                actor=request.requested_by,
                action=f"control_request.{request.kind.value}",
                control_id=request.control_id,
                from_status=None,
                to_status=ControlRequestStatus.REQUESTED,
                detail={"target": request.target, "volume": request.volume},
            )
            return request

        return self._run(self._client.transaction(), txn)

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

        def txn(transaction: Any) -> ControlRequest:
            snapshot = self._ref(control_id).get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                raise ControlClaimRejected(f"no such control request {control_id}")
            current = ControlRequest.model_validate(snapshot.to_dict())
            if current.status is not ControlRequestStatus.REQUESTED:
                raise ControlClaimRejected(
                    f"{control_id} is {current.status.value}, not REQUESTED"
                )
            assert_control_request_transition(
                current.status, ControlRequestStatus.EXECUTING
            )
            claimed = current.model_copy(
                update={
                    "status": ControlRequestStatus.EXECUTING,
                    "executor_instance_id": executor_id,
                    "lease_expires_at": moment + timedelta(seconds=lease_seconds),
                }
            )
            transaction.set(self._ref(control_id), claimed.model_dump(mode="json"))
            self._write_audit(
                transaction,
                actor=executor_id,
                action="control_request.claim",
                control_id=control_id,
                from_status=current.status,
                to_status=ControlRequestStatus.EXECUTING,
            )
            return claimed

        return self._run(self._client.transaction(), txn)

    def resolve(
        self,
        control_id: str,
        executor_id: str,
        status: ControlRequestStatus,
        *,
        failure_code: FailureCode | None = None,
        failure_message: str | None = None,
        now: datetime | None = None,
    ) -> ControlRequest:
        """Record the outcome of a control request."""
        moment = to_utc(now or utc_now())

        def txn(transaction: Any) -> ControlRequest:
            snapshot = self._ref(control_id).get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                raise ControlClaimRejected(f"no such control request {control_id}")
            current = ControlRequest.model_validate(snapshot.to_dict())
            if current.status is status:
                return current
            try:
                assert_control_request_transition(current.status, status)
            except TransitionError as exc:
                raise ControlClaimRejected(str(exc)) from exc

            resolved = current.model_copy(
                update={
                    "status": status,
                    "completed_at": moment,
                    "failure_code": failure_code,
                    "failure_message": failure_message,
                }
            )
            transaction.set(self._ref(control_id), resolved.model_dump(mode="json"))
            self._write_audit(
                transaction,
                actor=executor_id,
                action="control_request.resolve",
                control_id=control_id,
                from_status=current.status,
                to_status=status,
                reason=failure_message,
            )
            return resolved

        return self._run(self._client.transaction(), txn)
