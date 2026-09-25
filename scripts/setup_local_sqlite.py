#!/usr/bin/env python3
"""Bootstrap and verify Aureon's local SQLite application database.

Examples:

    python scripts/setup_local_sqlite.py
    python scripts/setup_local_sqlite.py --path data/aureon.db
    python scripts/setup_local_sqlite.py --check
    python scripts/setup_local_sqlite.py --json

The script is deliberately local-only. It never contacts MT5, Discord or any cloud service and
never touches trading settings. It creates/updates the SQLite schema additively, verifies WAL,
foreign keys and required tables/columns, then prints the exact file Aureon will use.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.storage.backend import BACKEND_ENV, StorageBackend, selected_backend  # noqa: E402
from aureon.storage.local_database import (  # noqa: E402
    DEFAULT_PATH,
    PATH_ENV,
    LocalDatabase,
    local_db_path,
)

REQUIRED_TABLES = {
    "detections",
    "detection_evaluations",
    "setups",
    "setup_events",
    "setup_evaluations",
    "sessions",
    "system_state",
    "heartbeats",
    "notifications",
    "trade_requests",
    "trades",
    "control_requests",
    "ops_events",
}

REQUIRED_SETUP_COLUMNS = {
    "setup_id",
    "symbol",
    "timeframe",
    "family",
    "direction_context",
    "state",
    "market_date",
    "context_summary",
    "agent_confluence",
    "params_snapshot",
}


def inspect_local_database(database: LocalDatabase) -> dict[str, Any]:
    """Return a machine-readable health snapshot after schema creation."""
    with database.connect() as connection:
        journal_mode = str(
            connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        ).lower()
        foreign_keys = int(
            connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        )
        busy_timeout = int(
            connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        )
        tables = {
            str(row[0])
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if not str(row[0]).startswith("sqlite_")
        }
        setup_columns = {
            str(row[1])
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(setups)"
            ).fetchall()
        }
        counts: dict[str, int] = {}
        for table in ("detections", "setups", "setup_events", "detection_evaluations"):
            if table in tables:
                counts[table] = int(
                    connection.exec_driver_sql(
                        f'SELECT count(*) FROM "{table}"'
                    ).scalar_one()
                )

    missing_tables = sorted(REQUIRED_TABLES - tables)
    missing_setup_columns = sorted(REQUIRED_SETUP_COLUMNS - setup_columns)
    ok = (
        journal_mode == "wal"
        and foreign_keys == 1
        and not missing_tables
        and not missing_setup_columns
    )
    return {
        "ok": ok,
        "backend": StorageBackend.SQLITE.value,
        "path": str(database.path),
        "journal_mode": journal_mode,
        "foreign_keys": bool(foreign_keys),
        "busy_timeout_ms": busy_timeout,
        "table_count": len(tables),
        "missing_tables": missing_tables,
        "missing_setup_columns": missing_setup_columns,
        "counts": counts,
    }


def render(report: dict[str, Any]) -> str:
    mark = "PASS" if report["ok"] else "FAIL"
    lines = [
        f"{mark} local SQLite",
        f"  path: {report['path']}",
        f"  journal: {report['journal_mode']}",
        f"  foreign_keys: {'on' if report['foreign_keys'] else 'off'}",
        f"  busy_timeout: {report['busy_timeout_ms']} ms",
        f"  tables: {report['table_count']}",
        "  rows: "
        + ", ".join(f"{key}={value}" for key, value in report["counts"].items()),
    ]
    if report["missing_tables"]:
        lines.append("  missing tables: " + ", ".join(report["missing_tables"]))
    if report["missing_setup_columns"]:
        lines.append(
            "  missing setup columns: "
            + ", ".join(report["missing_setup_columns"])
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help=f"SQLite path (default: {PATH_ENV} or {DEFAULT_PATH})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify only; fail if the file does not already exist",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    try:
        backend = selected_backend()
    except ValueError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2
    if backend is not StorageBackend.SQLITE:
        print(
            f"FAIL {BACKEND_ENV}={backend.value}; local bootstrap requires sqlite",
            file=sys.stderr,
        )
        return 2

    path = (args.path or local_db_path()).expanduser().resolve()
    if args.check and not path.exists():
        print(f"FAIL local SQLite does not exist: {path}", file=sys.stderr)
        return 1

    database = LocalDatabase(path)
    try:
        if not args.check:
            database.ensure_schema()
        database.probe()
        report = inspect_local_database(database)
    except Exception as exc:  # noqa: BLE001 - operator CLI reports the exact failure
        report = {
            "ok": False,
            "backend": StorageBackend.SQLITE.value,
            "path": str(path),
            "error": f"{type(exc).__name__}: {exc}",
            "missing_tables": [],
            "missing_setup_columns": [],
            "counts": {},
        }
    finally:
        database.dispose()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        if "error" in report:
            print(f"FAIL local SQLite\n  path: {path}\n  error: {report['error']}")
        else:
            print(render(report))
        if report.get("ok"):
            print(
                "\nUse these environment values:\n"
                f"  {BACKEND_ENV}=sqlite\n"
                f"  {PATH_ENV}={path}"
            )
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
