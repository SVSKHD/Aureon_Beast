"""Trade requests, positions and the broker call contracts (§25-§53).

The request model is where exactly-once execution is made possible. Three groups
of fields carry that weight:

* **confirmation** -- ``confirmed_at`` / ``expires_at`` / ``confirmation_version``
  prove a human authorised *this* request recently enough (§28);
* **lease** -- ``executor_instance_id`` / ``lease_expires_at`` let exactly one
  worker act, and let every other worker recognise that it must not (§29);
* **attempt** -- ``comment_token`` / ``execution_attempt_id`` / tickets and deal
  ids are the breadcrumbs reconciliation follows to find an order whose result
  was never received (§32-§34).

The states themselves live in ``enums.py``; every move between them goes through
``assert_trade_request_transition``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from aureon.models.agent_decision import GuardianDecision, TradeManagementDecision
from aureon.models.base import AureonDocument, AureonModel, MarketTime, UtcDatetime, to_utc, utc_now
from aureon.models.enums import (
    Direction,
    ExcursionSource,
    FailureCode,
    FillingMode,
    LinkType,
    OrderType,
    TradeRequestStatus,
    TradeSource,
    TradeStatus,
)
from aureon.models.market import QuoteSnapshot

# Decision 15: convention, not yet an enum. Phase 5 will show what MT5 actually
# reports across sl / tp / expert / client / mobile / web; only then is freezing
# an enum safe. Until then an unrecognised reason is stored verbatim rather than
# rejected -- discarding it would destroy the very evidence needed to build the
# enum.
CLOSE_REASON_CONVENTION: frozenset[str] = frozenset(
    {"sl", "tp", "manual", "mobile", "broker", "discord", "unknown"}
)


class BrokerOrderRequest(AureonModel):
    """What Aureon asks the broker to do (§30, §37-§42).

    Built by the executor *after* the guard has passed, never by Discord.
    ``magic`` and ``comment`` are always set, because they are how a position is
    later recognised as Aureon's rather than a human's (§52) and how an order is
    found again during reconciliation (§34).
    """

    symbol: str
    order_type: OrderType
    volume: float = Field(gt=0)
    price: float | None = Field(
        default=None, description="Required for pending orders; None for market."
    )
    sl: float | None = None
    tp: float | None = None
    deviation_points: int = Field(default=0, ge=0)
    filling_mode: FillingMode | None = None
    magic: int
    comment: str = Field(max_length=31, description="comment_token (§34).")

    @model_validator(mode="after")
    def _pending_orders_need_a_price(self) -> BrokerOrderRequest:
        if self.order_type.is_pending and self.price is None:
            raise ValueError(f"{self.order_type} requires a price")
        return self

    @property
    def direction(self) -> Direction:
        return self.order_type.direction


class BrokerOrderResult(AureonModel):
    """What the broker said back (§31-§33).

    ``raw`` keeps the untouched broker payload. When a retcode we have not mapped
    appears at 3am, the raw payload is the difference between diagnosing it and
    guessing.
    """

    ok: bool
    retcode: int | None = None
    retcode_name: str | None = None
    order_ticket: int | None = None
    deal_ids: tuple[int, ...] = ()
    position_id: int | None = None
    fill_price: float | None = None
    filled_volume: float | None = None
    failure_code: FailureCode | None = None
    message: str | None = None
    raw: dict[str, object] = Field(default_factory=dict)


class TradeRequest(AureonDocument):
    """A human-initiated request to trade (§25-§34).

    Created by Discord in ``REQUESTED``, moved to ``CONFIRMED`` by an explicit
    human action, and only then visible to an executor. Nothing else may create
    one -- in particular no agent, engine or evaluator, which the boundary tests
    enforce.
    """

    request_id: str
    status: TradeRequestStatus = TradeRequestStatus.REQUESTED

    symbol: str
    order_type: OrderType
    volume: float = Field(gt=0)
    price: float | None = Field(default=None, description="Entry for pending orders.")
    sl: float | None = None
    tp: float | None = None
    deviation_points: int = Field(default=0, ge=0)
    filling_mode: FillingMode | None = None

    requested_by: str = Field(description="Discord user id of the requester.")
    requested_at: UtcDatetime = Field(default_factory=utc_now)

    confirmed_at: UtcDatetime | None = None
    confirmed_by: str | None = None
    expires_at: UtcDatetime | None = Field(
        default=None, description="Confirmation TTL deadline (§28)."
    )
    confirmation_version: int = Field(default=0, ge=0)
    quote: QuoteSnapshot | None = Field(
        default=None, description="Quote shown to the human at confirmation (§27)."
    )

    detection_id: str | None = Field(
        default=None, description="Explicitly linked detection, if any (§50)."
    )
    link_type: LinkType | None = None

    executor_instance_id: str | None = None
    lease_expires_at: UtcDatetime | None = None
    execution_started_at: UtcDatetime | None = None
    execution_attempt_id: str | None = None
    comment_token: str | None = None
    magic: int | None = None

    order_ticket: int | None = None
    position_id: int | None = None
    deal_ids: tuple[int, ...] = ()
    fill_price: float | None = None
    filled_volume: float | None = None

    failure_code: FailureCode | None = None
    failure_message: str | None = None

    #: Observational stamps, and the ONLY fields a terminal document may still take
    #: (§58). They record when something was last looked at and assert nothing about
    #: what happened, so writing one cannot rewrite history -- which is why the
    #: repositories allow exactly these two past a terminal status.
    last_reconciled_at: UtcDatetime | None = None
    last_synced_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _reject_inferred_links(self) -> TradeRequest:
        # Decision 8: an inferred link is a statistical guess. Allowing one here
        # would let a guess harden into a stored fact about a real trade; guesses
        # live only on review documents.
        if self.link_type is LinkType.INFERRED:
            raise ValueError(
                "link_type=inferred is not permitted on a TradeRequest (decision 8); "
                "inferred links live only on review documents"
            )
        if self.detection_id and self.link_type is None:
            raise ValueError("a linked detection requires link_type=explicit")
        return self

    @model_validator(mode="after")
    def _failure_code_only_with_failure(self) -> TradeRequest:
        # Decision 2: a failure is a status plus a code. A code on a non-failed
        # request would render a misleading reason in Discord.
        failed = self.status in {
            TradeRequestStatus.FAILED,
            TradeRequestStatus.FAILED_STALE,
            TradeRequestStatus.FAILED_RECONCILIATION,
        }
        if self.failure_code is not None and not failed:
            raise ValueError(
                f"failure_code {self.failure_code} set on non-failed status {self.status}"
            )
        return self

    def confirmation_expired(self, *, now: datetime | None = None) -> bool:
        """Whether the human's confirmation is too old to act on (§28)."""
        if self.expires_at is None:
            return False
        return to_utc(now or utc_now()) >= self.expires_at

    def lease_held_by(self, executor_id: str, *, now: datetime | None = None) -> bool:
        """Whether ``executor_id`` currently holds a live lease.

        The gate on every resolve. A worker whose lease has expired must stop
        touching the request even mid-flight: another worker may already have
        taken over, and two workers resolving one request is how a double trade
        gets recorded.
        """
        if self.executor_instance_id != executor_id or self.lease_expires_at is None:
            return False
        return to_utc(now or utc_now()) < self.lease_expires_at

    def lease_expired(self, *, now: datetime | None = None) -> bool:
        if self.lease_expires_at is None:
            return False
        return to_utc(now or utc_now()) >= self.lease_expires_at


class PendingOrder(AureonModel):
    """A live pending order at the broker (§43, decision 9).

    A model, not a collection: the ``PENDING`` trade request already is the
    record, and a second collection would create two sources of truth to keep in
    step. The monitor builds this over the request when it needs order shape.
    """

    order_ticket: int
    symbol: str
    order_type: OrderType
    volume: float = Field(gt=0)
    price: float
    sl: float | None = None
    tp: float | None = None
    magic: int | None = None
    comment: str | None = None
    placed_at: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None


class Excursion(AureonModel):
    """How far a position ran for and against, while it was open (§45).

    ``source`` is not decoration. Excursions watched tick by tick and excursions
    rebuilt from M1 candles after a restart have materially different resolution,
    and a review that mixed them without saying so would overstate its own
    precision.
    """

    mfe: float | None = Field(default=None, description="Max favourable excursion.")
    mfe_at: UtcDatetime | None = None
    mfe_price: float | None = None
    mae: float | None = Field(default=None, description="Max adverse excursion.")
    mae_at: UtcDatetime | None = None
    mae_price: float | None = None
    source: ExcursionSource = ExcursionSource.LIVE_TICKS


class Trade(AureonDocument):
    """A real position. MT5 is the truth (§49-§53).

    Aureon never decides a trade closed; it observes that it did. Every field
    here is a reflection of broker state, reconciled from deals, which is why a
    close seen in the MT5 mobile app lands correctly without Aureon being involved
    in it at all.
    """

    trade_id: str
    mt5_position_id: int
    trade_request_id: str | None = Field(
        default=None, description="None for externally-opened positions (§52)."
    )
    source: TradeSource = TradeSource.AUREON

    symbol: str
    direction: Direction
    volume: float = Field(gt=0, description="Original opened volume.")
    open_price: float
    open_time: MarketTime
    sl: float | None = None
    tp: float | None = None
    magic: int | None = None

    status: TradeStatus = TradeStatus.OPEN
    closed_volume: float = Field(default=0.0, ge=0)
    close_price: float | None = None
    close_time: MarketTime | None = None
    close_reason: str = Field(
        default="unknown",
        description=f"Convention (decision 15): {sorted(CLOSE_REASON_CONVENTION)}.",
    )
    close_reason_raw: str | None = Field(
        default=None, description="Broker's own reason code, kept verbatim."
    )

    realized_pnl: float | None = None
    commission: float = 0.0
    swap: float = 0.0
    deal_ids: tuple[int, ...] = ()

    excursion: Excursion = Field(default_factory=Excursion)

    # Agents 14/16 are observational state. They describe how Aureon would protect the
    # position; broker truth still decides whether and where it actually closes.
    management: TradeManagementDecision | None = None
    guardian: GuardianDecision | None = None

    detection_id: str | None = None
    link_type: LinkType | None = None

    #: Observational stamps, and the ONLY fields a terminal document may still take
    #: (§58). They record when something was last looked at and assert nothing about
    #: what happened, so writing one cannot rewrite history -- which is why the
    #: repositories allow exactly these two past a terminal status.
    last_reconciled_at: UtcDatetime | None = None
    last_synced_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _reject_inferred_links(self) -> Trade:
        # Decision 8, same reasoning as on TradeRequest: Phase 7 may infer a link
        # on a review, but a trade never carries a guess.
        if self.link_type is LinkType.INFERRED:
            raise ValueError(
                "link_type=inferred is not permitted on a Trade (decision 8); "
                "inferred links live only on review documents"
            )
        if self.detection_id and self.link_type is None:
            raise ValueError("a linked detection requires link_type=explicit")
        return self

    @model_validator(mode="after")
    def _closed_volume_within_volume(self) -> Trade:
        if self.closed_volume > self.volume + 1e-9:
            raise ValueError(
                f"closed_volume {self.closed_volume} exceeds opened volume {self.volume}"
            )
        if self.status is TradeStatus.CLOSED and self.close_time is None:
            raise ValueError("a CLOSED trade requires close_time")
        return self

    @property
    def remaining_volume(self) -> float:
        return round(self.volume - self.closed_volume, 8)

    @property
    def is_external(self) -> bool:
        return self.source is TradeSource.EXTERNAL_MT5
