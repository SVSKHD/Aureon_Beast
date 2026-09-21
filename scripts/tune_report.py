#!/usr/bin/env python3
"""What each threshold would have selected, from this symbol's own archived bars (11C, F-4).

    python scripts/tune_report.py --symbol XAGUSD --days 10
    python scripts/tune_report.py --symbol XAUUSD --days 5 --fixture   # a demonstration

Writes ``docs/TUNING_{SYMBOL}.md``: one section per threshold, each with the distribution of
the quantity the agent compares against it and a table of candidate values against how many
bars they would admit. The reasoning is in ``aureon/config/tuning_report.py``.

## It does not change anything, and cannot be made to

There is no ``--apply``. A threshold change is an ``agent_version`` bump (§12) -- the
parameters live in ``agent_params_snapshot`` and the version is part of the
``detection_id``, so a value changed without a bump leaves the old detections at ids the
new agent would never produce, with nothing to separate the populations. That is a commit,
made by a human, with the version beside it. A flag that edited ``OVERRIDES`` would make it
possible to retune a live system by running a report, and the retune would be invisible
afterwards.

## Real bars, or it says so in the title

The archive is what the observer actually served (§82). Without one there is nothing to
report and the script refuses -- except with ``--fixture``, which reads the generated
fixture instead and stamps **SYNTHETIC** on every page of the output. A generated random
walk has the distribution its generator was given, so a threshold chosen from one is a
threshold chosen from `scripts/gen_fixtures.py`; that is worth having as a demonstration of
the tool and worth nothing as a tuning decision.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.config import AureonConfig  # noqa: E402
from aureon.config.symbol_tuning import known_symbols, tuning_for  # noqa: E402
from aureon.config.tuning_report import (  # noqa: E402
    MEASURES,
    Distribution,
    candidates,
    describe,
)
from aureon.models.enums import Timeframe  # noqa: E402
from aureon.models.market import Candle  # noqa: E402

DOCS = REPO_ROOT / "docs"

#: Below this many real days a report is a curiosity, not evidence. The same threshold 11C's
#: F-9 uses for an assessment's maturity, and for the same reason: ten days is the smallest
#: stretch that contains two of every weekday and both ends of a week.
MATURE_DAYS = 10


def collect(
    symbol: str,
    *,
    days: int,
    timeframe: Timeframe,
    market_tz: str,
    archive_dir: Path | None,
    today: date | None = None,
) -> tuple[list[Candle], list[str]]:
    """Archived bars for the last ``days`` broker days, and which days were found."""
    from aureon.data.live_candle_archive import archive_path, read_archive

    end = today or datetime.now().date()
    found: list[str] = []
    candles: list[Candle] = []
    for back in range(days):
        market_date = (end - timedelta(days=back)).isoformat()
        if not archive_path(symbol, timeframe, market_date, root=archive_dir).exists():
            continue
        candles.extend(
            read_archive(
                symbol, timeframe, market_date, market_tz=market_tz, root=archive_dir
            )
        )
        found.append(market_date)
    return candles, sorted(found)


def from_fixture(symbol: str, *, market_tz: str) -> tuple[list[Candle], list[str]]:
    """The generated fixture, for a demonstration. Never evidence -- see the docstring."""
    from aureon.data.historical_provider import HistoricalDataProvider

    path = REPO_ROOT / "aureon" / "data" / "fixtures" / f"{symbol}_M5.csv"
    if not path.exists():
        raise SystemExit(f"no fixture at {path}")
    candles = HistoricalDataProvider(path, market_tz=market_tz, symbol=symbol).candles
    return candles, sorted({c.open_time.market_date for c in candles})


def render(
    symbol: str,
    distributions: list[Distribution],
    *,
    days: list[str],
    synthetic: bool,
    timeframe: Timeframe,
    generated_at: datetime,
) -> str:
    source = "SYNTHETIC (generated fixture)" if synthetic else "real archived bars"
    lines = [
        f"# Tuning report — {symbol}",
        "",
        f"**Source: {source}.** "
        + (
            "A generated random walk has the distribution its generator was given, so a "
            "threshold chosen from this page is a threshold chosen from "
            "`scripts/gen_fixtures.py`. This page demonstrates the tool; it is not a "
            "tuning decision."
            if synthetic
            else "What the observer served, replayed from the parquet archive (§82)."
        ),
        "",
        "**Nothing here has been applied.** A threshold change is an `agent_version` bump "
        "(§12): edit `OVERRIDES` in `aureon/config/symbol_tuning.py`, bump the agent's "
        "version in the same commit, and expect the old and new populations to be "
        "separable rather than merged. This script has no `--apply` and will not grow one.",
        "",
        "| | |",
        "|---|---|",
        f"| generated | {generated_at.isoformat(timespec='seconds')} |",
        f"| timeframe | `{timeframe.value}` |",
        f"| broker days | {len(days)} |",
        f"| first…last | {days[0] if days else '—'}…{days[-1] if days else '—'} |",
        f"| bars | {sum(d.n for d in distributions[:1]) if distributions else 0} |",
        f"| tuning | {'defaults' if tuning_for(symbol).is_default else 'overridden'} |",
        "",
    ]
    if not synthetic and len(days) < MATURE_DAYS:
        lines += [
            f"> **Immature history: {len(days)} real day(s), fewer than {MATURE_DAYS}.** "
            "Every number below is a description of that sample and not of the "
            "instrument. Ten days is the smallest stretch holding two of every weekday "
            "and both ends of a week; below it a single quiet Tuesday moves every "
            "quantile on this page.",
            "",
        ]

    for distribution in distributions:
        measure = distribution.measure
        lines += [
            f"## `{measure.field}` — currently {distribution.configured:g}",
            "",
            f"Measured: {measure.means} ({measure.unit}). "
            f"Compared in `{measure.agent}`.",
            "",
            f"**{distribution.verdict}**"
            + (
                f" — {distribution.skipped} bar(s) had no measurable value and are "
                "excluded."
                if distribution.skipped
                else ""
            ),
            "",
            "| quantile | value | would admit | share |",
            "|---:|---:|---:|---:|",
        ]
        for (quantile, _), (value, admitted, share) in zip(
            sorted(distribution.quantiles.items()),
            candidates(distribution),
            strict=True,
        ):
            lines.append(
                f"| p{quantile * 100:.0f} | {value:.4g} | {admitted} | {share:.1%} |"
            )
        lines.append("")

    lines += [
        "## What this cannot tell you",
        "",
        "Whether a detection was any good. That needs an outcome rule and a horizon, and "
        "`scripts/report_outcomes.py` is where it lives. This page answers the prior "
        "question: whether a threshold sits inside the range of the data at all. A value "
        "above every observed measure produces no detections and looks exactly like a quiet "
        "market; one below the first quartile produces a detection on most bars and looks "
        "exactly like a busy one. Neither is visible from the detections.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, help="one symbol, e.g. XAGUSD")
    parser.add_argument(
        "--days", type=int, default=MATURE_DAYS, help="how many broker days back to read"
    )
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--archive-dir", type=Path, default=None)
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="read the generated fixture instead of the archive; output is stamped SYNTHETIC",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--stdout", action="store_true", help="print the report instead of writing it"
    )
    args = parser.parse_args(argv)

    symbol = args.symbol.upper()
    if symbol not in known_symbols():
        parser.error(
            f"{symbol} has no reviewed tuning entry ({', '.join(known_symbols())}); its "
            "thresholds would be gold's numbers on an instrument nobody has looked at"
        )
    if args.days < 1:
        parser.error("--days must be at least 1")

    config = AureonConfig.from_env()
    timeframe = Timeframe(args.timeframe.upper())

    if args.fixture:
        candles, days = from_fixture(symbol, market_tz=config.market_tz)
    else:
        candles, days = collect(
            symbol,
            days=args.days,
            timeframe=timeframe,
            market_tz=config.market_tz,
            archive_dir=args.archive_dir,
        )
        if not candles:
            # A refusal rather than an empty report: a page of dashes under a real title
            # would be filed as "we looked and found nothing", which is a different claim
            # from "nothing has been recorded yet".
            print(
                f"no archived bars for {symbol} {timeframe.value} in the last "
                f"{args.days} broker day(s). The observer writes these as it runs "
                "(AUREON_ARCHIVE_DIR), so a report needs a session to have been "
                "recorded. Use --fixture for a demonstration against generated data.",
                file=sys.stderr,
            )
            return 1

    tuning = tuning_for(symbol)
    distributions = [describe(measure, candles, tuning) for measure in MEASURES]
    from aureon.models.base import utc_now

    text = render(
        symbol,
        distributions,
        days=days,
        synthetic=args.fixture,
        timeframe=timeframe,
        generated_at=utc_now(),
    )

    if args.stdout:
        print(text)
        return 0
    out = args.out or DOCS / f"TUNING_{symbol}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(candles)} bars over {len(days)} broker day(s))")
    if args.fixture:
        print("SYNTHETIC: generated data. Not a tuning decision.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
