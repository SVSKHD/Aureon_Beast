#!/usr/bin/env python3
"""Check everything that must be true before a session starts (P-2).

    python scripts/preflight.py                 # before a live session
    python scripts/preflight.py --skip-mt5      # everything but the terminal

Exits **non-zero if any check FAILED**, so it can gate a session script or a CI job. A
WARN does not fail it; a SKIP does not pass it, and the summary line says how many were
not run rather than leaving a short table to look green.

The checks, and what each one is really protecting against, are documented in
``aureon/services/preflight.py``. Everything here is argument parsing and printing.

It writes two documents and nothing else: ``heartbeats/preflight`` (the Firestore round
trip it is testing) and ``symbol_specs/{symbol}`` (the same publish the observer makes
seconds later). It never places an order and never touches `trading_enabled` -- that one
it prints, because whether it should be on depends on which session this is.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.models.enums import Timeframe  # noqa: E402
from aureon.services.preflight import Preflight  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default=None, help="default: the first configured")
    parser.add_argument("--timeframe", default=None, help="default: the first configured")
    parser.add_argument(
        "--skip-mt5",
        action="store_true",
        help="skip every terminal check (they are reported as SKIP, never as passing)",
    )
    parser.add_argument("--archive-dir", type=Path, default=None)
    parser.add_argument("--outbox", type=Path, default=None)
    parser.add_argument(
        "--drift-tolerance",
        type=float,
        default=None,
        help="seconds of clock drift to accept (default 2)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the results as JSON for an evidence file"
    )
    args = parser.parse_args(argv)

    from aureon.config import AureonConfig

    config = AureonConfig.from_env()
    kwargs = {}
    if args.drift_tolerance is not None:
        kwargs["drift_tolerance_seconds"] = args.drift_tolerance

    report = Preflight(
        config,
        symbol=args.symbol,
        timeframe=Timeframe(args.timeframe) if args.timeframe else None,
        skip_mt5=args.skip_mt5,
        archive_dir=args.archive_dir,
        outbox_path=args.outbox,
        **kwargs,
    ).run()

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "check": r.name,
                        "status": r.status.value,
                        "detail": r.detail,
                        "remedy": r.remedy,
                    }
                    for r in report.results
                ],
                indent=2,
            )
        )
    else:
        print(report.render())
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
