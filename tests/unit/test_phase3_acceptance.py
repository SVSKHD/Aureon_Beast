"""Phase-3 production acceptance tests."""

from aureon.services.phase3_acceptance import (
    REQUIRED_LIFECYCLE,
    REQUIRED_MANUAL_DRILLS,
    build_phase3_acceptance,
)


def _rows(numbers, status="PASS"):
    return [{"number": n, "status": status} for n in numbers]


def test_phase3_passes_only_with_complete_real_evidence():
    report = build_phase3_acceptance(
        automated_drills=_rows(range(1, 10)),
        mt5_drills=_rows([1, 2, 3, 4, 6, 9]),
        manual_drills={name: True for name in REQUIRED_MANUAL_DRILLS},
        lifecycle={name: True for name in REQUIRED_LIFECYCLE},
        symbols=["XAUUSD", "XAGUSD"],
        launcher_ok=True,
        demo_account=True,
        environment="windows/vps",
    )
    assert report["passed"] is True


def test_skip_or_missing_manual_evidence_never_counts_as_pass():
    report = build_phase3_acceptance(
        automated_drills=_rows(range(1, 10)),
        mt5_drills=_rows([1, 2, 3, 4, 6]) + [{"number": 9, "status": "SKIP"}],
        manual_drills={"spread_widening": True},
        lifecycle={name: True for name in REQUIRED_LIFECYCLE},
        symbols=["XAUUSD", "XAGUSD"],
        launcher_ok=True,
        demo_account=True,
        environment="windows",
    )
    assert report["passed"] is False
    assert report["checks"]["real_mt5_demo_drills"]["passed"] is False
    assert report["checks"]["manual_real_failure_recovery_drills"]["passed"] is False


def test_fake_proof_cannot_substitute_for_demo_account():
    report = build_phase3_acceptance(
        automated_drills=_rows(range(1, 10)),
        mt5_drills=_rows([1, 2, 3, 4, 6, 9]),
        manual_drills={name: True for name in REQUIRED_MANUAL_DRILLS},
        lifecycle={name: True for name in REQUIRED_LIFECYCLE},
        symbols=["XAUUSD", "XAGUSD"],
        launcher_ok=True,
        demo_account=False,
        environment="windows",
    )
    assert report["passed"] is False
    assert report["checks"]["real_mt5_demo_drills"]["passed"] is False
