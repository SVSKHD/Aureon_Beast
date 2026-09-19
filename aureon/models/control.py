"""Control requests: cancel an order, close a position (§46, §47).

Discord never cancels or closes directly -- it writes one of these, and the
executor consumes it under the same claim/lease discipline as a trade request.
That keeps a single process in charge of every broker call, so "cancel" cannot
race "it just filled", and the outcome a human is shown comes from the monitor's
reconciled view rather than from the request's own optimistic return.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from aureon.models.base import AureonDocument, UtcDatetime, utc_now
from aureon.models.enums import ControlRequestKind, ControlRequestStatus, FailureCode


class ControlRequest(AureonDocument):
    """A requested action on something already live (§46, §47)."""

    control_id: str
    kind: ControlRequestKind
    status: ControlRequestStatus = ControlRequestStatus.REQUESTED

    target: str = Field(description="Order ticket for cancel, position id for close.")
    symbol: str | None = None
    volume: float | None = Field(
        default=None, gt=0, description="Partial close volume; None closes all."
    )

    requested_by: str
    requested_at: UtcDatetime = Field(default_factory=utc_now)

    executor_instance_id: str | None = None
    lease_expires_at: UtcDatetime | None = None

    completed_at: UtcDatetime | None = None
    failure_code: FailureCode | None = None
    failure_message: str | None = None

    @model_validator(mode="after")
    def _volume_only_for_close(self) -> ControlRequest:
        if self.kind is ControlRequestKind.CANCEL and self.volume is not None:
            raise ValueError("volume is meaningless for a cancel; a pending order is whole")
        return self
