from aureon.services.phase5_certification import REQUIRED_GATES, build_phase5_certification


def test_phase5_is_binary_and_fails_closed():
    report = build_phase5_certification(gates={})
    assert report["ready_for_freeze"] is False
    assert report["verdict"] == "AUREON V1 READY FOR FREEZE: NO"
    assert report["failed_gates"] == list(REQUIRED_GATES)


def test_phase5_passes_only_when_every_gate_passes():
    gates = {name: {"passed": True} for name in REQUIRED_GATES}
    report = build_phase5_certification(gates=gates)
    assert report["ready_for_freeze"] is True
    assert report["verdict"] == "AUREON V1 READY FOR FREEZE: YES"
    assert report["failed_gates"] == []


def test_phase5_reports_exact_failed_gate():
    gates = {name: {"passed": True} for name in REQUIRED_GATES}
    gates["mt5_demo_certification"] = {"passed": False, "reason": "reconnect drill failed"}
    report = build_phase5_certification(gates=gates)
    assert report["failed_gates"] == ["mt5_demo_certification"]
