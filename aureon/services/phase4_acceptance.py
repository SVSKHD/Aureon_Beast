"""Final Aureon V1 release acceptance and post-V1 regression contract."""

from __future__ import annotations

from typing import Any

PHASE4_REPORT_SCHEMA = "AUREON_PHASE4_FINAL_ACCEPTANCE_V1"
V2_COMPARISON_SCHEMA = "AUREON_V2_VS_V1_COMPARISON_V1"

# Metrics where higher is better. V2 must not regress any required metric and must
# improve at least one. Risk metrics are handled separately because lower is better.
HIGHER_IS_BETTER = (
    "clean_10_precision",
    "clean_10_recall",
    "clean_10_f1",
    "net_move",
    "win_rate",
    "mfe_captured",
)
LOWER_IS_BETTER = ("clean_10_fpr", "brier", "max_drawdown", "given_back")


def build_phase4_acceptance(
    *,
    phase1: dict[str, Any],
    phase2: dict[str, Any],
    phase3: dict[str, Any],
    release_ledger: dict[str, Any],
    ci_green: bool,
    crash_free: bool,
    restart_recovery: bool,
    backup_restore_verified: bool,
    baseline_manifest_verified: bool,
) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}

    def add(name: str, passed: bool, **detail: Any) -> None:
        checks[name] = {"passed": bool(passed), **detail}

    add("phase1_historical_model_proof", phase1.get("passed") is True, schema=phase1.get("schema"))
    add("phase2_unseen_exit_proof", phase2.get("passed") is True, schema=phase2.get("schema"))
    add("phase3_production_demo_proof", phase3.get("passed") is True, schema=phase3.get("schema"))
    add(
        "reviewed_release_ledger",
        release_ledger.get("ready_for_freeze") is True,
        problems=list(release_ledger.get("problems") or []),
    )
    add("ci_green", ci_green)
    add("crash_free_hardening", crash_free)
    add("restart_recovery_verified", restart_recovery)
    add("backup_restore_verified", backup_restore_verified)
    add("baseline_manifest_verified", baseline_manifest_verified)

    return {
        "schema": PHASE4_REPORT_SCHEMA,
        "passed": all(row["passed"] for row in checks.values()),
        "checks": checks,
        "release_rule": (
            "Freeze/tag V1 only after every check passes. A code-complete phase without "
            "real evidence remains incomplete."
        ),
    }


def compare_v2_to_v1(
    *,
    v1: dict[str, float],
    v2: dict[str, float],
    min_improvement: float = 0.0,
) -> dict[str, Any]:
    """Fail closed: V2 cannot replace V1 if a required metric regresses.

    All available required metrics must be present on both sides. V2 must be no worse on
    every metric and materially better than V1 on at least one metric.
    """
    required = set(HIGHER_IS_BETTER) | set(LOWER_IS_BETTER)
    missing = sorted(k for k in required if k not in v1 or k not in v2)
    comparisons: dict[str, dict[str, Any]] = {}
    improvements = 0
    regressions: list[str] = []

    if not missing:
        for metric in HIGHER_IS_BETTER:
            before, after = float(v1[metric]), float(v2[metric])
            delta = after - before
            ok = delta >= 0
            improved = delta > min_improvement
            improvements += int(improved)
            if not ok:
                regressions.append(metric)
            comparisons[metric] = {
                "v1": before, "v2": after, "delta": delta,
                "passed": ok, "improved": improved, "direction": "higher",
            }
        for metric in LOWER_IS_BETTER:
            before, after = float(v1[metric]), float(v2[metric])
            delta = before - after
            ok = delta >= 0
            improved = delta > min_improvement
            improvements += int(improved)
            if not ok:
                regressions.append(metric)
            comparisons[metric] = {
                "v1": before, "v2": after, "improvement": delta,
                "passed": ok, "improved": improved, "direction": "lower",
            }

    return {
        "schema": V2_COMPARISON_SCHEMA,
        "passed": not missing and not regressions and improvements > 0,
        "missing": missing,
        "regressions": regressions,
        "improved_metrics": improvements,
        "comparisons": comparisons,
        "rule": "V2 must be no worse on every required V1 metric and improve at least one.",
    }
