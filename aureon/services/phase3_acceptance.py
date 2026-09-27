"""Aureon V1 Phase-3 production workflow acceptance.

Phase 3 is intentionally evidence-driven. Unit/fake-broker drills prove deterministic
failure handling; MT5 evidence proves the adapter against a real DEMO terminal; lifecycle
evidence proves one Aureon-owned trade made it from confirmation to broker close and
durable trade history. SKIP is never PASS.
"""

from __future__ import annotations

from typing import Any

PHASE3_REPORT_SCHEMA = "AUREON_PHASE3_PRODUCTION_PROOF_V1"

REQUIRED_AUTOMATED_DRILLS = set(range(1, 10))
REQUIRED_MT5_DRILLS = {1, 2, 3, 4, 6, 9}
REQUIRED_MANUAL_DRILLS = {"spread_widening", "unknown_outcome_reconnect", "cancel_fill_race"}
REQUIRED_LIFECYCLE = {
    "agents_decision",
    "champion_prediction",
    "discord_alert_with_chart",
    "human_confirmed",
    "execution_guard_passed",
    "mt5_demo_order",
    "position_monitored",
    "exit_management_observed",
    "broker_close_observed",
    "outcome_persisted",
    "learning_memory_persisted",
}


def _passed_drills(rows: list[dict[str, Any]]) -> set[int]:
    return {
        int(row["number"])
        for row in rows
        if str(row.get("status", "")).upper() == "PASS"
    }


def build_phase3_acceptance(
    *,
    automated_drills: list[dict[str, Any]],
    mt5_drills: list[dict[str, Any]],
    manual_drills: dict[str, bool],
    lifecycle: dict[str, bool],
    symbols: list[str] | tuple[str, ...],
    launcher_ok: bool,
    demo_account: bool,
    environment: str,
) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}

    def add(name: str, passed: bool, **detail: Any) -> None:
        checks[name] = {"passed": bool(passed), **detail}

    auto = _passed_drills(automated_drills)
    real = _passed_drills(mt5_drills)
    add(
        "all_automated_failure_drills",
        REQUIRED_AUTOMATED_DRILLS <= auto,
        required=sorted(REQUIRED_AUTOMATED_DRILLS),
        passed_drills=sorted(auto),
    )
    add(
        "real_mt5_demo_drills",
        demo_account and REQUIRED_MT5_DRILLS <= real,
        required=sorted(REQUIRED_MT5_DRILLS),
        passed_drills=sorted(real),
        demo_account=demo_account,
    )
    add(
        "manual_real_failure_recovery_drills",
        all(bool(manual_drills.get(name)) for name in REQUIRED_MANUAL_DRILLS),
        required=sorted(REQUIRED_MANUAL_DRILLS),
        evidence={name: bool(manual_drills.get(name)) for name in sorted(REQUIRED_MANUAL_DRILLS)},
    )
    normalized_symbols = {str(s).upper() for s in symbols}
    add(
        "xauusd_and_xagusd_verified",
        {"XAUUSD", "XAGUSD"} <= normalized_symbols,
        symbols=sorted(normalized_symbols),
    )
    add(
        "main_launcher_environment_proof",
        launcher_ok and environment.lower() in {"windows", "vps", "windows/vps"},
        launcher_ok=launcher_ok,
        environment=environment,
    )
    missing_lifecycle = sorted(name for name in REQUIRED_LIFECYCLE if not lifecycle.get(name))
    add(
        "complete_demo_trade_lifecycle",
        not missing_lifecycle,
        required=sorted(REQUIRED_LIFECYCLE),
        missing=missing_lifecycle,
        evidence={name: bool(lifecycle.get(name)) for name in sorted(REQUIRED_LIFECYCLE)},
    )

    return {
        "schema": PHASE3_REPORT_SCHEMA,
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
        "note": (
            "Phase 3 passes only on recorded DEMO evidence. Fake-broker proof cannot "
            "substitute for MT5 proof, and a skipped real/manual drill is not a pass."
        ),
    }
