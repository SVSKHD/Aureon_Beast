#!/usr/bin/env python3
"""Bracket one observation session and record what was true going in (P-3).

    python scripts/session_run.py                 # preflight, then run the observer
    python scripts/session_run.py --dry-run       # preflight and write the record only

Three things, in this order:

1. run the preflight (P-2) and **refuse to start on a FAIL**, because a session run on a
   terminal logged into the wrong server costs a market day and produces evidence about
   nothing;
2. write ``docs/evidence/session_{market_date}.md`` with the preflight table, the commit,
   the terminal build and the configuration -- *before* the observer starts, so a session
   that crashes still leaves a record filed under the right date;
3. run the observer until it is stopped, then stamp the stop time into the same file.

``--force`` starts anyway and says so in the document. It exists because a FAIL can be
something an operator has genuinely decided to accept, and a gate with no override is a
gate people work around by not running it -- but the override is recorded, which an
unrun preflight is not.

After the close, ``scripts/session_verify.py {market_date}`` fills in the second half.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.models.base import utc_now  # noqa: E402
from aureon.models.enums import Timeframe  # noqa: E402
from aureon.reviews.periods import market_date_of  # noqa: E402
from aureon.services.checks import CheckReport  # noqa: E402
from aureon.services.preflight import Preflight  # noqa: E402
from aureon.services.session_evidence import (  # noqa: E402
    Block,
    SessionDocument,
    SessionMeta,
    evidence_path,
    git_commit,
    git_dirty,
)
from aureon.storage import paths  # noqa: E402


def build_meta(config, *, market_date: str, symbol: str, timeframe: Timeframe,
               report: CheckReport, started_at: datetime | None) -> SessionMeta:
    """The header. The terminal build and broker server come from preflight's own rows.

    Read out of the check details rather than by connecting a second time: a second
    connection could report a different terminal, and then the document would describe
    one terminal while the session ran on another.
    """
    init, account = report.get("mt5_init"), report.get("mt5_account")
    terminal = init.detail if init and init.status.value == "PASS" else None
    server = None
    if account is not None:
        for part in account.detail.split():
            if part.startswith("server="):
                server = part.removeprefix("server=")
    return SessionMeta(
        market_date=market_date,
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
        terminal=terminal,
        server=server,
        started_at=started_at,
    )


def main(
    argv: list[str] | None = None,
    *,
    preflight_factory=None,
    observer=None,
    evidence_root: Path | None = None,
    now=utc_now,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default=None, help="default: the first configured")
    parser.add_argument("--timeframe", default=None)
    parser.add_argument(
        "--market-date", default=None, help="default: today on the broker clock"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preflight and write the record, but do not start the observer",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="start even if preflight FAILED (recorded in the document)",
    )
    parser.add_argument("--skip-mt5", action="store_true", help="passed to preflight")
    parser.add_argument("--evidence-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    from aureon.config import AureonConfig

    config = AureonConfig.from_env()
    symbol = args.symbol or config.symbols[0]
    timeframe = Timeframe(args.timeframe) if args.timeframe else config.timeframes[0]
    market_date = args.market_date or market_date_of(now(), config.market_tz)
    root = args.evidence_dir or evidence_root

    factory = preflight_factory or (
        lambda: Preflight(config, symbol=symbol, timeframe=timeframe,
                          skip_mt5=args.skip_mt5)
    )
    report = factory().run()
    rendered = report.render()
    print(rendered)

    started_at = None if args.dry_run else now()
    meta = build_meta(
        config,
        market_date=market_date,
        symbol=symbol,
        timeframe=timeframe,
        report=report,
        started_at=started_at,
    )
    blocks = [
        Block(title="Preflight", body=rendered, command="python scripts/preflight.py")
    ]
    note = None
    if not report.ok and args.force:
        note = (
            "**Started with --force over a FAILED preflight.** The failing rows are in "
            "the table below; whatever they say was not true when this session started."
        )
    elif not report.ok:
        note = "**Session not started:** preflight failed and --force was not given."
    elif args.dry_run:
        note = "**Dry run:** the observer was not started."

    path = evidence_path(market_date, root=root)
    SessionDocument(meta=meta, blocks=blocks, note=note).write(path)
    print(f"\nwrote {path}")

    if not report.ok and not args.force:
        print("preflight failed; not starting the observer", file=sys.stderr)
        return report.exit_code
    if args.dry_run:
        return 0

    run_observer = observer or _default_observer
    print(f"\nstarting the observer for {symbol} {timeframe.value} — Ctrl-C to stop\n")
    try:
        code = run_observer()
    finally:
        # Stamped in a finally: a crash is exactly the case where the document matters,
        # and it is the case where nothing else would record when the session ended.
        meta.ended_at = now()
        SessionDocument(meta=meta, blocks=blocks, note=note).write(path)
        print(f"\nupdated {path}")
    print(f"\nnext: python scripts/session_verify.py {market_date}")
    return code


def _default_observer() -> int:
    from main_observer import main as observer_main

    return observer_main()


if __name__ == "__main__":
    raise SystemExit(main())
