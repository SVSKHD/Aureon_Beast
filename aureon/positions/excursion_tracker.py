"""How far a position ran for and against, while it was open (§45).

Tracked from the live quote stream while the monitor is running, and **reconstructed
from M1 candles** for any stretch it was not.

## Why the source is recorded, not just the numbers

Live ticks and rebuilt candles have materially different resolution. A 1-minute candle's
high tells you the best price in that minute but not whether the position was still open
when it happened, and it cannot see a spike that reversed within the minute. A review that
mixed the two without saying so would overstate its own precision -- so
``excursion_source`` is part of the record, and ``reconstructed`` is the honest label for
the weaker measurement (§45).

## Signed by direction

``mfe`` is how far price ran **in the position's favour**, in points; ``mae`` how far
against, normally negative. Both are signed relative to the position's direction so a BUY
and a SELL are directly comparable -- the same convention the detection evaluator uses.

``mfe`` can be negative: it means the best price ever offered was still worse than the
entry. That is informative, so it is stored raw rather than clamped to zero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from aureon.models.base import to_utc
from aureon.models.enums import Direction, ExcursionSource
from aureon.models.market import Candle, QuoteSnapshot
from aureon.models.trade import Excursion, Trade

log = logging.getLogger(__name__)


@dataclass
class _Running:
    """Mutable excursion accumulator for one position."""

    direction: Direction
    open_price: float
    point: float
    source: ExcursionSource
    mfe: float | None = None
    mfe_at: datetime | None = None
    mfe_price: float | None = None
    mae: float | None = None
    mae_at: datetime | None = None
    mae_price: float | None = None

    def observe(self, price: float, at: datetime) -> bool:
        """Fold in one observed price. True if either extreme moved."""
        excursion = (price - self.open_price) * self.direction.sign / self.point
        changed = False
        if self.mfe is None or excursion > self.mfe:
            self.mfe, self.mfe_at, self.mfe_price = excursion, at, price
            changed = True
        if self.mae is None or excursion < self.mae:
            self.mae, self.mae_at, self.mae_price = excursion, at, price
            changed = True
        return changed

    def to_excursion(self) -> Excursion:
        return Excursion(
            mfe=self.mfe,
            mfe_at=self.mfe_at,
            mfe_price=self.mfe_price,
            mae=self.mae,
            mae_at=self.mae_at,
            mae_price=self.mae_price,
            source=self.source,
        )


class ExcursionTracker:
    """Maintains excursions for open positions (§45)."""

    def __init__(self, *, point: float = 0.01) -> None:
        self.point = point
        self._running: dict[str, _Running] = {}

    # ── Live tracking ─────────────────────────────────────────────────────────

    def track(self, trade: Trade, *, source: ExcursionSource = ExcursionSource.LIVE_TICKS) -> None:
        """Begin (or resume) tracking a position.

        An already-stored excursion is **carried forward** rather than restarted, so a
        monitor restart does not discard hours of accumulated extremes. If the stored
        figures were reconstructed, that weaker label is kept: the record must describe
        the weakest measurement it contains, not the most recent one.
        """
        if trade.trade_id in self._running:
            return
        stored = trade.excursion
        carried_source = (
            ExcursionSource.RECONSTRUCTED
            if stored.source is ExcursionSource.RECONSTRUCTED
            else source
        )
        self._running[trade.trade_id] = _Running(
            direction=trade.direction,
            open_price=trade.open_price,
            point=self.point,
            source=carried_source,
            mfe=stored.mfe,
            mfe_at=stored.mfe_at,
            mfe_price=stored.mfe_price,
            mae=stored.mae,
            mae_at=stored.mae_at,
            mae_price=stored.mae_price,
        )

    def on_quote(self, trade_id: str, quote: QuoteSnapshot) -> Excursion | None:
        """Fold in a quote. Returns the updated excursion only when it changed.

        The **exit** side of the book is used, because that is the price the position
        could actually be closed at: the bid for a long, the ask for a short. Using the
        mid would flatter every excursion by half the spread.
        """
        running = self._running.get(trade_id)
        if running is None:
            return None
        price = quote.bid if running.direction is Direction.BUY else quote.ask
        if not running.observe(price, to_utc(quote.captured_at)):
            return None
        return running.to_excursion()

    def current(self, trade_id: str) -> Excursion | None:
        running = self._running.get(trade_id)
        return running.to_excursion() if running else None

    def release(self, trade_id: str) -> Excursion | None:
        """Stop tracking a closed position, returning its final excursion."""
        running = self._running.pop(trade_id, None)
        return running.to_excursion() if running else None

    @property
    def tracked(self) -> int:
        return len(self._running)


def reconstruct_from_candles(
    trade: Trade,
    candles: list[Candle],
    *,
    point: float = 0.01,
    until: datetime | None = None,
) -> Excursion:
    """Rebuild excursions for a position the monitor did not watch live (§45).

    Only candles that overlap the position's life are used: one that closed before the
    position opened, or opened after it closed, says nothing about it. Including them
    would attribute someone else's price action to this trade.

    The result is always labelled ``reconstructed``, never ``live_ticks``, however
    complete it looks -- a candle cannot say whether the position was still open at the
    moment of its high.
    """
    opened = trade.open_time.utc
    closed = to_utc(until) if until else (trade.close_time.utc if trade.close_time else None)

    running = _Running(
        direction=trade.direction,
        open_price=trade.open_price,
        point=point,
        source=ExcursionSource.RECONSTRUCTED,
    )

    for candle in candles:
        if candle.close_time <= opened:
            continue
        if closed is not None and candle.open_time.utc >= closed:
            continue
        # Both extremes of the candle are observed. Their order within the candle is
        # unknowable, which is precisely the imprecision the `reconstructed` label warns
        # about -- the timestamps are the candle's close, the first moment either was known.
        running.observe(candle.high, candle.close_time)
        running.observe(candle.low, candle.close_time)

    return running.to_excursion()
