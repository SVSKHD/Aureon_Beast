"""Runtime settings held in Firestore at ``settings/execution`` (§56, §57).

Decision 11: these override the environment at runtime. The reason is
operational: ``/trading disable`` has to take effect on the next request without
a redeploy or a process restart, so the value an executor obeys must be the one
in Firestore rather than the one baked into its environment at boot.

``trading_enabled`` defaults to **false**, so a freshly deployed system cannot
trade until a human turns it on.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from aureon.models.base import AureonDocument, AureonModel, UtcDatetime


class SymbolLimits(AureonModel):
    """Per-symbol overrides of the execution limits (9A).

    Every field is optional and ``None`` means "use the global value", so an entry can
    override one limit without restating the others.

    Why per symbol at all: two of the three limits are in POINTS, and a point is a
    different amount of money on every instrument. ``max_deviation_points = 20`` is $0.20
    on gold, about 0.008% of its price, and $0.02 on silver, about 0.07% -- so one number
    tolerates nearly ten times more slippage, relative to price, on the second symbol than
    on the first. ``max_lot`` is in lots, and one lot is 100 oz of gold against 5000 oz of
    silver, so it is a different notional too.

    The shipped default is **empty**: the global values apply to every symbol, which is
    what every existing deployment has and is a KNOWN approximation rather than a
    researched one. Nothing here invents a silver number; it makes room for one to be set
    deliberately, in the settings document, by a human who can say why.
    """

    model_config = ConfigDict(extra="forbid")

    max_lot: float | None = Field(default=None, gt=0)
    max_spread_points: float | None = Field(default=None, gt=0)
    max_deviation_points: int | None = Field(default=None, ge=0)


class ResolvedLimits(AureonModel):
    """The limits that actually apply to one symbol, with no ``None`` left.

    Returned as a group rather than field by field so a caller cannot read a per-symbol
    ``max_lot`` and a global ``max_spread_points`` in the same decision and believe both
    came from the same place.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    max_lot: float
    max_spread_points: float
    max_deviation_points: int
    #: True when at least one value came from a per-symbol entry. Reported on the
    #: confirmation screen so a human can see they are not looking at the global policy.
    overridden: tuple[str, ...] = ()


#: Which agents Discord announces by default (9C). The four a human watching a chart would
#: notice: a cross, a sweep, a break and a rejection wick. `rsi` and `session_trend` are
#: deliberately absent -- they are context, and an alert for every RSI reading is an alert
#: for nothing.
DEFAULT_NOTIFIED_AGENTS: tuple[str, ...] = ("ema_cross", "wick", "liquidity", "breakout")


#: 12 T-11. Every name that may appear in ``settings/notifications.setup_states``: all nine
#: states a setup can be in, and all seventeen descriptive ``WATCH_*`` event types.
#:
#: Twenty-six, not the nine the field's name suggests. A name in this list is a **trigger**,
#: matched against either the setup's state or the event type that caused the change, whichever
#: the change was -- see ``Notifier._setup_trigger``. The field keeps the name ``setup_states``
#: because that is the documented key an operator edits.
SETUP_STATE_ANNOUNCEMENTS: tuple[str, ...] = (
    "observing",
    "watch",
    "developing",
    "confirmed",
    "pullback",
    "continuation",
    "fakeout_risk",
    "completed",
    "invalidated",
)

#: The three observations worth telling a human about before anything has confirmed: a level
#: tested a third time, a bar that poked through and came back, tick volume expanding at a level.
#: These are ON by default.
NOTABLE_SETUP_EVENTS: tuple[str, ...] = (
    "repeated_level_test",
    "breakout_pressure",
    "volume_expansion_at_level",
    "favourable_move_6_reached",
    "favourable_move_20_reached",
    "favourable_move_40_reached",
)

#: The other fourteen. Configurable and OFF by default, which is the one judgement call in this
#: block and is made in the open: "price is near the previous day's high" is true on dozens of
#: consecutive candles, so a card subscribed to it would be edited on every one of them. Turning
#: any of these on is a one-line edit to ``settings/notifications.setup_states``, and it takes
#: effect on the next sweep without a redeploy -- which is the whole reason this lives in
#: Firestore rather than in the environment (decision 11).
QUIET_SETUP_EVENTS: tuple[str, ...] = (
    "favourable_move_6_tracking",
    "ema_fast_slope_change",
    "ema_gap_narrowing",
    "high_tick_volume_rejection",
    "high_tick_volume_sweep",
    "profile_reclaim",
    "profile_rejection",
    "proximity_liquidity_level",
    "proximity_poc",
    "proximity_prev_day_extreme",
    "proximity_session_extreme",
    "proximity_vah",
    "proximity_val",
    "rsi_momentum_turn",
    "tick_volume_expansion",
)

#: Every trigger a human may name. Validated against, so a typo in the settings document is a
#: refused write rather than a silently ignored line.
SETUP_ANNOUNCEMENTS: tuple[str, ...] = (
    SETUP_STATE_ANNOUNCEMENTS + NOTABLE_SETUP_EVENTS + QUIET_SETUP_EVENTS
)

DEFAULT_SETUP_ANNOUNCEMENTS: tuple[str, ...] = (
    SETUP_STATE_ANNOUNCEMENTS + NOTABLE_SETUP_EVENTS
)


class NotificationSettings(AureonDocument):
    """What Discord announces, held at ``settings/notifications`` (9C).

    In Firestore rather than the environment for the same reason `trading_enabled` is
    (decision 11): silencing a noisy agent at 02:00 must take effect on the next detection,
    without a redeploy. Nothing here can enable trading or change what is detected -- an
    agent's output is stored either way, and this decides only whether a message is posted.
    """

    enabled_kinds: tuple[str, ...] = Field(
        default=DEFAULT_NOTIFIED_AGENTS,
        description="agent_name values Discord posts an embed for (9C).",
    )
    #: False silences every detection embed without forgetting which kinds were enabled.
    detections_enabled: bool = True
    #: 12 T-11. Which setup changes get a card, named from ``SETUP_ANNOUNCEMENTS``. A trigger is
    #: matched against either the setup's state or the event type that caused the change,
    #: whichever the change was -- see ``Notifier._setup_trigger``.
    setup_states: tuple[str, ...] = Field(
        default=DEFAULT_SETUP_ANNOUNCEMENTS,
        description="Setup states and watch-event types Discord posts or edits a card for.",
    )
    #: False silences every setup card without forgetting which triggers were enabled. Separate
    #: from ``detections_enabled`` because the two answer different questions: a channel can
    #: reasonably want structures and not every EMA cross.
    setups_enabled: bool = True
    updated_at: UtcDatetime | None = None
    updated_by: str | None = None

    @model_validator(mode="after")
    def _every_trigger_is_a_real_one(self) -> NotificationSettings:
        """A name that is not a trigger is refused, not ignored.

        A typo in a hand-edited settings document -- ``confermed`` -- would otherwise be a line
        that silently never fires, which is indistinguishable from a feature that does not work.
        The write fails and the operator sees why.
        """
        unknown = [name for name in self.setup_states if name not in SETUP_ANNOUNCEMENTS]
        if unknown:
            raise ValueError(
                f"{unknown} is not a setup announcement trigger; the twenty-eight are "
                f"{list(SETUP_ANNOUNCEMENTS)}"
            )
        return self

    def announces(self, agent_name: str) -> bool:
        return self.detections_enabled and agent_name in self.enabled_kinds

    def announces_setup(self, trigger: str) -> bool:
        """Whether a setup change named by ``trigger`` gets a card."""
        return self.setups_enabled and trigger in self.setup_states


class ExecutionSettings(AureonDocument):
    """Execution gates and limits (§56, §84)."""

    # Decision 11: fail-closed default. A new deploy, or a document that has lost
    # this field, must not be able to place a trade.
    trading_enabled: bool = False

    max_lot: float = Field(default=1.0, gt=0)
    max_spread_points: float = Field(default=50.0, gt=0)
    max_deviation_points: int = Field(default=20, ge=0)
    max_open_positions: int = Field(default=5, ge=0)
    max_daily_trades: int = Field(default=20, ge=0)

    confirmation_ttl_seconds: float = Field(default=60.0, gt=0)
    quote_ttl_seconds: float = Field(default=15.0, gt=0)
    status_stale_after_seconds: float = Field(default=45.0, gt=0)
    executor_lease_seconds: float = Field(default=60.0, gt=0)

    allowed_symbols: tuple[str, ...] = Field(
        default=(), description="Empty means no symbol allowlist is enforced."
    )

    #: Per-symbol overrides, keyed by symbol. Empty means the global limits apply to
    #: every symbol -- see ``SymbolLimits`` for why that is a known approximation rather
    #: than an equivalence.
    per_symbol: dict[str, SymbolLimits] = Field(default_factory=dict)

    #: Incremented on every write through ``set_trading_enabled`` (§57). The
    #: transaction's read-then-conditional-write compares it, so two concurrent
    #: toggles cannot both win: the loser's transaction sees a changed version and
    #: Firestore aborts it. Without this, a disable racing an enable is last-write-wins
    #: on the one setting that decides whether real money can move.
    settings_version: int = Field(default=0, ge=0)

    updated_at: UtcDatetime | None = None
    updated_by: str | None = None
    disabled_reason: str | None = None

    def symbol_allowed(self, symbol: str) -> bool:
        return not self.allowed_symbols or symbol in self.allowed_symbols

    def limits_for(self, symbol: str) -> ResolvedLimits:
        """The limits that apply to one symbol: its own where set, global otherwise."""
        entry = self.per_symbol.get(symbol.upper()) or SymbolLimits()
        overridden = tuple(
            name
            for name in ("max_lot", "max_spread_points", "max_deviation_points")
            if getattr(entry, name) is not None
        )
        return ResolvedLimits(
            symbol=symbol,
            max_lot=entry.max_lot if entry.max_lot is not None else self.max_lot,
            max_spread_points=(
                entry.max_spread_points
                if entry.max_spread_points is not None
                else self.max_spread_points
            ),
            max_deviation_points=(
                entry.max_deviation_points
                if entry.max_deviation_points is not None
                else self.max_deviation_points
            ),
            overridden=overridden,
        )
