"""Turning broker deals into trade state (§36, §44).

Deals are the only reliable evidence that money moved, so this module decides what a
position *did* purely from them.

## Match on position id, never on the comment (§36)

The comment carries the request token and is invaluable for finding an *entry* whose
result was lost (Phase 4). It is useless for exits: a closing deal often carries no
comment at all, or one the broker wrote itself ("sl 2398.00"), or a truncation. Matching
exits by comment would therefore miss every stop-loss and every close from a phone.

``mt5_position_id`` plus the deal's ``entry`` type is the durable pairing: entries open a
position, exits reduce it, and the position id ties them together regardless of what any
comment says.

## Realized P&L is the broker's arithmetic, not ours

It is the **sum of the closing deals' profit**, plus commission and swap. Recomputing it
from prices and volumes would silently disagree with the account statement the moment a
symbol has a contract size we assumed, a currency conversion, or a partial close at two
prices. The broker's number is the one the money followed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aureon.models.broker import BrokerDeal
from aureon.models.enums import DealEntry
from aureon.models.trade import CLOSE_REASON_CONVENTION

log = logging.getLogger(__name__)

# MT5 deal reason codes → our convention (decision 15). Anything else keeps its raw
# value and reports "unknown", so the evidence for widening this map survives.
CLOSE_REASON_BY_BROKER_REASON: dict[str, str] = {
    "sl": "sl",
    "tp": "tp",
    "so": "broker",  # stop-out: the broker closed it, not a human
    "rollover": "broker",
    "client": "manual",  # closed from the desktop terminal
    "mobile": "mobile",
    "web": "manual",
    "expert": "discord",  # placed/closed by an EA -- for us, that is Aureon
}

UNKNOWN_REASON = "unknown"


def close_reason_for(deal: BrokerDeal) -> tuple[str, str | None]:
    """Our close reason, plus the broker's raw code kept verbatim (decision 15).

    An unmapped code yields ``"unknown"`` and preserves the raw value rather than
    guessing. Discarding it would destroy exactly the evidence needed to decide what the
    enum should eventually contain.
    """
    raw = (deal.reason or "").strip().lower() or None
    if raw is None:
        return UNKNOWN_REASON, None
    mapped = CLOSE_REASON_BY_BROKER_REASON.get(raw, UNKNOWN_REASON)
    if mapped not in CLOSE_REASON_CONVENTION:  # pragma: no cover - map is curated
        mapped = UNKNOWN_REASON
    return mapped, deal.reason


@dataclass
class PositionOutcome:
    """What the deals say happened to one position."""

    position_id: int
    opened_volume: float = 0.0
    closed_volume: float = 0.0
    open_price: float | None = None
    close_price: float | None = None
    close_reason: str = UNKNOWN_REASON
    close_reason_raw: str | None = None
    realized_pnl: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    deal_ids: tuple[int, ...] = ()
    entry_deals: list[BrokerDeal] = field(default_factory=list)
    exit_deals: list[BrokerDeal] = field(default_factory=list)

    @property
    def is_fully_closed(self) -> bool:
        """Whether the exits account for everything that was opened.

        Tolerance absorbs float noise in lot arithmetic, not a genuinely open remainder.
        """
        return self.opened_volume > 0 and self.closed_volume + 1e-9 >= self.opened_volume

    @property
    def is_partially_closed(self) -> bool:
        return 0 < self.closed_volume < self.opened_volume - 1e-9

    @property
    def remaining_volume(self) -> float:
        return round(max(self.opened_volume - self.closed_volume, 0.0), 8)


def summarise_position(deals: list[BrokerDeal], position_id: int) -> PositionOutcome:
    """Fold every deal for one position into an outcome (§36, §44).

    ``INOUT`` deals -- a reversal that closes one direction and opens the other in a
    single deal -- are counted on **both** sides, because that is what they did. Treating
    one as a pure exit would leave the newly opened side invisible.
    """
    relevant = [d for d in deals if d.position_id == position_id]
    outcome = PositionOutcome(position_id=position_id)
    if not relevant:
        return outcome

    ordered = sorted(relevant, key=lambda d: (d.executed_at, d.deal_id))
    for deal in ordered:
        if deal.entry in (DealEntry.IN, DealEntry.INOUT):
            outcome.entry_deals.append(deal)
            outcome.opened_volume += deal.volume
            if outcome.open_price is None:
                outcome.open_price = deal.price
        if deal.entry in (DealEntry.OUT, DealEntry.INOUT):
            outcome.exit_deals.append(deal)
            outcome.closed_volume += deal.volume
            # The LAST exit's price and reason are the ones reported: that is the price
            # the position finally left the market at.
            outcome.close_price = deal.price
            outcome.close_reason, outcome.close_reason_raw = close_reason_for(deal)

        # The broker's own arithmetic, summed across every deal (see the module docstring).
        outcome.realized_pnl += deal.profit
        outcome.commission += deal.commission
        outcome.swap += deal.swap

    outcome.opened_volume = round(outcome.opened_volume, 8)
    outcome.closed_volume = round(outcome.closed_volume, 8)
    outcome.realized_pnl = round(outcome.realized_pnl, 8)
    outcome.deal_ids = tuple(d.deal_id for d in ordered)
    return outcome


def summarise_all(deals: list[BrokerDeal]) -> dict[int, PositionOutcome]:
    """One outcome per position id present in the deals."""
    ids = {d.position_id for d in deals if d.position_id is not None}
    return {pid: summarise_position(deals, pid) for pid in ids}
