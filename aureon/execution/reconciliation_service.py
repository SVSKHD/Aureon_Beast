"""Repairing requests whose outcome was never learned (§35, §76).

The executor's promise is that an unknown result leaves the request ``EXECUTING`` and is
never retried. This service is the other half of that promise: it goes and *looks* at
the broker, decides what actually happened, and writes it down.

**It never sends an order.** Not once, not as a fallback, not to "complete" a request it
cannot find. That is the whole point -- the executor refuses to retry precisely because
this service exists, and if this service could send, the guarantee would be circular.

## How a match is found

By ``magic`` + ``comment_token`` + symbol + direction, inside a window that starts
shortly before the send began (§35). The comment token is a pure function of the request
id, so an executor that died mid-send can re-derive exactly the token it stamped.

Volume is compared with a tolerance rather than exactly, because a partial fill is a
legitimate match with a *smaller* volume -- insisting on equality would turn every
partial fill into an unfindable order.

## The three outcomes

* **exactly one match** -- repair to FILLED / PARTIALLY_FILLED / PENDING;
* **no match, and past the grace period** -- ``FAILED_RECONCILIATION``. Only after the
  grace: a broker can take seconds to publish a deal, and failing too early would
  declare a live order dead;
* **more than one match** -- ``FAILED_RECONCILIATION`` with the candidates listed. Two
  matching orders means something already went wrong, and guessing between them could
  attach a request to the wrong position. A human needs to look.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from aureon.execution.broker_interface import BrokerInterface
from aureon.models.base import to_utc, utc_now
from aureon.models.broker import BrokerDeal, BrokerOrder, BrokerPosition
from aureon.models.enums import FailureCode, TradeRequestStatus
from aureon.models.identity import comment_token
from aureon.models.trade import TradeRequest
from aureon.storage.trade_request_repository import LeaseLost, TradeRequestRepository

log = logging.getLogger(__name__)

# How far before execution_started_at to begin searching (§35). Covers a clock skew
# between our host and the broker's.
SEARCH_BACKOFF_SECONDS = 60.0

# How long to wait before declaring an unfindable order dead.
DEFAULT_GRACE_SECONDS = 120.0

# Volume match tolerance, in lots. Absorbs float noise, not a genuinely different size.
VOLUME_TOLERANCE = 1e-6


@dataclass
class Candidate:
    """Something at the broker that might be this request's order."""

    kind: str  # "position" | "order" | "deal"
    identifier: int
    volume: float
    price: float | None = None
    position_id: int | None = None
    order_ticket: int | None = None
    deal_ids: tuple[int, ...] = ()

    def describe(self) -> str:
        return f"{self.kind}#{self.identifier} volume={self.volume}"


@dataclass
class ReconciliationOutcome:
    """What reconciliation decided about one request."""

    request_id: str
    action: str  # "repaired" | "failed" | "waiting" | "unchanged"
    status: TradeRequestStatus | None = None
    detail: str | None = None


class ReconciliationService:
    """Resolves requests left in an unknown state (§35)."""

    def __init__(
        self,
        repository: TradeRequestRepository,
        broker: BrokerInterface,
        *,
        magic: int,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        executor_id: str = "reconciliation",
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.magic = magic
        self.grace_seconds = grace_seconds
        self.executor_id = executor_id

    # ── Entry point ───────────────────────────────────────────────────────────

    def reconcile_all(self, *, now: datetime | None = None) -> list[ReconciliationOutcome]:
        """Sweep every request in a state that needs verifying (§76).

        Run at startup **before** serving, so a request abandoned by the previous
        process is resolved before a new one can be claimed.
        """
        moment = to_utc(now or utc_now())
        outcomes: list[ReconciliationOutcome] = []
        pending_states = [
            TradeRequestStatus.EXECUTING,
            TradeRequestStatus.PENDING,
            TradeRequestStatus.PARTIALLY_FILLED,
        ]
        for request in self.repository.list_by_status(pending_states):
            try:
                outcomes.append(self.reconcile(request, now=moment))
            except Exception:  # noqa: BLE001 - one bad request must not stop the sweep
                log.exception("reconciliation failed for %s", request.request_id)
                outcomes.append(
                    ReconciliationOutcome(
                        request.request_id, "unchanged", detail="reconciliation error"
                    )
                )
        return outcomes

    def reconcile(
        self, request: TradeRequest, *, now: datetime | None = None
    ) -> ReconciliationOutcome:
        moment = to_utc(now or utc_now())
        if request.status is TradeRequestStatus.EXECUTING:
            return self._reconcile_executing(request, moment)
        if request.status is TradeRequestStatus.PENDING:
            return self._reconcile_pending(request, moment)
        if request.status is TradeRequestStatus.PARTIALLY_FILLED:
            return self._reconcile_partial(request, moment)
        return ReconciliationOutcome(request.request_id, "unchanged")

    # ── EXECUTING: did the order ever exist? ──────────────────────────────────

    def _reconcile_executing(
        self, request: TradeRequest, now: datetime
    ) -> ReconciliationOutcome:
        token = request.comment_token or comment_token(request.request_id)
        candidates = self._find(request, token, now=now)

        if len(candidates) == 1:
            return self._repair(request, candidates[0], now=now)

        if len(candidates) > 1:
            listed = ", ".join(c.describe() for c in candidates)
            self._fail(
                request,
                FailureCode.RECONCILIATION_AMBIGUOUS,
                f"{len(candidates)} broker records match token {token}: {listed}. "
                "Refusing to guess which is this request's order -- a human must look.",
                now=now,
            )
            return ReconciliationOutcome(
                request.request_id,
                "failed",
                TradeRequestStatus.FAILED_RECONCILIATION,
                detail=listed,
            )

        # Nothing found. Wait out the grace period before declaring it dead: a broker can
        # take seconds to publish a deal, and failing early would kill a live order.
        started = request.execution_started_at
        age = (now - started).total_seconds() if started else self.grace_seconds + 1
        if age < self.grace_seconds:
            return ReconciliationOutcome(
                request.request_id,
                "waiting",
                detail=f"no match yet, {age:.0f}s of {self.grace_seconds:.0f}s grace elapsed",
            )

        self._fail(
            request,
            FailureCode.RECONCILIATION_NOT_FOUND,
            f"no broker order, position or deal carries token {token} within "
            f"{self.grace_seconds:.0f}s of {started}. The order was never placed.",
            now=now,
        )
        return ReconciliationOutcome(
            request.request_id, "failed", TradeRequestStatus.FAILED_RECONCILIATION
        )

    # ── Searching ─────────────────────────────────────────────────────────────

    def _window(self, request: TradeRequest, now: datetime) -> tuple[datetime, datetime]:
        started = request.execution_started_at or (now - timedelta(seconds=self.grace_seconds))
        return started - timedelta(seconds=SEARCH_BACKOFF_SECONDS), now

    def _find(self, request: TradeRequest, token: str, *, now: datetime) -> list[Candidate]:
        """Every broker record that could be this request's order (§35).

        Looks in three places because an order can be in any of them by now: still
        resting, already a position, or only visible as a deal.
        """
        start, end = self._window(request, now)
        found: list[Candidate] = []

        for position in self._safe(self.broker.open_positions):
            if self._matches(position, request, token):
                found.append(
                    Candidate(
                        "position",
                        position.position_id,
                        position.volume,
                        position.open_price,
                        position_id=position.position_id,
                    )
                )

        for order in self._safe(self.broker.pending_orders):
            if self._matches(order, request, token):
                found.append(
                    Candidate(
                        "order",
                        order.order_ticket,
                        order.volume,
                        order.price,
                        order_ticket=order.order_ticket,
                    )
                )

        deals = [
            deal
            for deal in self._safe(lambda: self.broker.deals_history(start, end))
            if self._matches(deal, request, token) and deal.is_entry
        ]
        # Deals for one position are one execution, not several -- a partial fill can
        # arrive as multiple deals and must not look like multiple orders.
        by_position: dict[int | None, list[BrokerDeal]] = {}
        for deal in deals:
            by_position.setdefault(deal.position_id, []).append(deal)
        for position_id, group in by_position.items():
            if any(c.position_id == position_id for c in found if position_id is not None):
                continue  # already represented by the live position
            found.append(
                Candidate(
                    "deal",
                    group[0].deal_id,
                    sum(d.volume for d in group),
                    group[0].price,
                    position_id=position_id,
                    order_ticket=group[0].order_ticket,
                    deal_ids=tuple(d.deal_id for d in group),
                )
            )
        return found

    def _matches(
        self,
        record: BrokerPosition | BrokerOrder | BrokerDeal,
        request: TradeRequest,
        token: str,
    ) -> bool:
        """Whether a broker record is this request's (§35).

        Magic and comment together: magic says "Aureon placed this", the token says
        "*this* request placed it". Volume is compared with tolerance and only as an
        upper bound, so a partial fill still matches.
        """
        if record.symbol != request.symbol:
            return False
        if record.magic != self.magic:
            return False
        if (record.comment or "") != token:
            return False

        direction = getattr(record, "direction", None)
        if isinstance(record, BrokerOrder):
            direction = record.order_type.direction
        if direction is not None and direction is not request.order_type.direction:
            return False
        if record.volume > request.volume + VOLUME_TOLERANCE:
            return False
        return True

    @staticmethod
    def _safe(fn):  # noqa: ANN001, ANN205
        """Call a broker reader, treating a failure as "nothing found".

        A broker we cannot read must not produce a confident "the order does not exist";
        the grace period plus the next sweep covers it.
        """
        try:
            return list(fn())
        except Exception:  # noqa: BLE001
            log.exception("broker read failed during reconciliation")
            return []

    # ── Repair ────────────────────────────────────────────────────────────────

    def _repair(
        self, request: TradeRequest, candidate: Candidate, *, now: datetime
    ) -> ReconciliationOutcome:
        if candidate.kind == "order":
            status = TradeRequestStatus.PENDING
        elif candidate.volume + VOLUME_TOLERANCE < request.volume:
            status = TradeRequestStatus.PARTIALLY_FILLED
        else:
            status = TradeRequestStatus.FILLED

        updates = {
            "order_ticket": candidate.order_ticket,
            "position_id": candidate.position_id,
            "deal_ids": candidate.deal_ids,
            "fill_price": candidate.price,
            "filled_volume": candidate.volume if candidate.kind != "order" else None,
        }
        try:
            self.repository.resolve(
                request.request_id,
                self.executor_id,
                status,
                updates=updates,
                reconciliation=True,
                now=now,
            )
        except LeaseLost as exc:
            log.warning("could not repair %s: %s", request.request_id, exc)
            return ReconciliationOutcome(request.request_id, "unchanged", detail=str(exc))

        log.info(
            "repaired %s to %s from %s", request.request_id, status.value, candidate.describe()
        )
        return ReconciliationOutcome(
            request.request_id, "repaired", status, detail=candidate.describe()
        )

    def _fail(
        self,
        request: TradeRequest,
        code: FailureCode,
        message: str,
        *,
        now: datetime,
    ) -> None:
        try:
            self.repository.resolve(
                request.request_id,
                self.executor_id,
                TradeRequestStatus.FAILED_RECONCILIATION,
                failure_code=code,
                failure_message=message,
                reconciliation=True,
                now=now,
            )
        except LeaseLost as exc:
            log.warning("could not fail %s: %s", request.request_id, exc)
        log.error("%s -> FAILED_RECONCILIATION: %s", request.request_id, message)

    # ── PENDING and PARTIALLY_FILLED ──────────────────────────────────────────

    def _reconcile_pending(
        self, request: TradeRequest, now: datetime
    ) -> ReconciliationOutcome:
        """A resting order: did it fill, expire or get cancelled while we were away?"""
        token = request.comment_token or comment_token(request.request_id)
        resting = [
            order
            for order in self._safe(self.broker.pending_orders)
            if self._matches(order, request, token)
        ]
        if resting:
            return ReconciliationOutcome(request.request_id, "unchanged", detail="still resting")

        start, end = self._window(request, now)
        entries = [
            deal
            for deal in self._safe(lambda: self.broker.deals_history(start, end))
            if self._matches(deal, request, token) and deal.is_entry
        ]
        if entries:
            filled = sum(d.volume for d in entries)
            status = (
                TradeRequestStatus.FILLED
                if filled + VOLUME_TOLERANCE >= request.volume
                else TradeRequestStatus.PARTIALLY_FILLED
            )
            self.repository.resolve(
                request.request_id,
                self.executor_id,
                status,
                updates={
                    "position_id": entries[0].position_id,
                    "deal_ids": tuple(d.deal_id for d in entries),
                    "fill_price": entries[0].price,
                    "filled_volume": filled,
                },
                reconciliation=True,
                now=now,
            )
            return ReconciliationOutcome(request.request_id, "repaired", status)

        # Gone with no fill: cancelled or expired. CANCELLED is the honest default --
        # claiming EXPIRED would assert an expiry time we never saw.
        self.repository.resolve(
            request.request_id,
            self.executor_id,
            TradeRequestStatus.CANCELLED,
            failure_message="pending order is no longer at the broker and never filled",
            reconciliation=True,
            now=now,
        )
        return ReconciliationOutcome(
            request.request_id, "repaired", TradeRequestStatus.CANCELLED
        )

    def _reconcile_partial(
        self, request: TradeRequest, now: datetime
    ) -> ReconciliationOutcome:
        """A partial fill: has the rest arrived since?"""
        token = request.comment_token or comment_token(request.request_id)
        start, end = self._window(request, now)
        entries = [
            deal
            for deal in self._safe(lambda: self.broker.deals_history(start, end))
            if self._matches(deal, request, token) and deal.is_entry
        ]
        if not entries:
            return ReconciliationOutcome(request.request_id, "unchanged", detail="no new deals")

        filled = sum(d.volume for d in entries)
        if filled + VOLUME_TOLERANCE < request.volume:
            return ReconciliationOutcome(
                request.request_id, "unchanged", detail=f"still partial at {filled}"
            )
        self.repository.resolve(
            request.request_id,
            self.executor_id,
            TradeRequestStatus.FILLED,
            updates={
                "deal_ids": tuple(d.deal_id for d in entries),
                "filled_volume": filled,
            },
            reconciliation=True,
            now=now,
        )
        return ReconciliationOutcome(
            request.request_id, "repaired", TradeRequestStatus.FILLED
        )
