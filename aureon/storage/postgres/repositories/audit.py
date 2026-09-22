"""The audit log, on PostgreSQL (§71).

Append-only, and written INSIDE the transaction that made the change it describes. That
placement is the whole point: an audit row committed separately can be lost when the state
change rolls back, or survive when it does, and either way the log stops being evidence.

Arrives with ``trade_requests`` rather than with "the rest", because every money operation
writes one and the claim cannot be built without it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from aureon.models.audit import AuditRecord
from aureon.models.base import to_utc
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


class AuditRepository(PostgresRepository):
    """Appends and reads ``audit_logs``."""

    table = tables.AuditRecord.__table__

    def append(self, record: AuditRecord, *, connection: Any = None) -> str:
        """Write one audit row.

        ``_insert_only``, not an upsert: an audit id that already exists is not a retry to
        absorb but a bug or a replay, and overwriting the first row would destroy the only
        record of what actually happened first.
        """
        self._insert_only(self._to_row(record), connection=connection)
        return record.audit_id

    def get(self, audit_id: str) -> AuditRecord | None:
        row = self._row(audit_id)
        return None if row is None else AuditRecord.model_validate(self._to_model_dict(row))

    def for_document(self, collection: str, document_id: str) -> list[AuditRecord]:
        """Everything recorded about one document, oldest first.

        The question asked of any surprising state: what happened to this request, in what
        order, and who asked for it.
        """
        statement = (
            select(self.table)
            .where(self.table.c.collection == collection)
            .where(self.table.c.document_id == document_id)
            .order_by(self.table.c.at, self.table.c.audit_id)
        )
        return self._parse_all(self._rows(statement), AuditRecord, what="audit_log")

    def in_period(self, start: datetime, end: datetime) -> list[AuditRecord]:
        statement = (
            select(self.table)
            .where(self.table.c.at >= to_utc(start))
            .where(self.table.c.at < to_utc(end))
            .order_by(self.table.c.at, self.table.c.audit_id)
        )
        return self._parse_all(self._rows(statement), AuditRecord, what="audit_log")

    @staticmethod
    def _to_row(record: AuditRecord) -> dict[str, Any]:
        payload = record.model_dump(mode="json")
        return {
            "audit_id": record.audit_id,
            "schema_version": record.schema_version,
            "at": record.at,
            "actor": record.actor,
            "action": record.action,
            "collection": record.collection,
            "document_id": record.document_id,
            "from_status": record.from_status,
            "to_status": record.to_status,
            "reason": record.reason,
            "reconciliation": record.reconciliation,
            "detail": payload["detail"],
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        return dict(row)
