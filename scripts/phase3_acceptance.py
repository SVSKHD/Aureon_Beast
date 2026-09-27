#!/usr/bin/env python3
"""Build the Phase-3 production-proof report from recorded drill/lifecycle evidence.

Example:
  python scripts/phase3_acceptance.py --evidence phase3_evidence.json --output phase3_report.json

The input is deliberately explicit: real MT5/network/manual observations cannot be
manufactured by a CI test. This command validates that the complete evidence set exists.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aureon.services.phase3_acceptance import build_phase3_acceptance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    report = build_phase3_acceptance(
        automated_drills=evidence.get("automated_drills", []),
        mt5_drills=evidence.get("mt5_drills", []),
        manual_drills=evidence.get("manual_drills", {}),
        lifecycle=evidence.get("lifecycle", {}),
        symbols=evidence.get("symbols", []),
        launcher_ok=bool(evidence.get("launcher_ok")),
        demo_account=bool(evidence.get("demo_account")),
        environment=str(evidence.get("environment", "")),
    )
    for name, check in report["checks"].items():
        print(f"{'PASS' if check['passed'] else 'FAIL'} {name}")
    print(f"PHASE-3 {'PASS' if report['passed'] else 'FAIL'}")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.output}")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
