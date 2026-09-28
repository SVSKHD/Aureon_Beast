#!/usr/bin/env python3
"""Train/evaluate JAN23_RESEARCH_CHALLENGER after Phase-3 acceptance only."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aureon.services.phase4_jan23_challenger import train_jan23_challenger  # noqa: E402


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase-4 JAN23 research challenger")
    parser.add_argument("--artifacts", default="artifacts")
    parser.add_argument("--prefix", default="phase3_jan23")
    parser.add_argument("--output", default="artifacts/phase4_jan23_challenger_report.json")
    args = parser.parse_args()
    root = Path(args.artifacts)
    report = json.loads((root / f"{args.prefix}_report.json").read_text(encoding="utf-8"))
    train = _load_jsonl(root / f"{args.prefix}_train.jsonl")
    validation = _load_jsonl(root / f"{args.prefix}_validation.jsonl")
    test = _load_jsonl(root / f"{args.prefix}_test.jsonl")

    result = train_jan23_challenger(train, validation, test, phase3_report=report)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print("✓ PHASE-3 GATE VERIFIED")
    print("✓ JAN23_RESEARCH_CHALLENGER TRAINED")
    print("✓ UNTOUCHED TEST PARTITION EVALUATED")
    print("✓ NO CHAMPION/SHADOW/LIVE STATE MODIFIED")
    print(f"Report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
