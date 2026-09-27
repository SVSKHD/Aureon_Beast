#!/usr/bin/env python3
"""Assemble the final V1 acceptance report and optionally compare V2 with frozen V1."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aureon.services.phase4_acceptance import build_phase4_acceptance, compare_v2_to_v1
from aureon.services.v1_release import validate_ledger


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase1", type=Path, required=True)
    parser.add_argument("--phase2", type=Path, required=True)
    parser.add_argument("--phase3", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--ci-green", action="store_true")
    parser.add_argument("--crash-free", action="store_true")
    parser.add_argument("--restart-recovery", action="store_true")
    parser.add_argument("--backup-restore", action="store_true")
    parser.add_argument("--manifest-verified", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--v1-metrics", type=Path)
    parser.add_argument("--v2-metrics", type=Path)
    args = parser.parse_args()

    ledger = validate_ledger(args.ledger)
    report = build_phase4_acceptance(
        phase1=_read(args.phase1),
        phase2=_read(args.phase2),
        phase3=_read(args.phase3),
        release_ledger=ledger,
        ci_green=args.ci_green,
        crash_free=args.crash_free,
        restart_recovery=args.restart_recovery,
        backup_restore_verified=args.backup_restore,
        baseline_manifest_verified=args.manifest_verified,
    )
    if bool(args.v1_metrics) != bool(args.v2_metrics):
        parser.error("--v1-metrics and --v2-metrics must be supplied together")
    if args.v1_metrics:
        report["v2_comparison"] = compare_v2_to_v1(
            v1=_read(args.v1_metrics), v2=_read(args.v2_metrics)
        )
    for name, row in report["checks"].items():
        print(f"{'PASS' if row['passed'] else 'FAIL'} {name}")
    print(f"PHASE-4 {'PASS' if report['passed'] else 'FAIL'}")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.output}")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
