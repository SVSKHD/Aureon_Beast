"""Weekly movement-ladder report by detector and timeframe."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any

from aureon.models.training import WeeklyAgentMovementRow, WeeklyTrainingReport


def build_weekly_training_report(
    memory: Any,
    *,
    symbol: str,
    iso_year: int,
    iso_week: int,
) -> WeeklyTrainingReport:
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    next_monday = monday + timedelta(days=7)
    examples = memory.examples_between(
        symbol,
        monday.isoformat(),
        next_monday.isoformat(),
    )

    buckets: dict[tuple[str, object], list[tuple[Any, dict[str, Any]]]] = defaultdict(list)
    for example in examples:
        for agent_name, read in (example.agent_read or {}).items():
            stance = str((read or {}).get("stance") or "").lower()
            if stance not in {"bullish", "bearish"}:
                continue
            buckets[(agent_name, example.timeframe)].append((example, read or {}))

    rows: list[WeeklyAgentMovementRow] = []
    for (agent_name, timeframe), items in sorted(
        buckets.items(),
        key=lambda one: (one[0][1].minutes, one[0][0]),
    ):
        aligned = [
            example
            for example, read in items
            if str(read.get("alignment") or "").lower() == "aligned"
        ]
        opposed = sum(
            str(read.get("alignment") or "").lower() == "opposed"
            for _, read in items
        )
        max_moves = [
            example.max_favourable_move_price
            for example in aligned
            if example.max_favourable_move_price is not None
        ]
        extensions = [
            example.extension_after_six_price
            for example in aligned
            if example.extension_after_six_price is not None
        ]
        rows.append(
            WeeklyAgentMovementRow(
                agent_name=agent_name,
                timeframe=timeframe,
                decisions=len(items),
                aligned_decisions=len(aligned),
                opposed_decisions=opposed,
                reached_six=sum(example.six_dollar_reached is True for example in aligned),
                reached_twenty=sum(
                    example.twenty_dollar_reached is True for example in aligned
                ),
                reached_forty=sum(
                    example.forty_dollar_reached is True for example in aligned
                ),
                max_move_available=len(max_moves),
                median_max_move_price=(
                    float(median(max_moves)) if max_moves else None
                ),
                maximum_move_price=max(max_moves) if max_moves else None,
                median_extension_after_six_price=(
                    float(median(extensions)) if extensions else None
                ),
            )
        )

    return WeeklyTrainingReport(
        symbol=symbol.upper(),
        iso_year=iso_year,
        iso_week=iso_week,
        start_market_date=monday.isoformat(),
        end_market_date=next_monday.isoformat(),
        examples=len(examples),
        rows=tuple(rows),
    )


def render_weekly_training_report(report: WeeklyTrainingReport) -> str:
    lines = [
        (
            f"weekly movement {report.iso_year}-W{report.iso_week:02d} "
            f"[{report.symbol}] · {report.examples} setup examples"
        ),
        "agent/timeframe        decisions aligned  $6  $20  $40   median-max  max  +after-$6",
        "--------------------------------------------------------------------------------",
    ]
    for row in report.rows:
        median_max = (
            f"{row.median_max_move_price:.1f}"
            if row.median_max_move_price is not None
            else "—"
        )
        maximum = (
            f"{row.maximum_move_price:.1f}"
            if row.maximum_move_price is not None
            else "—"
        )
        extension = (
            f"{row.median_extension_after_six_price:.1f}"
            if row.median_extension_after_six_price is not None
            else "—"
        )
        name = f"{row.agent_name}/{row.timeframe.value}"
        lines.append(
            f"{name:<22} {row.decisions:>4} {row.aligned_decisions:>7} "
            f"{row.reached_six:>3} {row.reached_twenty:>4} {row.reached_forty:>4} "
            f"{median_max:>10} {maximum:>5} {extension:>10}"
        )
    if not report.rows:
        lines.append("no directional agent decisions in this week")
    lines.append(
        "Movement columns use aligned detector decisions only; opposed decisions are counted "
        "but are not credited with the setup-direction move."
    )
    return "\n".join(lines)
