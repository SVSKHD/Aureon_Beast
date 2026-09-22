#!/usr/bin/env python3
"""Apply and inspect the database schema (plan §6).

    python scripts/migrate.py upgrade    # bring the database to the head revision
    python scripts/migrate.py current    # what the database is at, and what the code wants
    python scripts/migrate.py check      # exit non-zero unless they agree AND match the models

``check`` is what preflight runs and what a deployment should run before starting the
services. It fails in BOTH directions (C-7): a database behind the code means a query will
hit a missing column, and a database AHEAD of the code means an older build is about to
write NULL into every column added since -- on ``trade_requests``, a failure code or broker
ticket that never gets recorded, reported as a clean run.

This is an operator tool, so it talks to the database directly rather than through a
repository -- its subject IS the database, and the boundary test that keeps SQL out of
``aureon/`` deliberately does not cover ``scripts/`` (decision 334).

Exit codes are stable, because a deployment script branches on them:

    0  the schema is current and matches the models
    1  the schema is wrong (behind, ahead, drifted, or never migrated)
    2  the database could not be reached
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.storage.postgres import schema  # noqa: E402
from aureon.storage.postgres.database import (  # noqa: E402
    Database,
    DatabaseUnavailable,
    DatabaseUrlError,
    database_url,
    redacted,
)

EXIT_OK = 0
EXIT_SCHEMA_WRONG = 1
EXIT_UNREACHABLE = 2


def _database() -> Database:
    return Database(database_url())


def upgrade() -> int:
    """Run every migration up to the head revision."""
    from alembic import command

    url = database_url()
    print(f"database  {redacted(url)}")
    database = Database(url)
    try:
        before = schema.database_revision(database) or "none"
    finally:
        database.dispose()

    print(f"at        {before}")
    print(f"head      {schema.head_revision()}")
    if before == schema.head_revision():
        print("nothing to do")
        return EXIT_OK

    command.upgrade(schema.alembic_config(url), "head")
    database = Database(url)
    try:
        after = schema.database_revision(database) or "none"
    finally:
        database.dispose()
    print(f"now       {after}")
    return EXIT_OK


def current() -> int:
    """Report where the database is, without changing anything."""
    url = database_url()
    database = Database(url)
    try:
        state = schema.compare(database)
        print(f"database  {redacted(url)}")
        print(f"expected  {state.expected}")
        print(f"found     {state.found or 'none'}")
        print(f"status    {state.status.value}")
        print(f"          {state.render()}")
    finally:
        database.dispose()
    if state.status is schema.SchemaStatus.UNKNOWN:
        return EXIT_UNREACHABLE
    return EXIT_OK if state.ok else EXIT_SCHEMA_WRONG


def check() -> int:
    """The preflight's question: is this database safe for THIS build to start against?

    Two questions, not one. The revision has to match, AND the tables have to match the
    models -- a hand-edited migration, or a model changed without generating one, leaves
    the revision looking perfect while the column a query needs is absent.
    """
    url = database_url()
    database = Database(url)
    try:
        state = schema.compare(database)
        if state.status is schema.SchemaStatus.UNKNOWN:
            # Not a schema verdict: the database did not answer. Distinguished because a
            # deployment script branching on exit 1 will try to MIGRATE, and migrating a
            # database that is down is not the remedy for a database that is down.
            print(f"FAIL  migrations  {state.render()}")
            return EXIT_UNREACHABLE
        if not state.ok:
            print(f"FAIL  migrations  {state.render()}")
            return EXIT_SCHEMA_WRONG
        differences = schema.drift(database)
        if differences:
            print("FAIL  migrations  the database does not match the models:")
            for line in differences:
                print(f"        {line}")
            print("      generate a migration: python -m alembic revision --autogenerate")
            return EXIT_SCHEMA_WRONG
        print(f"PASS  migrations  {state.render()}")
        return EXIT_OK
    finally:
        database.dispose()


COMMANDS = {"upgrade": upgrade, "current": current, "check": check}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-5s %(message)s",
    )

    try:
        return COMMANDS[args.command]()
    except DatabaseUrlError as exc:
        print(f"FAIL  {exc}")
        return EXIT_UNREACHABLE
    except DatabaseUnavailable as exc:
        print(f"FAIL  {exc}")
        return EXIT_UNREACHABLE


if __name__ == "__main__":
    raise SystemExit(main())
