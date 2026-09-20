#!/usr/bin/env python3
"""Run the nine execution drills and record what they showed (P-4).

    python scripts/demo_drills.py --list
    python scripts/demo_drills.py --all --broker fake     # against the emulator, first
    python scripts/demo_drills.py --drill 2 --broker mt5   # on a DEMO account
    python scripts/demo_drills.py --all --broker mt5 --evidence docs/evidence/demo_drills.md

Each drill sets up one dangerous situation, drives the real ``ExecutionWorker`` through it
and states what must be true afterwards -- including, for five of the nine, that **no order
reached the broker at all**. ``aureon/execution/drills.py`` holds the catalogue and the
reasoning; ``docs/DEMO_EXECUTION_CHECKLIST.md`` is the human half.

Exits non-zero if any drill FAILED. A SKIP is not a pass: the summary line says how many
did not run.

## --broker mt5 places real orders

Four drills place real orders and one of them deliberately loses a connection mid-send.
**Use a demo account.** The script refuses to run against a terminal whose account reports
itself as real unless ``--i-know-this-is-real-money`` is passed, which exists so that
refusal cannot be mistaken for an inability to do it rather than a decision not to.

It also needs ``trading_enabled`` to be **true** in ``settings/execution`` to be
meaningful -- drill 1 cannot fill anything with the kill switch on. That is the opposite of
an observation session, which is why this is a different checklist.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.execution.drills import (  # noqa: E402
    DrillContext,
    catalogue_markdown,
    run_drills,
)
from aureon.models.settings import ExecutionSettings  # noqa: E402


def drill_settings(config) -> ExecutionSettings:
    """Generous limits, so a drill blocks on what it is testing and nothing else.

    Deliberately NOT the stored settings: a drill whose verdict depended on whatever
    ``settings/execution`` happened to hold would pass or fail for reasons its output does
    not record. Each drill tightens the one limit it is about.
    """
    return ExecutionSettings(
        trading_enabled=True,
        max_lot=10.0,
        max_spread_points=200.0,
        max_deviation_points=100,
        max_open_positions=100,
        max_daily_trades=1000,
        confirmation_ttl_seconds=300.0,
        quote_ttl_seconds=300.0,
        executor_lease_seconds=60.0,
    )


def build_broker_factory(kind: str, *, config, allow_real: bool):
    """A callable giving each drill its broker.

    A **fresh** FakeBroker per drill, because a drill inherits nothing: drill 5 widens the
    spread and drill 8 leaves a position, and either would decide drill 6's verdict if the
    broker were shared. A real terminal cannot be handed over fresh, which is exactly why
    the three drills that need to make a broker misbehave SKIP against one.
    """
    if kind == "fake":
        from aureon.execution.fake_broker import FakeBroker

        return FakeBroker

    from aureon.execution.mt5_broker import MT5Broker

    broker = MT5Broker(
        market_tz=config.market_tz,
        login=config.mt5_login,
        password=config.mt5_password,
        server=config.mt5_server,
        terminal_path=config.mt5_terminal_path,
    )
    broker.connect()
    account = broker.account_info()
    demo = bool(getattr(account, "is_demo", True))
    if not demo and not allow_real:
        raise SystemExit(
            "this terminal reports a REAL account. Four of these drills place real "
            "orders and one loses a connection mid-send. Use a demo account, or pass "
            "--i-know-this-is-real-money."
        )
    # One terminal, handed to every drill: there is no second one, and the drills that
    # would need it to misbehave skip rather than weaken what they assert.
    return lambda: broker


def main(argv: list[str] | None = None, *, context_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drill", type=int, action="append", help="run one; repeatable")
    parser.add_argument("--all", action="store_true", help="run the whole catalogue")
    parser.add_argument("--list", action="store_true", help="print the catalogue and stop")
    parser.add_argument("--broker", choices=("fake", "mt5"), default="fake")
    parser.add_argument("--evidence", type=Path, default=None, help="write a markdown record")
    parser.add_argument(
        "--i-know-this-is-real-money",
        action="store_true",
        dest="allow_real",
        help="permit a non-demo account (do not)",
    )
    args = parser.parse_args(argv)

    if args.list:
        print("\n".join(catalogue_markdown()))
        return 0
    if not args.all and not args.drill:
        parser.error("pass --all, or --drill N (repeatable), or --list")

    from aureon.config import AureonConfig

    config = AureonConfig.from_env()

    if context_factory is None:
        broker_factory = build_broker_factory(
            args.broker, config=config, allow_real=args.allow_real
        )
        from aureon.storage.firebase_service import get_client

        client = get_client(
            project_id=config.firebase_project_id,
            emulator_host=config.firestore_emulator_host,
        )
        settings = drill_settings(config)

        def context_factory() -> DrillContext:
            return DrillContext(
                client=client,
                broker=broker_factory(),
                settings=settings,
                magic=config.aureon_magic,
                symbol=config.symbols[0],
            )

    report, runs = run_drills(
        context_factory, numbers=None if args.all else sorted(set(args.drill))
    )
    print(report.render(ready="DRILLS PASSED", not_ready="DRILLS FAILED"))
    print()
    for run in runs:
        print(f"── {run.drill.number}. {run.drill.name} ({run.result.status.value})")
        print(f"   proves: {run.drill.proves}")
        print(f"   invariant: {run.drill.invariant}")
        for line in run.observations:
            print(f"   {line}")
        print()

    if args.evidence is not None:
        write_evidence(args.evidence, report, runs, broker=args.broker, config=config)
        print(f"wrote {args.evidence}")
    return report.exit_code


def write_evidence(path: Path, report, runs, *, broker: str, config) -> None:
    """A markdown record of one drill run, for the phase gate.

    Its own file rather than a session evidence file: a drill run is not a session, it has
    no market date, and filing it as one would put an execution rehearsal under a heading
    that means "we watched the market on this day".
    """
    from aureon.models.base import utc_now
    from aureon.services.session_evidence import git_commit, git_dirty
    from aureon.storage import paths

    commit = git_commit(cwd=REPO_ROOT)
    dirty = git_dirty(cwd=REPO_ROOT)
    lines = [
        "# Demo execution drills",
        "",
        "**Generated** by `python scripts/demo_drills.py`. Do not edit by hand.",
        "",
        "| property | value |",
        "|---|---|",
        f"| run at | `{utc_now().isoformat()}` |",
        f"| broker | `{broker}` |",
        f"| symbol | {config.symbols[0]} |",
        f"| collection prefix | `{paths.PREFIX}` |",
        f"| commit | `{commit or '—'}`"
        + (" **+ uncommitted changes**" if dirty else "")
        + " |",
        "",
        report.summary(ready="DRILLS PASSED", not_ready="DRILLS FAILED"),
        "",
    ]
    lines += report.markdown()
    lines.append("")
    for run in runs:
        lines += [
            f"## {run.drill.number}. `{run.drill.name}` — {run.result.status.value}",
            "",
            f"**Proves:** {run.drill.proves}",
            "",
            f"**Invariant:** {run.drill.invariant}",
            "",
        ]
        if run.observations:
            lines += ["```", *run.observations, "```", ""]
        else:
            lines += [f"_{run.result.detail}_", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
