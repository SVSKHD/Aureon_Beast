from aureon.services.phase4_acceptance import build_phase4_acceptance, compare_v2_to_v1


def test_phase4_requires_every_previous_phase_and_hardening_gate():
    good = {"passed": True, "schema": "proof"}
    report = build_phase4_acceptance(
        phase1=good, phase2=good, phase3=good,
        release_ledger={"ready_for_freeze": True, "problems": []},
        ci_green=True, crash_free=True, restart_recovery=True,
        backup_restore_verified=True, baseline_manifest_verified=True,
    )
    assert report["passed"] is True
    broken = build_phase4_acceptance(
        phase1=good, phase2=good, phase3={"passed": False},
        release_ledger={"ready_for_freeze": True, "problems": []},
        ci_green=True, crash_free=True, restart_recovery=True,
        backup_restore_verified=True, baseline_manifest_verified=True,
    )
    assert broken["passed"] is False


def _metrics():
    return {
        "clean_10_precision": .70, "clean_10_recall": .60, "clean_10_f1": .64,
        "net_move": 100.0, "win_rate": .55, "mfe_captured": .60,
        "clean_10_fpr": .20, "brier": .18, "max_drawdown": 12.0, "given_back": 20.0,
    }


def test_v2_must_beat_v1_without_regression():
    v1 = _metrics()
    v2 = dict(v1)
    v2["clean_10_precision"] = .72
    assert compare_v2_to_v1(v1=v1, v2=v2)["passed"] is True
    v2["max_drawdown"] = 13.0
    result = compare_v2_to_v1(v1=v1, v2=v2)
    assert result["passed"] is False
    assert "max_drawdown" in result["regressions"]


def test_v2_identical_to_v1_does_not_qualify():
    v1 = _metrics()
    assert compare_v2_to_v1(v1=v1, v2=dict(v1))["passed"] is False
