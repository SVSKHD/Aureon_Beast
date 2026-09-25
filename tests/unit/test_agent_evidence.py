"""Normalized agent evidence is complete, finite and survives local SQLite."""

from __future__ import annotations

import math

from aureon.agents.breakout_agent import BreakoutAgent
from aureon.agents.liquidity_agent import LiquidityAgent
from aureon.agents.rsi_agent import RsiAgent
from aureon.agents.session_trend_agent import SessionTrendAgent
from aureon.agents.wick_agent import WickAgent
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.engine.levels import LevelTracker
from aureon.storage.local_database import LocalDatabase
from aureon.storage.postgres.repositories.detections import DetectionRepository
from tests.conftest import ACCOUNT_SCOPE, MARKET_TZ, cross_agent


def _detections(agent, candles):
    engine = AnalysisEngine(
        [agent],
        account_scope=ACCOUNT_SCOPE,
        market_tz=MARKET_TZ,
    )
    return engine.feed(candles)


def test_all_six_agents_emit_normalized_evidence(candles) -> None:
    tracker = LevelTracker()
    agents = (
        cross_agent(),
        RsiAgent(),
        SessionTrendAgent(),
        WickAgent(),
        LiquidityAgent(level_tracker=tracker),
        BreakoutAgent(level_tracker=tracker),
    )

    for agent in agents:
        detections = _detections(agent, candles)
        assert detections, f"{agent.agent_name} produced no fixture detections"
        for detection in detections:
            evidence = detection.evidence
            assert evidence.schema_version == 1
            assert evidence.numeric, detection.agent_name
            assert evidence.categorical, detection.agent_name
            assert evidence.flags, detection.agent_name
            assert all(math.isfinite(value) for value in evidence.numeric.values())


def test_ema_evidence_contains_training_ready_cross_shape(candles) -> None:
    detection = _detections(cross_agent(), candles)[0]
    evidence = detection.evidence

    assert evidence.categorical["cross_direction"] in {"bullish", "bearish"}
    assert evidence.categorical["ema_relation"] in {"fast_above", "fast_below"}
    for key in (
        "ema_fast",
        "ema_slow",
        "ema_gap",
        "previous_ema_gap",
        "ema_gap_change",
        "fast_slope",
        "slow_slope",
    ):
        assert key in evidence.numeric
    assert "gap_expanding" in evidence.flags


def test_normalized_evidence_round_trips_through_local_sqlite(tmp_path, candles) -> None:
    detection = _detections(cross_agent(), candles)[0]
    database = LocalDatabase(tmp_path / "aureon.db")
    database.ensure_schema()
    repository = DetectionRepository(database)

    repository.upsert(detection)
    loaded = repository.get(detection.detection_id)

    assert loaded is not None
    assert loaded.evidence == detection.evidence
    assert loaded.model_dump(mode="json") == detection.model_dump(mode="json")
    database.dispose()
