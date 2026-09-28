"""Phase-4 JAN23 research challenger tests."""
import pytest

from aureon.services.phase4_jan23_challenger import train_jan23_challenger


def _row(i, decision, stage="CROSS", direction="BUY"):
    return {
        "sequence_id": f"s{i}", "snapshot_id": f"x{i}", "timestamp": f"2026-01-{i+1:02d}T00:00:00+00:00",
        "stage": stage,
        "features": {"rsi": 40 + i, "session": "london", "agent_states": {"wick": "ready"}},
        "labels": {
            "decision": decision, "setup_type": None, "direction": direction,
            "reached_6": i % 2 == 0, "reached_10": i % 3 == 0,
            "reached_20": False, "reached_30": False, "reached_40": False,
            "mfe": 8.0, "mae": 3.0,
            "bars_to_6": 2 if i % 2 == 0 else None,
            "bars_to_10": 3 if i % 3 == 0 else None,
            "bars_to_20": None, "bars_to_30": None, "bars_to_40": None,
        },
    }


def test_phase4_refuses_unaccepted_phase3() -> None:
    gate = {"acceptance_gate": {"status": "STOP", "anti_leakage": "PASS"}}
    rows = [_row(0, "ENTER")]
    with pytest.raises(ValueError, match="Phase 4 blocked"):
        train_jan23_challenger(rows, rows, rows, phase3_report=gate)


def test_phase4_is_research_only_and_does_not_promote() -> None:
    train = [_row(i, ("ENTER", "WAIT", "INVALIDATE")[i % 3]) for i in range(12)]
    validation = [_row(i + 20, ("ENTER", "WAIT", "INVALIDATE")[i % 3]) for i in range(6)]
    test = [_row(i + 30, ("ENTER", "WAIT", "INVALIDATE")[i % 3]) for i in range(6)]
    gate = {"acceptance_gate": {"status": "PHASE_3_CURRICULUM_PASS", "anti_leakage": "PASS"}}
    result = train_jan23_challenger(train, validation, test, phase3_report=gate)
    assert result["model_id"] == "JAN23_RESEARCH_CHALLENGER"
    assert result["research_only"] is True
    assert result["production_eligible"] is False
    assert result["champion_promotion_allowed"] is False
    assert result["live_execution_allowed"] is False
    assert result["registry_write_allowed"] is False
    assert result["test"]["decision"]["samples"] == len(test)
