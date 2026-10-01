"""Aureon V3 definition-of-done acceptance checks (TODO 099)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from aureon.models.enums import NotificationKind
from aureon.models.settings import NotificationSettings
from aureon.services.v3_ema_ops import DriftPolicy, RetrainingPolicy
from aureon.services.v3_target_ladder import target_ladder_for_symbol

ROOT = Path(__file__).resolve().parents[2]


def test_v3_definition_of_done_contract() -> None:
    """Pin the externally meaningful V3 completion rules in one place."""
    settings = NotificationSettings()
    hidden = settings.model_copy(update={"model_confidence_enabled": False})

    assert settings.signal_first_mode is True
    assert settings.model_confidence_enabled is True
    assert hidden.model_confidence_enabled is False
    assert NotificationKind.JOURNEY.value == "journey"
    assert NotificationKind.JOURNEY_OUTCOME.value == "journey_outcome"

    gold = target_ladder_for_symbol("XAUUSD")
    silver = target_ladder_for_symbol("XAGUSD")
    assert gold.targets == (3.0, 5.0, 10.0, 20.0, 30.0, 40.0)
    assert silver.targets == (0.03, 0.05, 0.10, 0.20, 0.30, 0.40)

    drift = DriftPolicy()
    retraining = RetrainingPolicy()
    assert drift.min_samples >= 30
    assert drift.max_brier <= 0.24
    assert retraining.cadence_days == 7
    assert retraining.minimum_new_examples == 30


def test_v2_benchmark_is_frozen_and_not_v3_data() -> None:
    path = ROOT / "docs" / "V2_BENCHMARK_FROZEN.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema"] == "AUREON_V2_FROZEN_BENCHMARK_V1"
    assert payload["status"] == "frozen"
    assert payload["source_rule"] == "XAU_OUTCOME_V2"
    assert payload["dataset"]["history_source"] == "synthetic"
    assert payload["ema_cross_detection"] == {
        "agent_version": "2.3.0",
        "total": 29,
        "bullish": 14,
        "bearish": 15,
        "by_session": {
            "asia": 9,
            "london": 13,
            "new_york": 6,
            "off": 1,
        },
    }
    assert payload["ema_cross_outcomes"]["day_close"]["complete"] == 19
    assert payload["ema_cross_outcomes"]["day_close"]["reach_10"]["count"] == 5


def test_v3_architecture_and_decisions_are_documented() -> None:
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    decisions = (ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")

    assert "Aureon V3 EMA journey intelligence" in architecture
    assert "Candidate -> Challenger -> Shadow -> Champion" in architecture
    assert "model_confidence_enabled" in architecture
    assert "One market move is one journey" in decisions
    assert "Discord follows journeys, not isolated EMA messages" in decisions
    assert "V2 comparison is frozen before V3 certification" in decisions
