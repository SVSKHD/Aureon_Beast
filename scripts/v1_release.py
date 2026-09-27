#!/usr/bin/env python3
"""Review real-run evidence and report the eleven V1 gates. Never runs broker commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aureon.services.backup_service import _atomic_write  # noqa: E402
from aureon.services.v1_release import REQUIREMENTS, review_gate, validate_ledger  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("requirements")
    status = sub.add_parser("status")
    status.add_argument("--evidence", type=Path, required=True)
    review = sub.add_parser("review", help="attest only after inspecting raw real-run evidence")
    review.add_argument("--evidence", type=Path, required=True)
    review.add_argument("--gate", choices=tuple(REQUIREMENTS), required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--notes", required=True)
    review.add_argument(
        "--artifact",
        action="append",
        required=True,
        help="path relative to ledger directory; repeatable",
    )
    review.add_argument(
        "--check",
        action="append",
        required=True,
        help="explicitly attest one acceptance check; repeat for each",
    )
    args = parser.parse_args(argv)
    if args.command == "requirements":
        print(
            json.dumps(
                {k: {"source": s, "checks": c} for k, (s, c) in REQUIREMENTS.items()}, indent=2
            )
        )
        return 0
    try:
        report = validate_ledger(args.evidence)
        if args.command == "review":
            review_gate(
                report["ledger"],
                root=args.evidence.parent,
                gate=args.gate,
                reviewer=args.reviewer,
                notes=args.notes,
                artifacts=args.artifact,
                checks=args.check,
            )
            _atomic_write(args.evidence, json.dumps(report["ledger"], indent=2, sort_keys=True))
            print(f"recorded operator review: {args.gate}; not automatic market verification")
            return 0
        print(json.dumps({k: v for k, v in report.items() if k != "ledger"}, indent=2))
        return 0 if report["ready_for_freeze"] else 2
    except (ValueError, OSError) as exc:
        print(f"evidence refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
