"""Broker-facing contracts (§30-§36, §43-§45).

What the broker tells us about itself: the account, live positions, resting orders and
the deals that moved money. These are read-only *observations*; the request and result
models for placing orders live in ``aureon/models/trade.py``.

``raw`` is carried on several of these on purpose. When an unmapped retcode or an
unfamiliar reason code shows up at 3am on a real account, the untouched broker payload
is the difference between diagnosing it and guessing.
"""

from __future__ import annotations

from pydantic import Field

from aureon.models.base import AureonModel, UtcDatetime
from aureon.models.enums import DealEntry, Direction, OrderType


class AccountInfo(AureonModel):
    """Account state, for the margin and exposure guards (§56)."""

    login: int
    currency: str = "USD"
    balance: float
    equity: float
    margin: float = 0.0
    margin_free: float = 0.0
    margin_level: float | None = None
    leverage: int | None = None
    server: str | None = None
    raw: dict[str, object] = Field(default_factory=dict)


class BrokerPosition(AureonModel):
    """An open position as the broker reports it (§49).

    ``magic`` is what distinguishes Aureon's positions from a human's: anything without
    our magic number was opened elsewhere and is imported, never managed (§52).
    """

    position_id: int
    symbol: str
    direction: Direction
    volume: float = Field(gt=0)
    open_price: float
    open_time: UtcDatetime
    sl: float | None = None
    tp: float | None = None
    profit: float = 0.0
    swap: float = 0.0
    magic: int | None = None
    comment: str | None = None
    raw: dict[str, object] = Field(default_factory=dict)


class BrokerOrder(AureonModel):
    """A resting (pending) order as the broker reports it (§43)."""

    order_ticket: int
    symbol: str
    order_type: OrderType
    volume: float = Field(gt=0)
    price: float
    sl: float | None = None
    tp: float | None = None
    placed_at: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None
    magic: int | None = None
    comment: str | None = None
    raw: dict[str, object] = Field(default_factory=dict)


class BrokerDeal(AureonModel):
    """A deal -- the record of something actually executing (§36).

    Deals are the only reliable evidence that money moved. Reconciliation matches on
    ``position_id`` and ``entry``, never on the comment (§36): a broker may truncate or
    drop a comment, and a close deal often carries none at all.
    """

    deal_id: int
    order_ticket: int | None = None
    position_id: int | None = None
    symbol: str
    direction: Direction
    entry: DealEntry
    volume: float = Field(gt=0)
    price: float
    executed_at: UtcDatetime
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    magic: int | None = None
    comment: str | None = None
    reason: str | None = Field(
        default=None, description="Broker reason code, kept verbatim (decision 15)."
    )
    raw: dict[str, object] = Field(default_factory=dict)

    @property
    def is_entry(self) -> bool:
        return self.entry is DealEntry.IN

    @property
    def is_exit(self) -> bool:
        return self.entry is DealEntry.OUT
