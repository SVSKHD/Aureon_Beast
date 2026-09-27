"""Release gates reject incomplete, altered, synthetic-labelled and stale evidence.

All evidence here is fabricated ONLY to exercise integrity validation, never a real-run claim.
"""

from __future__ import annotations

import json

import pytest

from aureon.services.backup_service import verify_manifest
from aureon.services.v1_baseline import freeze_v1_baseline
from aureon.services.v1_release import (
    REQUIREMENTS,
    new_ledger,
    release_context,
    review_gate,
    validate_ledger,
)
from tests.unit.v1_fixtures import constant_model


def reviewed_ledger(tmp_path, champion, *, config=None, thresholds=None, exit_policy=None):
    context = release_context(
        commit="a" * 40,
        champion=champion,
        config=config or {},
        thresholds=thresholds or {},
        exit_policy=exit_policy or {},
    )
    ledger = new_ledger(context)
    for name, (_, checks) in REQUIREMENTS.items():
        path = tmp_path / f"{name}.txt"
        path.write_text("TEST ONLY: fabricated integrity-test artifact", encoding="utf-8")
        review_gate(
            ledger,
            root=tmp_path,
            gate=name,
            reviewer="test-only",
            notes="Test assertion, not real evidence",
            artifacts=[path.name],
            checks=list(checks),
        )
    target = tmp_path / "ledger.json"
    target.write_text(json.dumps(ledger), encoding="utf-8")
    return target


def test_new_ledger_has_no_passing_gates(tmp_path):
    path = reviewed_ledger(tmp_path, constant_model("champ"))
    ledger = json.loads(path.read_text())
    path.write_text(json.dumps(new_ledger(ledger["context"])))
    report = validate_ledger(path)
    assert not report["ready_for_freeze"]
    assert len(report["problems"]) == 11


@pytest.mark.parametrize("change", ["tamper", "missing", "synthetic", "skip", "naive", "empty"])
def test_evidence_cannot_pass_with_invalid_artifacts_or_reviews(tmp_path, change):
    path = reviewed_ledger(tmp_path, constant_model("champ"))
    ledger = json.loads(path.read_text())
    gate = ledger["gates"]["demo_execution"]
    artifact = tmp_path / gate["artifacts"][0]["path"]
    if change == "tamper":
        artifact.write_text("different")
    elif change == "missing":
        artifact.unlink()
    elif change == "synthetic":
        gate["source"] = "fake"
    elif change == "skip":
        gate["checks"]["reconnect"] = "SKIP"
    elif change == "naive":
        gate["reviewed_at"] = "2026-01-01T00:00:00"
    else:
        gate["artifacts"] = []
    path.write_text(json.dumps(ledger))
    assert not validate_ledger(path)["ready_for_freeze"]


def test_review_requires_every_check_and_confines_paths(tmp_path):
    ledger = new_ledger({})
    with pytest.raises(ValueError, match="all named"):
        review_gate(
            ledger,
            root=tmp_path,
            gate="xauusd_session",
            reviewer="operator",
            notes="review",
            artifacts=["missing"],
            checks=["session_verified"],
        )
    with pytest.raises(ValueError, match="outside"):
        review_gate(
            ledger,
            root=tmp_path,
            gate="xauusd_session",
            reviewer="operator",
            notes="review",
            artifacts=["../outside"],
            checks=list(REQUIREMENTS["xauusd_session"][1]),
        )


def test_freeze_requires_champion_and_evidence(storage, tmp_path):
    args = dict(
        root=tmp_path / "baselines",
        tag="v1",
        symbol="XAUUSD",
        storage=storage,
        config_snapshot={},
        thresholds={},
        exit_policy={},
    )
    with pytest.raises(ValueError, match="Champion"):
        freeze_v1_baseline(**args)
    storage.models.write_model(constant_model("champ"))
    with pytest.raises(ValueError, match="evidence"):
        freeze_v1_baseline(**args)
    assert not (tmp_path / "baselines").exists()


def test_freeze_is_portable_and_bound_to_release(storage, tmp_path):
    champion = constant_model("champ")
    storage.models.write_model(champion)
    evidence = reviewed_ledger(tmp_path, champion)
    args = dict(
        root=tmp_path / "baselines",
        tag="v1",
        symbol="XAUUSD",
        storage=storage,
        config_snapshot={},
        thresholds={},
        exit_policy={},
        evidence=evidence,
    )
    with pytest.raises(ValueError, match="different"):
        freeze_v1_baseline(**args, commit="b" * 40)
    with pytest.raises(ValueError, match="missing included"):
        freeze_v1_baseline(**args, commit="a" * 40, extra_files=[tmp_path / "missing"])
    with pytest.raises(ValueError, match="unique"):
        freeze_v1_baseline(
            **args, commit="a" * 40, extra_files=[tmp_path / "x", tmp_path / "other" / "x"]
        )
    target = freeze_v1_baseline(**args, commit="a" * 40)
    evidence.unlink()
    for name in REQUIREMENTS:
        (tmp_path / f"{name}.txt").unlink()
    assert verify_manifest(target).ok
    assert validate_ledger(target / "evidence" / "release.json")["ready_for_freeze"]
    with pytest.raises(FileExistsError):
        freeze_v1_baseline(**args, commit="a" * 40)


@pytest.mark.parametrize("tag", ["../escape", "/tmp/escape", ".", "..", "bad/name"])
def test_baseline_rejects_unsafe_tags(storage, tmp_path, tag):
    with pytest.raises(ValueError, match="tag"):
        freeze_v1_baseline(
            root=tmp_path,
            tag=tag,
            symbol="XAUUSD",
            storage=storage,
            config_snapshot={},
            exit_policy={},
            thresholds={},
        )


def test_cli_prepares_real_configuration_and_refuses_pending_freeze(storage, tmp_path, monkeypatch):
    from scripts import freeze_v1_baseline as cli
    from scripts.v1_release import main as release_main

    storage.models.write_model(constant_model("champ"))
    monkeypatch.setattr(cli, "git_commit", lambda **kw: "a" * 40)
    monkeypatch.setattr(cli, "git_dirty", lambda **kw: False)
    monkeypatch.setattr(cli, "build_storage", lambda **kw: storage)
    monkeypatch.setenv("AUREON_SYMBOLS", "XAUUSD,XAGUSD")
    monkeypatch.setenv("AUREON_EVAL_RULES", "XAUUSD:XAU_OUTCOME_V2,XAGUSD:XAG_OUTCOME_V1")
    ledger = tmp_path / "release.json"
    assert cli.main(["--symbol", "XAUUSD", "--tag", "v1", "--prepare-evidence", str(ledger)]) == 0
    assert release_main(["status", "--evidence", str(ledger)]) == 2
    with pytest.raises(ValueError, match="incomplete"):
        cli.main(
            [
                "--symbol",
                "XAUUSD",
                "--tag",
                "v1",
                "--evidence",
                str(ledger),
                "--root",
                str(tmp_path / "baselines"),
            ]
        )
    assert not (tmp_path / "baselines").exists()
