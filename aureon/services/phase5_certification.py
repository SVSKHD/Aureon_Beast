"""Phase 5: certification-only V1 gate.

No architecture is decided here. This module composes already-produced real evidence into
one binary release decision and names every failed gate. A missing artifact is a failure.
"""

from __future__ import annotations

from typing import Any

PHASE5_SCHEMA = "AUREON_V1_FINAL_CERTIFICATION_V1"

REQUIRED_GATES = (
    "historical_foundation",
    "unseen_month_learning",
    "exit_policy_calibration",
    "mt5_demo_certification",
    "production_lifecycle",
    "windows_vps_certification",
    "live_replay_agent_calibration",
    "final_hardening",
    "acceptance_evidence",
    "freeze_preconditions",
)


def build_phase5_certification(*, gates: dict[str, Any]) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    for name in REQUIRED_GATES:
        raw = gates.get(name)
        if isinstance(raw, dict):
            passed = raw.get("passed") is True
            detail = raw
        else:
            passed = raw is True
            detail = {"passed": passed}
        rows[name] = {"passed": passed, **{k: v for k, v in detail.items() if k != "passed"}}
        if not passed:
            failed.append(name)
    return {
        "schema": PHASE5_SCHEMA,
        "ready_for_freeze": not failed,
        "verdict": (
            "AUREON V1 READY FOR FREEZE: YES"
            if not failed
            else "AUREON V1 READY FOR FREEZE: NO"
        ),
        "failed_gates": failed,
        "gates": rows,
        "rule": "No new architecture. Fix only failed certification gates, then rerun.",
    }
