"""Liveness and system state (§59, §61-§63, §67).

Freshness is computed, never stored. A stored "status: live" would keep claiming
liveness precisely when the writer had died -- the one moment the field matters.
Deriving it from ``updated_at`` at read time means a dead service looks dead
because nothing is updating its timestamp.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from aureon.models.base import AureonDocument, AureonModel, UtcDatetime, to_utc, utc_now
from aureon.models.enums import Freshness, MarketState, Timeframe
from aureon.models.market import QuoteSnapshot

# §84 defaults; overridable via config. 46s old must read STALE at 45s.
DEFAULT_STALE_AFTER_SECONDS = 45.0
DEFAULT_OFFLINE_AFTER_SECONDS = 180.0


def freshness_of(
    updated_at: datetime | None,
    *,
    stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
    offline_after: float = DEFAULT_OFFLINE_AFTER_SECONDS,
    now: datetime | None = None,
) -> Freshness:
    """Classify an ``updated_at`` as LIVE / STALE / OFFLINE.

    Shared by ``/status``, the Vue dashboard's freshness composable and the
    heartbeat checks, so all three agree by construction rather than by three
    implementations happening to match (§62).
    """
    if updated_at is None:
        return Freshness.OFFLINE
    age = (to_utc(now or utc_now()) - to_utc(updated_at)).total_seconds()
    if age >= offline_after:
        return Freshness.OFFLINE
    if age >= stale_after:
        return Freshness.STALE
    return Freshness.LIVE


class Heartbeat(AureonDocument):
    """A service's liveness beat, at ``heartbeats/{service}`` (§67).

    Decision 10: this document is the source of truth. A copy is also embedded in
    ``system_state`` so ``/status`` and the dashboard can render everything from a
    single read, but the copy is derived -- never written independently.
    """

    service: str = Field(description="observer | executor | monitor | discord")
    updated_at: UtcDatetime = Field(default_factory=utc_now)
    instance_id: str | None = None
    detail: dict[str, object] = Field(default_factory=dict)

    def freshness(
        self,
        *,
        stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
        offline_after: float = DEFAULT_OFFLINE_AFTER_SECONDS,
        now: datetime | None = None,
    ) -> Freshness:
        return freshness_of(
            self.updated_at, stale_after=stale_after, offline_after=offline_after, now=now
        )


class SymbolState(AureonModel):
    """Per symbol/timeframe observation state (§59).

    ``last_quote`` carries the most recent bid/ask **as state**, not as a tick stream.
    CLAUDE.md forbids tick data in Firestore, and this respects that: it is one current
    snapshot per symbol, overwritten in place and throttled with the rest of
    ``system_state`` (at most every ``AUREON_STATE_HEARTBEAT_SECONDS``), never an append.

    It exists because Discord must show bid/ask on the §40 confirmation screen and must
    check the quote's age before confirming (§27), while being forbidden from calling the
    broker. Without it there is no honest price for Discord to show at all (decision 80).
    """

    symbol: str
    timeframe: Timeframe
    market_state: MarketState = MarketState.UNKNOWN
    last_closed_candle_time: UtcDatetime | None = None
    last_tick_at: UtcDatetime | None = None
    last_quote: QuoteSnapshot | None = Field(
        default=None,
        description="Latest bid/ask as state, for Discord's screens (decision 80).",
    )
    detections_today: int = Field(default=0, ge=0)

    # ── EMA cross tallies (§13, §66) ──────────────────────────────────────────
    # Six rather than two: "4 crosses today" and "3 up, 1 down" answer different
    # questions and neither is derivable from the other. Reset on the MARKET clock,
    # so the evening's crosses land under the broker date a trader would name.
    ema_crosses_today: int = Field(default=0, ge=0)
    ema_crosses_session: int = Field(default=0, ge=0)
    bullish_crosses_today: int = Field(default=0, ge=0)
    bearish_crosses_today: int = Field(default=0, ge=0)
    bullish_crosses_session: int = Field(default=0, ge=0)
    bearish_crosses_session: int = Field(default=0, ge=0)


class SystemState(AureonDocument):
    """One document describing the whole system's health (§59, §61-§63).

    Written by the observer on candle close, and otherwise throttled to at most
    once every ``AUREON_STATE_HEARTBEAT_SECONDS``. The throttle exists because an
    unthrottled tick-rate write would cost real money in Firestore writes while
    telling a reader nothing new.
    """

    updated_at: UtcDatetime = Field(default_factory=utc_now)
    symbols: tuple[SymbolState, ...] = ()
    # Decision 10: embedded copies for a one-read status view.
    heartbeats: dict[str, UtcDatetime] = Field(default_factory=dict)
    trading_enabled: bool | None = Field(
        default=None, description="Mirror of settings/execution, for display only."
    )
    notes: str | None = None

    def freshness(
        self,
        *,
        stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
        offline_after: float = DEFAULT_OFFLINE_AFTER_SECONDS,
        now: datetime | None = None,
    ) -> Freshness:
        """Freshness of the observer's own state write (§59)."""
        return freshness_of(
            self.updated_at, stale_after=stale_after, offline_after=offline_after, now=now
        )

    def service_freshness(
        self,
        *,
        stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
        offline_after: float = DEFAULT_OFFLINE_AFTER_SECONDS,
        now: datetime | None = None,
    ) -> dict[str, Freshness]:
        """Freshness per service, from the embedded heartbeat copies."""
        return {
            name: freshness_of(
                ts, stale_after=stale_after, offline_after=offline_after, now=now
            )
            for name, ts in self.heartbeats.items()
        }
