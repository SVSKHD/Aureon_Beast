"""Runtime settings held in Firestore at ``settings/execution`` (§56, §57).

Decision 11: these override the environment at runtime. The reason is
operational: ``/trading disable`` has to take effect on the next request without
a redeploy or a process restart, so the value an executor obeys must be the one
in Firestore rather than the one baked into its environment at boot.

``trading_enabled`` defaults to **false**, so a freshly deployed system cannot
trade until a human turns it on.
"""

from __future__ import annotations

from pydantic import Field

from aureon.models.base import AureonDocument, UtcDatetime


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

    updated_at: UtcDatetime | None = None
    updated_by: str | None = None
    disabled_reason: str | None = None

    def symbol_allowed(self, symbol: str) -> bool:
        return not self.allowed_symbols or symbol in self.allowed_symbols
