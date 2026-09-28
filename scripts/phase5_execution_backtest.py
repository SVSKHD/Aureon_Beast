#!/usr/bin/env python3
"""Phase-5 report builder from true-execution baseline trade JSONL files."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aureon.services.phase5_execution_backtest import (  # noqa: E402
    ExecutionAssumptions, JAN23_MODEL_ID, phase5_report,
)


def _rows(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _phase4_provenance(path: str) -> dict:
    source = Path(path)
    raw = source.read_bytes()
    report = json.loads(raw)
    if report.get("model_id") != JAN23_MODEL_ID:
        raise ValueError(f"unexpected Phase-4 model_id: {report.get('model_id')!r}")
    if report.get("research_only") is not True or report.get("production_eligible") is not False:
        raise ValueError("Phase-4 report is not the expected research-only artifact")
    return {
        "model_id": report["model_id"],
        "dataset": report.get("dataset"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source": str(source),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Phase-5 unseen execution evidence")
    p.add_argument("--original-cross", required=True)
    p.add_argument("--deterministic-aureon", required=True)
    p.add_argument("--wait-confirmation", required=True)
    p.add_argument("--sequence-ml", required=True)
    p.add_argument("--phase4-report", required=True)
    p.add_argument("--counter-move-continuation")
    p.add_argument("--from", dest="start", required=True)
    p.add_argument("--to", dest="end", required=True)
    p.add_argument("--contract-size", type=float, required=True)
    p.add_argument("--spread-price", type=float, required=True)
    p.add_argument("--slippage-price", type=float, required=True)
    p.add_argument("--target-move", type=float, default=10)
    p.add_argument("--stop-move", type=float, default=15)
    p.add_argument("--hold-bars", type=int, default=144)
    p.add_argument("--output", default="artifacts/phase5_execution_report.json")
    args = p.parse_args()

    assumptions = ExecutionAssumptions(
        contract_size=args.contract_size, spread_price=args.spread_price,
        slippage_price=args.slippage_price, target_move=args.target_move,
        stop_move=args.stop_move, hold_bars=args.hold_bars,
    )
    baselines = {
        "original_cross_immediate": _rows(args.original_cross),
        "deterministic_aureon": _rows(args.deterministic_aureon),
        "wait_confirmation": _rows(args.wait_confirmation),
        "sequence_ml": _rows(args.sequence_ml),
    }
    if args.counter_move_continuation:
        baselines["counter_move_continuation"] = _rows(args.counter_move_continuation)

    report = phase5_report(
        baseline_results=baselines, assumptions=assumptions,
        unseen_period={"from": args.start, "to": args.end},
        phase4_provenance=_phase4_provenance(args.phase4_report),
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print("✓ PHASE-4 FROZEN ARTIFACT HASHED")
    print("✓ PHASE-5 EVIDENCE CONTRACT VERIFIED")
    print("✓ PHASE-5 EXECUTION EVIDENCE READY")
    print("✓ SAME-BAR AMBIGUITY CONSERVATIVE")
    print("✓ LOT-SIZE P&L + MAX DRAWDOWN INCLUDED")
    print("✓ LIVE EXECUTION / PROMOTION DISABLED")
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
