"""Audit records (§60).

Every status transition writes one of these *in the same Firestore transaction*
as the transition itself. Writing it afterwards would leave a window in which a
crash loses the record of a state change that did happen -- and for the execution
path that record is the only account of what was attempted with real money.
"""

from __future__ import annotations

from pydantic import Field

from aureon.models.base import AureonDocument, UtcDatetime, utc_now


class AuditRecord(AureonDocument):
    """One audited action."""

    audit_id: str
    at: UtcDatetime = Field(default_factory=utc_now)

    actor: str = Field(description="Discord user id, executor instance id, or 'system'.")
    action: str = Field(description='e.g. "trade_request.confirm", "trading.disable".')

    collection: str | None = None
    document_id: str | None = None

    from_status: str | None = None
    to_status: str | None = None

    reason: str | None = None
    detail: dict[str, object] = Field(default_factory=dict)
    # True when written by reconciliation repairing state rather than by a human
    # or a normal transition -- the two must be distinguishable after the fact.
    reconciliation: bool = False
