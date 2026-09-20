"""Runtime settings held in Firestore at ``settings/execution`` (§56, §57).

Decision 11: these override the environment at runtime. The reason is
operational: ``/trading disable`` has to take effect on the next request without
a redeploy or a process restart, so the value an executor obeys must be the one
in Firestore rather than the one baked into its environment at boot.

``trading_enabled`` defaults to **false**, so a freshly deployed system cannot
trade until a human turns it on.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field

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
