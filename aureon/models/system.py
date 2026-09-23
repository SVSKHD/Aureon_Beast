"""Liveness and system state (§59, §61-§63, §67).

Freshness is computed, never stored. A stored "status: live" would keep claiming
liveness precisely when the writer had died -- the one moment the field matters.
Deriving it from ``updated_at`` at read time means a dead service looks dead
because nothing is updating its timestamp.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from aureon.models.assessment import TrendRead
from aureon.models.base import AureonDocument, AureonModel, UtcDatetime, to_utc, utc_now
from aureon.models.enums import (
    AccountMode,
    Freshness,
    MarketState,
    SessionName,
    SleepPhase,
    Timeframe,
)
from aureon.models.market import QuoteSnapshot
from aureon.models.mtf import MtfContext
from aureon.models.profile import ProfileSummary, VolatilityContext

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

    # ── The indicator snapshot (§59, §66) ─────────────────────────────────────
    # Values, not labels, for the same reason IndicatorSnapshot stores values: "rsi was
    # 61.4" survives a later change to whatever we currently call overbought, where a
    # stored label does not. ``rsi_zone`` is the one exception and is derived here so
    # /status and a dashboard cannot disagree about the thresholds.
    ema_fast: float | None = None
    ema_slow: float | None = None
    ema_distance: float | None = Field(
        default=None, description="fast - slow, in price. Sign is the current bias."
    )
    rsi: float | None = None
    rsi_zone: str | None = Field(
        default=None, description="overbought | oversold | neutral, derived from rsi."
    )

    # ── Volume profile and volatility (9B, §19) ───────────────────────────────
    # On SymbolState rather than on SystemState because after 9A-2 the state document is
    # per symbol, and a profile is a fact about one instrument. Summaries rather than
    # profiles: the panel needs the POC, the value area and the nodes, and two hundred
    # bins per scope would be a payload nobody reads on a phone.
    volume_profile: dict[str, ProfileSummary] = Field(
        default_factory=dict,
        description="current_session | asia | day -> that scope's summary (9B).",
    )
    volatility: VolatilityContext | None = Field(
        default=None, description="ATR(14) and the session range vs its median (9B)."
    )

    # ── The trend read (9D, §62) ──────────────────────────────────────────────
    #: Computed by the OBSERVER, which has the candles, and published here for Discord to
    #: read. `/monitor` renders it without recomputing anything: the Discord process holds
    #: no data provider at all, and a boundary test enforces that by inspecting BotContext's
    #: own annotations (decision 194).
    trend_read: TrendRead | None = Field(
        default=None,
        description="What the last N closed candles did, as facts and a summary (9D).",
    )
    #: Where the higher timeframes stood at the last closed candle (11D). Published for the
    #: same reason the trend read is: Discord holds no data provider, so the process with the
    #: feed computes it and Discord renders it. ``None`` when the observer has too little M5
    #: history to seed an EMA above M5 -- which is not "flat", and is why every reader goes
    #: through ``MtfContext.bias_of`` rather than assuming a missing read means sideways.
    mtf: MtfContext | None = Field(
        default=None,
        description="Higher-timeframe reads at the last closed candle (11D).",
    )

    # ── Session context (§18) ─────────────────────────────────────────────────
    session: SessionName | None = None
    # session_trend is the most recently COMPLETED session summary, kept for historical
    # compatibility. session_live_trend is the observer's in-progress read for the session
    # that owns the latest closed candle.
    session_trend: str | None = None
    session_live_trend: str | None = None
    session_open: float | None = None
    session_close: float | None = None
    session_high: float | None = None
    session_low: float | None = None

    # ── Last of each event kind (§59) ─────────────────────────────────────────
    # A dict rather than a typed sub-model per event: these are display values read by
    # /status and, later, a dashboard, and a new agent must be able to report its last
    # event without a schema migration. Shapes are documented on LastEvent below.
    last_cross: dict[str, object] | None = Field(
        default=None, description="{direction, at, price, detection_id}"
    )
    last_cross_at: UtcDatetime | None = Field(
        default=None,
        description="Denormalised from last_cross so freshness needs no dict parsing.",
    )
    last_sweep: dict[str, object] | None = Field(
        default=None, description="{direction, level_type, at}"
    )
    last_wick: dict[str, object] | None = Field(
        default=None, description="{classification, at}"
    )
    last_breakout: dict[str, object] | None = Field(
        default=None, description="{direction, level_type, at}"
    )


    @model_validator(mode="after")
    def _derive_display_values(self) -> SymbolState:
        """Fill ``ema_distance`` and ``rsi_zone`` from their inputs when absent.

        They are stored rather than computed on read because a dashboard reads this
        document directly and should not have to re-implement "where does overbought
        begin". But a stored derivation can go stale or arrive inconsistent, so it is
        derived HERE, on every validation -- which means a document that carries an EMA
        pair always carries the matching distance, and one that carries neither carries
        no distance either.

        Only filled when absent, never overwritten: a writer that computed them from the
        same values gets the same answer, and one that deliberately set something else
        deserves to keep it rather than have it silently corrected.
        """
        if self.ema_distance is None and None not in (self.ema_fast, self.ema_slow):
            object.__setattr__(
                self, "ema_distance", round(self.ema_fast - self.ema_slow, 8)
            )
        if self.rsi_zone is None and self.rsi is not None:
            from aureon.agents.rsi_agent import rsi_zone

            object.__setattr__(
                self, "rsi_zone", rsi_zone(self.rsi, overbought=70.0, oversold=30.0)
            )
        return self


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
    #: Which kind of money the observer's terminal is logged into (11A, F-3).
    #:
    #: Published so `/status` and the ops channel can say it without opening a terminal, and
    #: so a session's evidence file records which account its candles came from. Display
    #: only: the executor reads the account mode from its OWN broker connection, because a
    #: guard that trusted a value another process wrote would be trusting a document instead
    #: of a terminal.
    account_mode: AccountMode | None = None
    #: Where the observer is in the weekly sleep cycle (11B), and when the market is next
    #: expected to open.
    #:
    #: Published because Discord may not call the broker and must not compute the weekly
    #: boundary itself: two processes each deciding when the market opens is two places for
    #: that answer to be wrong, and the one holding the feed is the one that knows. Display
    #: only, like ``account_mode`` -- the executor refuses a closed market through the guard,
    #: on its own broker's word, not on a phase another process wrote here.
    sleep_phase: SleepPhase | None = None
    next_market_open: UtcDatetime | None = None
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
