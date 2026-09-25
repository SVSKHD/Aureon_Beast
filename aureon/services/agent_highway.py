"""Deterministic Agent Highway and crash-isolating bridges.

No threads, no background tasks, no retries inside agent code. Every call is made in candle
order. A failing agent returns a BridgeResult instead of unwinding the observer, and after a
configurable number of consecutive failures its circuit opens for a deterministic number of
future invocations before a half-open probe is attempted.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from typing import Any

from aureon.models.agent_highway import (
    AgentHealth,
    AgentHealthState,
    BridgeResult,
    HighwayEnvelope,
)

log = logging.getLogger(__name__)


class AgentHighway:
    def __init__(self, *, history_size: int = 512) -> None:
        if history_size < 1:
            raise ValueError("history_size must be positive")
        self._sequence = 0
        self._history: deque[HighwayEnvelope] = deque(maxlen=history_size)
        self._latest: dict[tuple[str, str | None, str | None], HighwayEnvelope] = {}
        self._health: dict[str, AgentHealth] = {}
        self._bridges: dict[str, AgentBridge] = {}

    def bridge(
        self,
        agent_name: str,
        *,
        failure_threshold: int = 3,
        probe_after_invocations: int = 5,
    ) -> "AgentBridge":
        existing = self._bridges.get(agent_name)
        if existing is not None:
            return existing
        bridge = AgentBridge(
            highway=self,
            agent_name=agent_name,
            failure_threshold=failure_threshold,
            probe_after_invocations=probe_after_invocations,
        )
        self._bridges[agent_name] = bridge
        self._health.setdefault(agent_name, AgentHealth(agent_name=agent_name))
        return bridge

    def publish(
        self,
        *,
        topic: str,
        source_agent: str,
        payload: dict[str, object] | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        observed_at: object | None = None,
    ) -> HighwayEnvelope:
        self._sequence += 1
        envelope = HighwayEnvelope(
            sequence=self._sequence,
            topic=topic,
            source_agent=source_agent,
            symbol=symbol,
            timeframe=timeframe,
            observed_at=observed_at,
            payload=dict(payload or {}),
        )
        self._history.append(envelope)
        self._latest[(topic, symbol, timeframe)] = envelope
        return envelope

    def latest(
        self, topic: str, *, symbol: str | None = None, timeframe: str | None = None
    ) -> HighwayEnvelope | None:
        return self._latest.get((topic, symbol, timeframe))

    def history(self, *, topic: str | None = None) -> tuple[HighwayEnvelope, ...]:
        if topic is None:
            return tuple(self._history)
        return tuple(item for item in self._history if item.topic == topic)

    def health(self, agent_name: str) -> AgentHealth:
        return self._health.setdefault(agent_name, AgentHealth(agent_name=agent_name))

    def health_snapshot(self) -> dict[str, AgentHealth]:
        return {
            name: health.model_copy(deep=True)
            for name, health in sorted(self._health.items())
        }

    def _set_health(self, health: AgentHealth) -> None:
        self._health[health.agent_name] = health


class AgentBridge:
    def __init__(
        self,
        *,
        highway: AgentHighway,
        agent_name: str,
        failure_threshold: int,
        probe_after_invocations: int,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if probe_after_invocations < 1:
            raise ValueError("probe_after_invocations must be positive")
        self.highway = highway
        self.agent_name = agent_name
        self.failure_threshold = failure_threshold
        self.probe_after_invocations = probe_after_invocations

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[BridgeResult, Any]:
        current = self.highway.health(self.agent_name)
        invocation = current.invocations + 1

        if current.state is AgentHealthState.DISABLED:
            health = current.model_copy(update={"invocations": invocation})
            self.highway._set_health(health)
            return BridgeResult(ok=False, skipped=True, health=health), None

        if (
            current.state is AgentHealthState.CIRCUIT_OPEN
            and current.circuit_open_until_invocation is not None
            and invocation < current.circuit_open_until_invocation
        ):
            health = current.model_copy(
                update={
                    "invocations": invocation,
                    "skipped_by_circuit": current.skipped_by_circuit + 1,
                }
            )
            self.highway._set_health(health)
            return BridgeResult(ok=False, skipped=True, health=health), None

        try:
            value = func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - bridge boundary intentionally contains agents
            failures = current.consecutive_failures + 1
            state = (
                AgentHealthState.CIRCUIT_OPEN
                if failures >= self.failure_threshold
                else AgentHealthState.DEGRADED
            )
            until = (
                invocation + self.probe_after_invocations
                if state is AgentHealthState.CIRCUIT_OPEN
                else None
            )
            health = current.model_copy(
                update={
                    "state": state,
                    "invocations": invocation,
                    "failures": current.failures + 1,
                    "consecutive_failures": failures,
                    "last_error": f"{type(exc).__name__}: {exc}",
                    "last_failure_sequence": invocation,
                    "circuit_open_until_invocation": until,
                }
            )
            self.highway._set_health(health)
            self.highway.publish(
                topic="agent.health",
                source_agent=self.agent_name,
                payload={
                    "state": health.state.value,
                    "error": health.last_error or "unknown",
                    "consecutive_failures": health.consecutive_failures,
                },
            )
            log.exception("agent bridge contained failure: %s", self.agent_name)
            return (
                BridgeResult(
                    ok=False,
                    skipped=False,
                    error=health.last_error,
                    health=health,
                ),
                None,
            )

        was_unhealthy = current.state is not AgentHealthState.HEALTHY
        health = current.model_copy(
            update={
                "state": AgentHealthState.HEALTHY,
                "invocations": invocation,
                "successes": current.successes + 1,
                "consecutive_failures": 0,
                "last_error": None,
                "last_success_sequence": invocation,
                "circuit_open_until_invocation": None,
            }
        )
        self.highway._set_health(health)
        if was_unhealthy:
            self.highway.publish(
                topic="agent.health",
                source_agent=self.agent_name,
                payload={"state": AgentHealthState.HEALTHY.value, "recovered": True},
            )
        return BridgeResult(ok=True, health=health), value

    def disable(self) -> AgentHealth:
        current = self.highway.health(self.agent_name)
        health = current.model_copy(update={"state": AgentHealthState.DISABLED})
        self.highway._set_health(health)
        return health

    def reset(self) -> AgentHealth:
        current = self.highway.health(self.agent_name)
        health = current.model_copy(
            update={
                "state": AgentHealthState.HEALTHY,
                "consecutive_failures": 0,
                "last_error": None,
                "circuit_open_until_invocation": None,
            }
        )
        self.highway._set_health(health)
        return health
