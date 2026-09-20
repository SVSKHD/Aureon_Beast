#!/usr/bin/env python3
"""Verify a finished session and fill in the second half of its evidence (P-3).

    python scripts/session_verify.py 2026-09-16

Six checks against what the session actually left behind -- the candle archive, the
live-vs-replay comparison, the stored detections, the outbox, and the observer's own
heartbeat against its last candle -- written into
``docs/evidence/session_{market_date}.md`` below the ``## After the close`` marker.

Everything **above** that marker is preserved byte for byte. It was written before the
open by ``scripts/session_run.py``, and a verification that could rewrite it could
retroactively improve the record of a session that went wrong.

Exits non-zero if any check FAILED, so it can gate a phase. It is safe to re-run: the
second half is regenerated, which is what you want the day after a session, when
horizons that were PENDING at the close have resolved.

Reads only. It never writes a detection, an evaluation or a setting.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.models.base import utc_now  # noqa: E402
from aureon.models.enums import Timeframe  # noqa: E402
from aureon.services.session_evidence import (  # noqa: E402
    SessionMeta,
    evidence_path,
    git_commit,
    git_dirty,
    orphan_document,
    render_closing,
    replace_after,
)
from aureon.services.session_verifier import SessionVerifier  # noqa: E402
from aureon.storage import paths  # noqa: E402


def main(
    argv: list[str] | None = None,
    *,
    verifier_factory=None,
    evidence_root: Path | None = None,
    now=utc_now,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("market_date", help="broker date of the session, YYYY-MM-DD")
    parser.add_argument("--symbol", default=None, help="default: the first configured")
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--archive-dir", type=Path, default=None)
    parser.add_argument("--outbox", type=Path, default=None)
    parser.add_argument("--evidence-dir", type=Path, default=None)
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="print the verdict without touching the evidence file",
    )
    args = parser.parse_args(argv)

    from aureon.config import AureonConfig

    config = AureonConfig.from_env()
    symbol = args.symbol or config.symbols[0]
    timeframe = Timeframe(args.timeframe) if args.timeframe else config.timeframes[0]

    factory = verifier_factory or (
        lambda: SessionVerifier(
            config,
            market_date=args.market_date,
            symbol=symbol,
            timeframe=timeframe,
            archive_dir=args.archive_dir,
            outbox_path=args.outbox,
        )
    )
    verifier = factory()
    report = verifier.run()
    print(
        report.render(ready="SESSION VERIFIED", not_ready="SESSION NOT VERIFIED")
    )

    if args.no_write:
        return report.exit_code

    root = args.evidence_dir or evidence_root
    path = evidence_path(args.market_date, symbol, root=root)
    closing = render_closing(report, verifier.blocks, verified_at=now())
    if path.exists():
        path.write_text(
            replace_after(path.read_text(encoding="utf-8"), closing), encoding="utf-8"
        )
    else:
        # No pre-session record. The result is still worth keeping, and the document says
        # what it is rather than implying a preflight nobody ran.
        meta = SessionMeta(
            market_date=args.market_date,
            symbol=symbol,
            timeframe=timeframe.value,
            market_tz=config.market_tz,
            account_scope=config.account_scope,
            collection_prefix=paths.PREFIX,
            evaluation_rule_id=config.evaluation_rule_id,
            ema_fast=config.ema_fast,
            ema_slow=config.ema_slow,
            commit=git_commit(cwd=REPO_ROOT),
            dirty=git_dirty(cwd=REPO_ROOT),
        )
        orphan_document(meta).write(path)
        path.write_text(
            replace_after(path.read_text(encoding="utf-8"), closing), encoding="utf-8"
        )
        print(f"\n{path} had no pre-session record; wrote one saying so", file=sys.stderr)

    print(f"\nwrote {path}")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
