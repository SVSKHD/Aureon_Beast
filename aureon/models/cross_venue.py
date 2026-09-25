"""Agent 19 models: cross-venue replication blueprints and target validation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from aureon.models.base import AureonModel, UtcDatetime
from aureon.models.enums import Direction


class ExecutionVenue(StrEnum):
    MT5 = "mt5"
    CTRADER = "ctrader"
    PAPER = "paper"


class ReplicationState(StrEnum):
    BLUEPRINT = "blueprint"
    WAITING_TARGET = "waiting_target"
    EXECUTABLE = "executable"
    SKIP_LATE = "skip_late"
    SKIP_MOVED = "skip_moved"
    SKIP_INVALIDATED = "skip_invalidated"
    SKIP_SPREAD = "skip_spread"
    SKIP_MARKET = "skip_market"
    SKIP_RISK = "skip_risk"
    SENT = "sent"
    UNKNOWN = "unknown"
    FILLED = "filled"
    REJECTED = "rejected"


class CrossVenueBlueprint(AureonModel):
    blueprint_id: str
    source_venue: ExecutionVenue = ExecutionVenue.MT5
    target_venue: ExecutionVenue = ExecutionVenue.CTRADER
    source_symbol: str
    target_symbol: str
    direction: Direction
    source_observed_at: UtcDatetime
    source_price: float
    scenario_signature: str | None = None

    preferred_zone_low: float | None = None
    preferred_zone_high: float | None = None
    invalidation_price: float | None = None

    primary_target_move: float = Field(default=10.0, gt=0)
    expected_delay_ms: float = Field(default=0.0, ge=0)
    max_valid_delay_ms: float = Field(default=2500.0, gt=0)
    max_price_drift: float = Field(default=2.0, ge=0)
    max_spread_points: float | None = Field(default=None, ge=0)

    # Intentionally no target volume. Contract/volume semantics are target-venue facts
    # and must be resolved by the target execution guard.
    source_detection_id: str | None = None
    source_request_id: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _zone_ordered(self) -> "CrossVenueBlueprint":
        if (
            self.preferred_zone_low is not None
            and self.preferred_zone_high is not None
            and self.preferred_zone_low > self.preferred_zone_high
        ):
            raise ValueError("preferred_zone_low exceeds preferred_zone_high")
        return self


class CrossVenueDecision(AureonModel):
    blueprint_id: str
    state: ReplicationState
    actual_delay_ms: float = Field(ge=0)
    target_price: float | None = None
    signed_price_drift: float | None = None
    absolute_price_drift: float | None = Field(default=None, ge=0)
    expected_delay_ms: float = Field(default=0.0, ge=0)
    delay_delta_ms: float = 0.0
    within_entry_zone: bool | None = None
    spread_points: float | None = Field(default=None, ge=0)
    reason: str
    evidence: tuple[str, ...] = ()


class VenueLatencySample(AureonModel):
    blueprint_id: str
    source_venue: ExecutionVenue
    target_venue: ExecutionVenue
    symbol: str
    source_observed_at: UtcDatetime
    target_observed_at: UtcDatetime
    latency_ms: float = Field(ge=0)
    source_price: float
    target_price: float
    signed_price_drift: float
