#!/usr/bin/env python3
"""Report normalized agent-evidence coverage from the local SQLite database.

This is a data-readiness report, not a performance score. It answers:
- how many detections each agent/version has stored,
- how many contain normalized evidence,
- which numeric/categorical/flag keys exist.

Example:

    python scripts/report_agent_evidence.py
    python scripts/report_agent_evidence.py --symbol XAUUSD
    python scripts/report_agent_evidence.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.models.detection import AgentEvidence  # noqa: E402
from aureon.storage.local_database import get_database  # noqa: E402
from aureon.storage.postgres import tables  # noqa: E402


def build_report(*, symbol: str | None = None) -> dict[str, Any]:
    database = get_database()
    table = tables.Detection.__table__
    statement = select(
        table.c.agent_name,
        table.c.agent_version,
        table.c.evidence,
    ).order_by(table.c.agent_name, table.c.agent_version)
    if symbol:
        statement = statement.where(table.c.symbol == symbol)

    groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "detections": 0,
            "with_evidence": 0,
            "numeric_keys": set(),
            "categorical_keys": set(),
            "flag_keys": set(),
        }
    )
    with database.connect() as connection:
        rows = connection.execute(statement).mappings().all()

    for row in rows:
        key = (str(row["agent_name"]), str(row["agent_version"]))
        group = groups[key]
        group["detections"] += 1
        evidence = AgentEvidence.model_validate(row["evidence"] or {})
        if evidence.numeric or evidence.categorical or evidence.flags:
            group["with_evidence"] += 1
        group["numeric_keys"].update(evidence.numeric)
        group["categorical_keys"].update(evidence.categorical)
        group["flag_keys"].update(evidence.flags)

    agents = []
    for (name, version), group in sorted(groups.items()):
        count = int(group["detections"])
        covered = int(group["with_evidence"])
        agents.append(
            {
                "agent": name,
                "version": version,
                "detections": count,
                "with_evidence": covered,
                "coverage_pct": round(covered / count * 100, 1) if count else 0.0,
                "numeric_keys": sorted(group["numeric_keys"]),
                "categorical_keys": sorted(group["categorical_keys"]),
                "flag_keys": sorted(group["flag_keys"]),
            }
        )

    return {
        "database": str(database.path),
        "symbol": symbol,
        "total_detections": len(rows),
        "agents": agents,
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        "Aureon agent evidence readiness",
        f"database: {report['database']}",
        f"symbol: {report['symbol'] or 'ALL'}",
        f"detections: {report['total_detections']}",
        "",
    ]
    if not report["agents"]:
        lines.append("No detections stored yet.")
        return "\n".join(lines)

    for item in report["agents"]:
        lines.extend(
            [
                f"{item['agent']} v{item['version']}",
                (
                    f"  evidence: {item['with_evidence']}/{item['detections']} "
                    f"({item['coverage_pct']:.1f}%)"
                ),
                "  numeric: " + (", ".join(item["numeric_keys"]) or "—"),
                "  categorical: " + (", ".join(item["categorical_keys"]) or "—"),
                "  flags: " + (", ".join(item["flag_keys"]) or "—"),
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = build_report(symbol=args.symbol)
    except Exception as exc:  # noqa: BLE001 - operator CLI reports storage failures
        print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
