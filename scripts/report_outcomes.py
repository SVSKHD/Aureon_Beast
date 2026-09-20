#!/usr/bin/env python3
"""Outcomes per agent and per session, for a rule and a date range (P-1).

    python scripts/report_outcomes.py --rule XAU_OUTCOME_V2 --from 2026-09-14 --to 2026-09-18

Reads **stored** detections and evaluations from Firestore, so the same command that
produced the baseline section works on a real week later without a flag change. The
dates are **broker** dates on the market clock and the range is inclusive of both ends
(internally a half-open ``[from 00:00, to+1d 00:00)`` window, so consecutive ranges
tile without double counting). 23:30 UTC is already the next day in Athens, so a
UTC-bounded range would file a New York session under the wrong day.

``--replay FIXTURE`` computes the same report from a CSV replay instead. That is how
the baseline section is generated, and it is the only mode that works without a
Firestore client -- but it reports what the engine WOULD produce, not what was stored.
For a real session those two agreeing is the thing
``scripts/compare_live_vs_replay.py`` exists to check.

Reads only. There is no ``--write``: this aggregates evaluations, it does not create
them (``scripts/backfill_evaluations.py --write`` does that).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.evaluation.outcome_report import (  # noqa: E402
    aggregate,
    render_markdown,
    render_text,
)
from aureon.evaluation.rules import get_rule  # noqa: E402
from aureon.reviews.periods import day_period, previous_market_date  # noqa: E402

#: Default range when none is given: the last full trading week ending yesterday.
DEFAULT_DAYS = 7
POINT = 0.01


def parse_range(
    start: str | None, end: str | None, *, market_tz: str, now=None
) -> tuple[date, date]:
    """Resolve ``--from``/``--to`` to broker dates, defaulting to the last week.

    Both ends inclusive, and a single ``--from`` means that one day rather than
    everything since -- an open-ended range against a production collection is how a
    report accidentally reads a year of data.
    """
    from aureon.models.base import utc_now

    if end is None and start is None:
        last = date.fromisoformat(previous_market_date(market_tz, now=now or utc_now()))
        return last - timedelta(days=DEFAULT_DAYS - 1), last
    if start is None:
        start = end
    if end is None:
        end = start
    first, last = date.fromisoformat(start), date.fromisoformat(end)  # type: ignore[arg-type]
    if last < first:
        raise ValueError(f"--to {last} is before --from {first}")
    return first, last


def _replay(fixture: Path, rule, *, market_tz: str, account_scope: str, point: float):
    """Detections and evaluations from a CSV replay, for the baseline and for tests."""
    from aureon.config import AureonConfig
    from aureon.data.historical_provider import HistoricalDataProvider
    from aureon.evaluation.backfill import run_backfill
    from scripts.backfill_evaluations import build_agents

    config = AureonConfig.from_env()
    candles = HistoricalDataProvider(fixture, market_tz=market_tz).candles
    result = run_backfill(
        candles,
        build_agents(
            cross_only=False, ema_fast=config.ema_fast, ema_slow=config.ema_slow
        ),
        rule,
        account_scope=account_scope,
        market_tz=market_tz,
        point=point,
    )
    return result.detections, result.evaluations


def _stored(start, end, rule, *, account_scope: str):
    """Detections and evaluations read from Firestore for a UTC window.

    Through ``PeriodReader``, which is read-only by construction: this script cannot
    write a detection or an evaluation even by mistake.
    """
    from aureon.config import AureonConfig
    from aureon.storage.firebase_service import get_client
    from aureon.storage.period_reader import PeriodReader

    config = AureonConfig.from_env()
    client = get_client(
        project_id=config.firebase_project_id,
        emulator_host=config.firestore_emulator_host,
    )
    reader = PeriodReader(client, account_scope=account_scope)
    detections = reader.detections_in(start, end)
    evaluations = reader.evaluations_for(detections, rule.rule_id)
    return detections, evaluations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rule", default=None, help="rule id; default: configured")
    parser.add_argument("--from", dest="start", default=None, help="broker date, inclusive")
    parser.add_argument("--to", dest="end", default=None, help="broker date, inclusive")
    parser.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="report a CSV replay instead of stored data",
    )
    parser.add_argument(
        "--agents",
        default=None,
        help="comma-separated agent names; default: every agent present",
    )
    parser.add_argument(
        "--point", type=float, default=POINT, help="symbol point (default 0.01)"
    )
    parser.add_argument("--markdown", action="store_true", help="emit markdown tables")
    args = parser.parse_args(argv)

    from aureon.config import AureonConfig

    config = AureonConfig.from_env()
    rule = get_rule(args.rule or config.evaluation_rule_id)
    market_tz = config.market_tz
    account_scope = config.account_scope

    try:
        first, last = parse_range(args.start, args.end, market_tz=market_tz)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    window_start = day_period(first, market_tz).start
    window_end = day_period(last, market_tz).end

    if args.replay is not None:
        if not args.replay.exists():
            print(f"{args.replay} does not exist", file=sys.stderr)
            return 2
        detections, evaluations = _replay(
            args.replay,
            rule,
            market_tz=market_tz,
            account_scope=account_scope,
            point=args.point,
        )
        # The replay produces the whole fixture; the range still applies, so
        # --from/--to mean the same thing in both modes.
        wanted = {
            d.detection_id
            for d in detections
            if window_start <= d.detected_at.utc < window_end
        }
        detections = [d for d in detections if d.detection_id in wanted]
        evaluations = [e for e in evaluations if e.detection_id in wanted]
        source = f"replay of {args.replay}"
    else:
        detections, evaluations = _stored(
            window_start, window_end, rule, account_scope=account_scope
        )
        source = "Firestore"

    report = aggregate(
        detections,
        evaluations,
        rule,
        point=args.point,
        agents=(
            tuple(a.strip() for a in args.agents.split(",") if a.strip())
            if args.agents
            else None
        ),
    )

    print(
        f"{rule.rule_id} — broker dates {first.isoformat()} … {last.isoformat()} "
        f"({window_start.isoformat()} … {window_end.isoformat()}, {source})"
    )
    print(f"{len(detections)} detections, {len(list(evaluations))} evaluations")
    print()
    print("\n".join(render_markdown(report)) if args.markdown else render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
