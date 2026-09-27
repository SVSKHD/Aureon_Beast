"""Items 2 and 12: bias evidence table and the immutable V1 baseline freeze."""

from __future__ import annotations

import json

import pytest

from aureon.services.backup_service import verify_manifest
from aureon.services.foundation_pipeline import bias_evidence_report, render_bias_evidence
from aureon.services.v1_baseline import freeze_v1_baseline
from tests.unit.v1_fixtures import constant_model, examples


def test_bias_evidence_buckets_direction_session_and_bias_with_counts() -> None:
    rows = examples(40, session="london", daily_bias="bullish") + examples(
        8, session="asia", daily_bias="bearish", clean_every=1
    )
    report = bias_evidence_report(rows, min_samples=30)
    groups = report["groups"]
    london = groups["direction|session|daily_bias"]["buy|london|bullish"]
    assert (
        london["sample_count"] == 40
        and london["clean_10_rate"] == 0.5
        and not london["small_sample"]
    )
    asia = groups["direction|session|daily_bias"]["buy|asia|bearish"]
    assert asia["sample_count"] == 8 and asia["small_sample"] is True
    assert groups["daily_bias_alignment"]["aligned"]["sample_count"] == 40
    assert groups["daily_bias_alignment"]["opposed"]["sample_count"] == 8
    text = render_bias_evidence(report)
    assert "buy|london|bullish" in text and "not evidence" in text


def test_baseline_freeze_is_verifiable_and_immutable(storage, tmp_path) -> None:
    storage.models.write_model(constant_model("champ", clean=0.7))
    result = tmp_path / "exits.json"
    result.write_text(json.dumps([{"label": "fixed"}]), encoding="utf-8")
    target = freeze_v1_baseline(
        root=tmp_path / "baselines",
        tag="v1.0.0-test",
        symbol="XAUUSD",
        storage=storage,
        config_snapshot={"symbols": ["XAUUSD"]},
        exit_policy={"activation_move": 5.0},
        thresholds={"enter": 0.55},
        extra_files=[result],
    )
    assert (target / "champion.json").exists() and (target / "results" / "exits.json").exists()
    summary = json.loads((target / "baseline.json").read_text(encoding="utf-8"))
    assert summary["champion_model_id"] == "champ" and summary["schema"] == "AUREON_V1_BASELINE_V1"
    assert verify_manifest(target).ok
    with pytest.raises(FileExistsError):
        freeze_v1_baseline(
            root=tmp_path / "baselines",
            tag="v1.0.0-test",
            symbol="XAUUSD",
            storage=storage,
            config_snapshot={},
            exit_policy={},
            thresholds={},
        )
