#!/usr/bin/env python3
"""Resolve Phase 5 to YES or NO + exact failed gates from reviewed evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aureon.services.phase5_certification import build_phase5_certification


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.evidence.read_text(encoding="utf-8"))
    report = build_phase5_certification(gates=payload.get("gates", {}))
    print(report["verdict"])
    if report["failed_gates"]:
        print("FAILED GATES:")
        for gate in report["failed_gates"]:
            detail = report["gates"][gate]
            reason = detail.get("reason") or detail.get("problems") or "missing or not passed"
            print(f"- {gate}: {reason}")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return 0 if report["ready_for_freeze"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
