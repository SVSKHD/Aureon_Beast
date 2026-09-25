"""Agent 19: cross-venue blueprints and delayed target validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aureon.models.cross_venue import ReplicationState
from aureon.models.enums import Direction
from aureon.models.market import QuoteSnapshot
from aureon.services.cross_venue_replication_agent import CrossVenueReplicationAgent


def _blueprint():
    agent = CrossVenueReplicationAgent()
    observed = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    return agent.create_blueprint(
        source_symbol="XAUUSD",
        target_symbol="GOLD",
        direction=Direction.BUY,
        observed_at=observed,
        source_price=4500.0,
        scenario_signature="reversal|buy|trend_expansion",
        preferred_zone_low=4499.5,
        preferred_zone_high=4501.5,
        invalidation_price=4495.0,
        primary_target_move=10.0,
        expected_delay_ms=500.0,
        max_valid_delay_ms=2000.0,
        max_price_drift=2.0,
        max_spread_points=50.0,
        source_detection_id="det-1",
    )


def test_blueprint_is_deterministic_and_has_no_target_volume() -> None:
    agent = CrossVenueReplicationAgent()
    first = _blueprint()
    second = _blueprint()

    assert first.blueprint_id == second.blueprint_id
    assert first.source_symbol == "XAUUSD"
    assert first.target_symbol == "GOLD"
    assert "volume" not in first.model_dump()


def test_target_is_executable_when_delay_and_price_are_valid() -> None:
    agent = CrossVenueReplicationAgent()
    blueprint = _blueprint()
    target_at = blueprint.source_observed_at + timedelta(milliseconds=650)
    quote = QuoteSnapshot(
        symbol="GOLD",
        bid=4500.2,
        ask=4500.4,
        captured_at=target_at,
        point=0.01,
    )

    decision, sample = agent.validate_target(
        blueprint,
        quote=quote,
        observed_at=target_at,
        market_open=True,
        risk_allowed=True,
    )

    assert decision.state is ReplicationState.EXECUTABLE
    assert decision.actual_delay_ms == 650.0
    assert decision.within_entry_zone is True
    assert sample.latency_ms == 650.0


def test_target_is_skipped_when_it_arrives_too_late() -> None:
    agent = CrossVenueReplicationAgent()
    blueprint = _blueprint()
    target_at = blueprint.source_observed_at + timedelta(milliseconds=2501)
    quote = QuoteSnapshot(
        symbol="GOLD",
        bid=4500.0,
        ask=4500.2,
        captured_at=target_at,
        point=0.01,
    )

    decision, _ = agent.validate_target(
        blueprint,
        quote=quote,
        observed_at=target_at,
        market_open=True,
        risk_allowed=True,
    )

    assert decision.state is ReplicationState.SKIP_LATE


def test_target_is_skipped_instead_of_chased_after_price_runs() -> None:
    agent = CrossVenueReplicationAgent()
    blueprint = _blueprint()
    target_at = blueprint.source_observed_at + timedelta(milliseconds=400)
    quote = QuoteSnapshot(
        symbol="GOLD",
        bid=4504.0,
        ask=4504.2,
        captured_at=target_at,
        point=0.01,
    )

    decision, _ = agent.validate_target(
        blueprint,
        quote=quote,
        observed_at=target_at,
        market_open=True,
        risk_allowed=True,
    )

    assert decision.state is ReplicationState.SKIP_MOVED


def test_invalidation_beats_replication() -> None:
    agent = CrossVenueReplicationAgent()
    blueprint = _blueprint()
    target_at = blueprint.source_observed_at + timedelta(milliseconds=300)
    quote = QuoteSnapshot(
        symbol="GOLD",
        bid=4494.8,
        ask=4494.9,
        captured_at=target_at,
        point=0.01,
    )

    decision, _ = agent.validate_target(
        blueprint,
        quote=quote,
        observed_at=target_at,
        market_open=True,
        risk_allowed=True,
    )

    assert decision.state is ReplicationState.SKIP_INVALIDATED


def test_target_risk_gate_can_block_replication() -> None:
    agent = CrossVenueReplicationAgent()
    blueprint = _blueprint()
    target_at = blueprint.source_observed_at + timedelta(milliseconds=300)
    quote = QuoteSnapshot(
        symbol="GOLD",
        bid=4500.0,
        ask=4500.2,
        captured_at=target_at,
        point=0.01,
    )

    decision, _ = agent.validate_target(
        blueprint,
        quote=quote,
        observed_at=target_at,
        market_open=True,
        risk_allowed=False,
    )

    assert decision.state is ReplicationState.SKIP_RISK
