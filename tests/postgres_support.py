"""Where the suite's PostgreSQL goes, and the refusal that keeps it out of production.

Two corrections live here.

**C-9 — test isolation.** The prefix that used to keep the suite away from live data is
gone with Firestore, and a prefix was never the right shape for a relational database:
there is nothing to prefix. The replacement is a name. ``AUREON_DATABASE_URL`` must point
at a database called ``aureon_test``, and a URL naming ``aureon`` is refused outright. The
refusal runs at conftest import, before any fixture or any test, because a check that ran
inside a fixture would already have let a module-scoped connection open.

Stated as two rules because they fail differently. "Must be ``aureon_test``" catches a
run pointed at ``aureon_staging`` or at somebody's scratch database, where the cost is
confusion. "Never ``aureon``" catches the one where the cost is a truncated production
table, and it gets its own test and its own message so the message says what nearly
happened.

**C-10 — a test backend without Docker.** The Docker daemon is not available everywhere
the suite has to run. So:

* ``AUREON_DATABASE_URL`` already set and naming ``aureon_test`` is used as it is. This is
  the CI shape: the workflow brings up a PostgreSQL service container and exports the URL.
  The harness deliberately does **not** manage containers -- a container lifecycle written
  on a machine with no Docker daemon would be code nobody had ever run, and the suite would
  be trusting it (decision 332).
* otherwise ``AUREON_TEST_ADMIN_URL`` -- a URL for a role that may ``CREATE DATABASE`` on
  the local server -- is used to create ``aureon_test`` and to drop it afterwards.
* otherwise the PostgreSQL tests skip, with a message naming both variables. They skip
  rather than fail because the unit suite must stay runnable on a laptop with no server,
  and a directory of red tests is a directory people stop reading.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

#: The only database name the suite may be pointed at.
TEST_DATABASE_NAME = "aureon_test"

#: The production name, refused by its own rule with its own message.
PRODUCTION_DATABASE_NAME = "aureon"

ADMIN_URL_ENV = "AUREON_TEST_ADMIN_URL"


class UnsafeTestDatabase(RuntimeError):
    """The suite was pointed at a database it must not touch."""


def assert_test_database(url: str) -> str:
    """Refuse a URL that is not ``aureon_test`` (C-9).

    Returns the URL so a caller can write ``url = assert_test_database(url)`` and cannot
    accidentally keep an unchecked one.
    """
    from aureon.storage.postgres.database import database_name, redacted

    name = database_name(url)
    if name == PRODUCTION_DATABASE_NAME:
        raise UnsafeTestDatabase(
            f"AUREON_DATABASE_URL names the PRODUCTION database {name!r} "
            f"({redacted(url)}). The suite creates, truncates and drops tables. "
            f"Point it at {TEST_DATABASE_NAME!r}. Refusing to run."
        )
    if name != TEST_DATABASE_NAME:
        raise UnsafeTestDatabase(
            f"AUREON_DATABASE_URL names {name!r} ({redacted(url)}); the suite may only "
            f"run against {TEST_DATABASE_NAME!r} (C-9). Refusing to run."
        )
    return url


def guard_configured_url(env: dict[str, str] | None = None) -> None:
    """Apply C-9's refusal to the environment, if a database URL is set at all.

    Called at conftest import. A URL that is absent is fine -- most of the suite needs no
    database -- and a URL that is present is checked before anything can connect.
    """
    source = os.environ if env is None else env
    raw = (source.get("AUREON_DATABASE_URL") or "").strip()
    if raw:
        assert_test_database(raw)


def with_database(url: str, name: str) -> str:
    """The same URL pointing at a different database, credentials intact."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def admin_url(env: dict[str, str] | None = None) -> str | None:
    """``AUREON_TEST_ADMIN_URL``, if set."""
    source = os.environ if env is None else env
    return (source.get(ADMIN_URL_ENV) or "").strip() or None


def resolve_test_url(env: dict[str, str] | None = None) -> tuple[str | None, str | None]:
    """``(url, skip_reason)`` -- where the PostgreSQL tests should run, or why not.

    Exactly one of the two is ``None``. Returning the reason rather than calling
    ``pytest.skip`` keeps this module importable by a plain ``python -c``, which is how the
    RUNBOOK's "is my machine set up" check is written.
    """
    source = os.environ if env is None else env
    configured = (source.get("AUREON_DATABASE_URL") or "").strip()
    if configured:
        # Already guarded at conftest import; re-checked here so a test that builds an env
        # dict by hand cannot route around the refusal.
        return assert_test_database(configured), None

    admin = admin_url(source)
    if admin:
        return with_database(admin, TEST_DATABASE_NAME), None

    return None, (
        "no local PostgreSQL configured: set AUREON_DATABASE_URL to an "
        f"{TEST_DATABASE_NAME!r} database (CI does this with a service container), or "
        f"{ADMIN_URL_ENV} to a role that may CREATE DATABASE on the local server (C-10). "
        "docs/RUNBOOK.md, 'A local PostgreSQL for the tests', has both."
    )


def should_drop(*, created: bool, admin: str | None) -> bool:
    """Whether the session fixture may drop the test database when it finishes.

    A rule rather than an inline condition, because the wrong answer is expensive and
    invisible: a harness that dropped whatever it was pointed at would delete a developer's
    scratch database the first time they exported ``AUREON_DATABASE_URL`` to debug one test,
    and they would find out by losing it.

    Two conditions, both necessary. ``created`` means this session made the database, so it
    is ours to remove; ``admin`` is the only credential that can remove it. A database handed
    to us -- CI's service container, or a developer's own -- has ``created`` false and is left
    exactly as it was found.
    """
    return created and admin is not None


def create_test_database(admin: str, *, name: str = TEST_DATABASE_NAME) -> bool:
    """Create ``name`` through ``admin`` if it does not exist. ``True`` if it was created.

    ``CREATE DATABASE`` cannot run inside a transaction, hence ``AUTOCOMMIT``. The
    existence check and the create are not atomic, and that is fine: two suites racing
    here both end up with the database, and the loser sees ``DuplicateDatabase``, which is
    caught.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError

    from aureon.storage.postgres.database import Database

    database = Database(admin)
    try:
        with database.connect() as connection:
            connection = connection.execution_options(isolation_level="AUTOCOMMIT")
            existing = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).first()
            if existing is not None:
                return False
            try:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
            except ProgrammingError:  # pragma: no cover - the losing side of a race
                return False
            return True
    finally:
        database.dispose()


def drop_test_database(admin: str, *, name: str = TEST_DATABASE_NAME) -> None:
    """Drop ``name`` through ``admin``, disconnecting anything still attached.

    Refuses any name but a test one, so a mistyped argument cannot drop production through
    a helper whose whole job is cleanup.
    """
    if name in {PRODUCTION_DATABASE_NAME, ""} or not name.endswith("_test"):
        raise UnsafeTestDatabase(f"refusing to drop {name!r}: not a test database")

    from sqlalchemy import text

    from aureon.storage.postgres.database import Database

    database = Database(admin)
    try:
        with database.connect() as connection:
            connection = connection.execution_options(isolation_level="AUTOCOMMIT")
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        database.dispose()
