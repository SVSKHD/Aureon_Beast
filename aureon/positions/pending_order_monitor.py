"""Resolving pending orders that are no longer resting (§43).

A ``PENDING`` trade request whose order ticket has vanished from the broker went one of
three ways, and the deals are what distinguish them:

* **filled** -- entry deals exist for it → ``FILLED`` (or ``PARTIALLY_FILLED``), and a
  ``Trade`` is created;
* **expired** -- the order carried an expiry that has passed → ``EXPIRED``;
* **cancelled** -- gone, no deals, no expiry → ``CANCELLED``.

The ordering matters. A fill is checked **first**, always. An order can fill in the same
instant a cancel is issued (Phase 4 scenario G), and recording CANCELLED for a position
that actually exists would leave a live trade nobody is watching -- the worst of the three
mistakes by a wide margin.

``CANCELLED`` is the default for "gone without a trace" rather than ``EXPIRED``, because
claiming expiry asserts an expiry time we never observed (decision 61).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from aureon.execution.broker_interface import BrokerInterface
from aureon.models.base import to_utc, utc_now
from aureon.models.broker import BrokerDeal
from aureon.models.enums import DealEntry, TradeRequestStatus
from aureon.models.identity import comment_token
from aureon.models.trade import TradeRequest
from aureon.storage.trade_request_repository import LeaseLost, TradeRequestRepository

log = logging.getLogger(__name__)

VOLUME_TOLERANCE = 1e-9


@dataclass
class PendingOutcome:
    """What happened to one pending order."""

    request_id: str
    status: TradeRequestStatus
    position_id: int | None = None
    filled_volume: float = 0.0
    deal_ids: tuple[int, ...] = ()
    detail: str | None = None

    @property
    def created_a_position(self) -> bool:
        return self.position_id is not None


class PendingOrderMonitor:
    """Decides the fate of PENDING requests whose orders are gone (§43)."""

    def __init__(
        self,
        repository: TradeRequestRepository,
        broker: BrokerInterface,
        *,
        magic: int,
        actor: str = "monitor",
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.magic = magic
        self.actor = actor

    def check(
        self,
        request: TradeRequest,
        *,
        deals: list[BrokerDeal],
        resting_tickets: set[int],
        now: datetime | None = None,
    ) -> PendingOutcome | None:
        """Resolve one PENDING request, or return ``None`` if it is still resting."""
        moment = to_utc(now or utc_now())
        if request.status is not TradeRequestStatus.PENDING:
            return None

        ticket = request.order_ticket
        if ticket is not None and ticket in resting_tickets:
            return None  # still at the broker; nothing to decide

        token = request.comment_token or comment_token(request.request_id)
        entries = [
            deal
            for deal in deals
            # Matching an ENTRY by token is correct and necessary here: a fill's entry
            # deal does carry the order's comment, and the position id is exactly what we
            # are trying to discover. Exits are matched by position id instead (§36).
            if deal.entry in (DealEntry.IN, DealEntry.INOUT)
            and deal.symbol == request.symbol
            and deal.magic == self.magic
            and (deal.comment or "") == token
        ]

        if entries:
            filled = round(sum(d.volume for d in entries), 8)
            status = (
                TradeRequestStatus.FILLED
                if filled + VOLUME_TOLERANCE >= request.volume
                else TradeRequestStatus.PARTIALLY_FILLED
            )
            outcome = PendingOutcome(
                request_id=request.request_id,
                status=status,
                position_id=entries[0].position_id,
                filled_volume=filled,
                deal_ids=tuple(d.deal_id for d in entries),
                detail=f"filled {filled} of {request.volume}",
            )
        elif request.expires_at is not None and moment >= request.expires_at:
            outcome = PendingOutcome(
                request_id=request.request_id,
                status=TradeRequestStatus.EXPIRED,
                detail=f"order expiry {request.expires_at.isoformat()} passed",
            )
        else:
            outcome = PendingOutcome(
                request_id=request.request_id,
                status=TradeRequestStatus.CANCELLED,
                detail="order is no longer at the broker and never filled",
            )

        self._persist(request, outcome, now=moment)
        return outcome

    def _persist(
        self, request: TradeRequest, outcome: PendingOutcome, *, now: datetime
    ) -> None:
        updates: dict[str, object] = {}
        if outcome.created_a_position:
            updates.update(
                position_id=outcome.position_id,
                deal_ids=outcome.deal_ids,
                filled_volume=outcome.filled_volume,
            )
        try:
            self.repository.resolve(
                request.request_id,
                self.actor,
                outcome.status,
                updates=updates or None,
                failure_message=(
                    outcome.detail
                    if outcome.status
                    in {TradeRequestStatus.CANCELLED, TradeRequestStatus.EXPIRED}
                    else None
                ),
                # The monitor holds no lease -- it observes rather than executes -- so it
                # resolves under the reconciliation flag, the same sanctioned exception
                # reconciliation uses.
                reconciliation=True,
                now=now,
            )
        except LeaseLost as exc:
            log.warning("could not resolve pending %s: %s", request.request_id, exc)
        else:
            log.info(
                "pending %s -> %s (%s)",
                request.request_id,
                outcome.status.value,
                outcome.detail,
            )
