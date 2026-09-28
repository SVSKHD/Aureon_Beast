#!/usr/bin/env python3
"""Generate JAN23_RESEARCH Phase-3 curriculum artifacts. No model training."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aureon.models.sequence_v1 import EMASequenceRecord  # noqa: E402
from aureon.services.sequence_curriculum import (  # noqa: E402
    FEATURE_SCHEMA, Phase3SequenceCurriculum,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Phase-3 JAN23 research curriculum")
    parser.add_argument("--input", default="artifacts/phase2_sequences.jsonl")
    parser.add_argument("--output-dir", default="artifacts")
    parser.add_argument("--prefix", default="phase3_jan23")
    args = parser.parse_args()

    source = Path(args.input)
    records = [
        EMASequenceRecord.model_validate_json(line)
        for line in source.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    records.sort(key=lambda row: row.snapshot.timestamp)
    curriculum = Phase3SequenceCurriculum()
    examples, split, report = curriculum.build(records)
    membership = {
        sid: name
        for name, ids in (("train", split.train), ("validation", split.validation), ("test", split.test))
        for sid in ids
    }
    buckets = {"train": [], "validation": [], "test": []}
    for row in examples:
        buckets[membership[row["sequence_id"]]].append(row)

    target_counts = {}
    for target in (6, 10, 20, 30, 40):
        eligible = [r for r in examples if r["labels"].get("mfe") is not None]
        hits = sum(bool(r["labels"][f"reached_{target}"]) for r in eligible)
        target_counts[str(target)] = {
            "eligible": len(eligible), "hits": hits,
            "prevalence": hits / len(eligible) if eligible else None,
        }
    report["movement_target_prevalence"] = target_counts
    report["partition_snapshot_counts"] = {k: len(v) for k, v in buckets.items()}

    feature_names = list(FEATURE_SCHEMA)
    missing = {
        key: sum(row["features"].get(key) is None for row in examples)
        for key in feature_names
    }
    schema = {
        "dataset": "JAN23_RESEARCH", "research_only": True,
        "feature_names": feature_names,
        "label_names": [
            "decision", "setup_type", "direction", "reached_6", "reached_10",
            "reached_20", "reached_30", "reached_40", "mfe", "mae",
            "bars_to_6", "bars_to_10", "bars_to_20", "bars_to_30", "bars_to_40",
        ],
        "future_labels_never_features": True,
    }
    agent_counts = {}
    for row in examples:
        for agent in row["features"].get("evidence_agents") or []:
            agent_counts[agent] = agent_counts.get(agent, 0) + 1
    report["agent_feature_coverage"] = dict(sorted(agent_counts.items()))
    report["feature_coverage"] = {
        key: {"missing": count, "missing_rate": count / len(examples) if examples else None}
        for key, count in missing.items()
    }

    out = Path(args.output_dir)
    _write_jsonl(out / f"{args.prefix}_examples.jsonl", examples)
    for name, rows in buckets.items():
        _write_jsonl(out / f"{args.prefix}_{name}.jsonl", rows)
    (out / f"{args.prefix}_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    (out / f"{args.prefix}_feature_schema.json").write_text(
        json.dumps(schema, indent=2, sort_keys=True), encoding="utf-8"
    )

    gate = report["acceptance_gate"]
    print(f"Phase 3 sequences: {len(records)}")
    print(f"Phase 3 snapshots: {len(examples)}")
    print(f"ANTI-LEAKAGE {gate['anti_leakage']}")
    print(gate["status"])
    print("No model training performed. Phase 4 remains separate.")
    return 0 if gate["status"] == "PHASE_3_CURRICULUM_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
