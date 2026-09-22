"""Fixtures for the tests that need a real PostgreSQL server (C-10).

Everything here skips cleanly when no server is configured, so ``pytest`` on a laptop with
no database still runs the whole unit suite. The alternative -- failing -- produces a
directory of red tests that people learn to ignore, which costs more than the coverage it
pretends to have.

The database is created once per session and dropped at the end, but only when this suite
created it. A database handed to us through ``AUREON_DATABASE_URL`` (the CI service
container) is left alone: dropping something we did not make is how a harness deletes a
developer's scratch database while they are using it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from aureon.storage.postgres.database import Database, redacted
from aureon.storage.postgres.models import Base
from tests.postgres_support import (
    admin_url,
    create_test_database,
    drop_test_database,
    resolve_test_url,
    should_drop,
)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A URL for a live, empty ``aureon_test``, or a skip explaining what is missing."""
    url, reason = resolve_test_url()
    if url is None:
        pytest.skip(reason or "no PostgreSQL configured")

    admin = admin_url()
    created = False
    if admin:
        created = create_test_database(admin)

    # Prove the server is actually there before a whole directory of tests fails one at a
    # time with the same connection error.
    probe = Database(url)
    try:
        probe.probe()
    except Exception as exc:  # noqa: BLE001 - the reason belongs in the skip message
        probe.dispose()
        if should_drop(created=created, admin=admin):
            drop_test_database(admin)  # type: ignore[arg-type]
        pytest.skip(f"{redacted(url)} is not reachable: {exc}")
    probe.dispose()

    yield url

    if should_drop(created=created, admin=admin):
        drop_test_database(admin)  # type: ignore[arg-type]


@pytest.fixture
def database(postgres_url: str) -> Iterator[Database]:
    """A ``Database`` on ``aureon_test``, its pool disposed afterwards.

    Function-scoped: a leaked connection from one test would hold a lock the next one waits
    on, and a suite that deadlocks is harder to read than one that reconnects.
    """
    instance = Database(postgres_url)
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture
def schema(database: Database) -> Iterator[Database]:
    """A database with the tables created and emptied, per test.

    Created with ``metadata.create_all`` rather than by running the migration, because
    these tests are about the REPOSITORIES: running alembic per test would make every one
    of them also a test of the migration, and a migration failure would then look like a
    repository bug in forty places at once. ``tests/postgres/test_migrations.py`` is where
    the migration is the subject.

    Emptied between tests by truncating rather than dropping: dropping and recreating 24
    tables per test is slow enough to change how often the suite gets run.
    """
    from sqlalchemy import text

    from aureon.storage.postgres import tables  # noqa: F401  -- registers every table

    with database.transaction() as connection:
        Base.metadata.create_all(connection)
    names = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    with database.transaction() as connection:
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    yield database
