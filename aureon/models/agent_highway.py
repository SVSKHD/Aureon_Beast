"""Typed messages and health records for the in-process Agent Highway.

The Highway is deterministic and synchronous by design. It is not a background queue and it
never reorders market observations. Its job is isolation + transport: every agent connects
through a bridge, publishes immutable snapshots, and can fail without throwing through the
whole observer loop.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from aureon.models.base import AureonModel, UtcDatetime


class AgentHealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CIRCUIT_OPEN = "circuit_open"
    DISABLED = "disabled"


class HighwayEnvelope(AureonModel):
    sequence: int = Field(ge=1)
    topic: str
    source_agent: str
    symbol: str | None = None
    timeframe: str | None = None
    observed_at: UtcDatetime | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class AgentHealth(AureonModel):
    agent_name: str
    state: AgentHealthState = AgentHealthState.HEALTHY
    invocations: int = Field(default=0, ge=0)
    successes: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    skipped_by_circuit: int = Field(default=0, ge=0)
    last_error: str | None = None
    last_success_sequence: int | None = Field(default=None, ge=1)
    last_failure_sequence: int | None = Field(default=None, ge=1)
    circuit_open_until_invocation: int | None = Field(default=None, ge=1)


class BridgeResult(AureonModel):
    ok: bool
    skipped: bool = False
    error: str | None = None
    health: AgentHealth
