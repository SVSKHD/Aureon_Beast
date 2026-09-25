"""Agent Highway: crash isolation, circuit breaking and deterministic transport."""

from __future__ import annotations

import pandas as pd

from aureon.agents.base_agent import BaseAgent
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.models.agent_highway import AgentHealthState
from aureon.models.detection import CandleContext
from aureon.services.agent_highway import AgentHighway
from tests.conftest import ACCOUNT_SCOPE, MARKET_TZ


class CrashingAgent(BaseAgent):
    agent_name = "crashing_test_agent"
    agent_version = "1.0.0"

    def params_snapshot(self) -> dict[str, object]:
        return {"warmup_bars": 1}

    def on_closed_candle(self, window: pd.DataFrame, ctx: CandleContext):
        raise RuntimeError("intentional bridge failure")


class AlwaysAgent(BaseAgent):
    agent_name = "always_test_agent"
    agent_version = "1.0.0"

    def params_snapshot(self) -> dict[str, object]:
        return {"warmup_bars": 1}

    def on_closed_candle(self, window: pd.DataFrame, ctx: CandleContext):
        return [
            self.build_detection(
                ctx=ctx,
                event_key="seen",
                price=float(window["close"].iloc[-1]),
            )
        ]


def test_one_agent_crash_does_not_stop_sibling_agents(candles) -> None:
    highway = AgentHighway()
    engine = AnalysisEngine(
        [CrashingAgent(), AlwaysAgent()],
        account_scope=ACCOUNT_SCOPE,
        market_tz=MARKET_TZ,
        agent_highway=highway,
    )

    detections = engine.feed(candles[:5])

    assert len(detections) == 5
    assert {d.agent_name for d in detections} == {"always_test_agent"}

    crashed = highway.health("crashing_test_agent:XAUUSD:M5")
    healthy = highway.health("always_test_agent:XAUUSD:M5")
    assert crashed.state is AgentHealthState.CIRCUIT_OPEN
    assert crashed.failures == 3
    assert crashed.skipped_by_circuit == 2
    assert healthy.state is AgentHealthState.HEALTHY
    assert healthy.successes == 5


def test_open_circuit_probes_and_recovers_deterministically() -> None:
    highway = AgentHighway()
    bridge = highway.bridge(
        "probe_agent:XAUUSD:M5",
        failure_threshold=3,
        probe_after_invocations=5,
    )
    actual_calls = 0

    def flaky():
        nonlocal actual_calls
        actual_calls += 1
        if actual_calls <= 3:
            raise ValueError("temporary")
        return "recovered"

    # Invocations 1-3 fail. Circuit opens through invocation 7.
    for _ in range(3):
        result, value = bridge.call(flaky)
        assert not result.ok
        assert value is None

    for _ in range(4):
        result, value = bridge.call(flaky)
        assert result.skipped is True
        assert value is None

    # Invocation 8 is the deterministic half-open probe.
    result, value = bridge.call(flaky)
    assert result.ok is True
    assert value == "recovered"
    assert actual_calls == 4
    assert highway.health("probe_agent:XAUUSD:M5").state is AgentHealthState.HEALTHY

    health_events = highway.history(topic="agent.health")
    assert health_events
    assert health_events[-1].payload.get("recovered") is True


def test_highway_keeps_latest_message_per_stream() -> None:
    highway = AgentHighway(history_size=10)

    first = highway.publish(
        topic="context.regime",
        source_agent="market_regime",
        symbol="XAUUSD",
        timeframe="M5",
        payload={"regime": "range"},
    )
    second = highway.publish(
        topic="context.regime",
        source_agent="market_regime",
        symbol="XAUUSD",
        timeframe="M5",
        payload={"regime": "trend_expansion"},
    )
    silver = highway.publish(
        topic="context.regime",
        source_agent="market_regime",
        symbol="XAGUSD",
        timeframe="M5",
        payload={"regime": "range"},
    )

    latest_gold = highway.latest("context.regime", symbol="XAUUSD", timeframe="M5")
    latest_silver = highway.latest("context.regime", symbol="XAGUSD", timeframe="M5")

    assert latest_gold is not None and latest_gold.sequence == second.sequence
    assert latest_gold.payload["regime"] == "trend_expansion"
    assert latest_silver is not None and latest_silver.sequence == silver.sequence
    assert first.sequence < second.sequence < silver.sequence


def test_disabled_bridge_is_skipped_without_calling_agent() -> None:
    highway = AgentHighway()
    bridge = highway.bridge("disabled_agent:XAUUSD:M5")
    bridge.disable()
    called = False

    def should_not_run():
        nonlocal called
        called = True
        return 1

    result, value = bridge.call(should_not_run)

    assert result.skipped is True
    assert value is None
    assert called is False
    assert result.health.state is AgentHealthState.DISABLED
