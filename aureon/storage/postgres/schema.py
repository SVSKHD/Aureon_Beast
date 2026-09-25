"""What revision the code expects, what the database is at, and whether they agree.

Plan §6 and C-7. The preflight ``migrations`` row is built from this, and so is
``scripts/migrate.py``. One module so the launcher, the preflight and the operator tool
cannot disagree about what "current" means.

**The comparison is two-sided, and that is C-7.** A database OLDER than the code is the
familiar case: somebody deployed without migrating, and a query will fail on a missing
column. A database NEWER than the code is the dangerous one, and the one a single
``>=`` check misses entirely: it means an older build has started against a schema a newer
build wrote. That build will read the tables it knows, write the columns it knows, and
leave every column added since silently NULL -- on ``trade_requests``, that is a request
whose failure code or broker ticket never gets recorded, reported as a clean run.

So both directions FAIL, with different messages, because the remedies are opposite: one
is "run the migration", the other is "you are running the wrong build".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The revision this build of the code was written against. Bumped in the same commit as
#: the migration that introduces the new head, so a build and its schema move together.
EXPECTED_REVISION = "0003"

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
SCRIPT_LOCATION = REPO_ROOT / "aureon" / "storage" / "postgres" / "migrations"


class SchemaStatus(StrEnum):
    """What the comparison found. Every value but ``CURRENT`` is a preflight FAIL."""

    CURRENT = "current"
    #: The database has no alembic_version table at all: never migrated.
    EMPTY = "empty"
    #: The database is behind the code. Remedy: run the migration.
    BEHIND = "behind"
    #: The database is AHEAD of the code. Remedy: run the right build (C-7).
    AHEAD = "ahead"
    #: The tables do not match the models even though the revision matches.
    DRIFTED = "drifted"
    #: The database could not be reached or read.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SchemaState:
    """The answer, with enough detail for a preflight row and an operator message."""

    status: SchemaStatus
    expected: str
    found: str | None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is SchemaStatus.CURRENT

    def render(self) -> str:
        """One line for the preflight table and the startup banner."""
        if self.status is SchemaStatus.CURRENT:
            return f"schema={self.expected} current"
        if self.status is SchemaStatus.EMPTY:
            return (
                f"schema=none expected={self.expected}; the database has never been "
                "migrated. Run: python scripts/migrate.py upgrade"
            )
        if self.status is SchemaStatus.BEHIND:
            return (
                f"schema={self.found} expected={self.expected}; the database is BEHIND "
                "this build. Run: python scripts/migrate.py upgrade"
            )
        if self.status is SchemaStatus.AHEAD:
            return (
                f"schema={self.found} expected={self.expected}; the database is AHEAD of "
                "this build. This build would silently ignore every column added since "
                f"{self.expected} (C-7). Run the build that matches the schema."
            )
        if self.status is SchemaStatus.DRIFTED:
            return (
                f"schema={self.found} matches, but the tables do not match the models: "
                f"{self.detail}"
            )
        return f"schema unknown: {self.detail}"


def alembic_config(url: str) -> Any:
    """An alembic ``Config`` pointed at this repository and that URL.

    Built here rather than read from the environment by alembic itself, so a caller that
    already holds a ``Database`` (the tests, and the launcher) cannot end up migrating a
    different database than the one it is about to use.
    """
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", url)
    return config


def head_revision() -> str:
    """The newest revision on disk. Should equal ``EXPECTED_REVISION``."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config("postgresql+psycopg://unused/x"))
    return script.get_current_head() or ""


def _ordered_revisions() -> list[str]:
    """Every revision oldest-first, so "behind" and "ahead" can be told apart.

    String comparison would do for ``0001`` and ``0002`` and would quietly break the first
    time a revision id is not a zero-padded number -- which is alembic's default. The walk
    is over the actual revision graph instead.
    """
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config("postgresql+psycopg://unused/x"))
    return [revision.revision for revision in script.walk_revisions()][::-1]


def database_revision(database: Any) -> str | None:
    """The revision stamped in the database, or ``None`` if it has never been migrated."""
    from sqlalchemy import inspect, text

    with database.connect() as connection:
        if not inspect(connection).has_table("alembic_version"):
            return None
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def compare(database: Any, *, expected: str = EXPECTED_REVISION) -> SchemaState:
    """The preflight ``migrations`` row (plan §35), as data rather than a printed line."""
    from aureon.storage.postgres.database import DatabaseUnavailable

    try:
        found = database_revision(database)
    except DatabaseUnavailable as exc:
        return SchemaState(SchemaStatus.UNKNOWN, expected, None, str(exc))

    if found is None:
        return SchemaState(SchemaStatus.EMPTY, expected, None)
    if found == expected:
        return SchemaState(SchemaStatus.CURRENT, expected, found)

    order = _ordered_revisions()
    if found in order and expected in order:
        ahead = order.index(found) > order.index(expected)
    else:
        # A revision this build has never heard of can only have been written by a newer
        # one. Treating it as "behind" would tell an operator to migrate, which would then
        # do nothing and leave them believing the check passed.
        ahead = found not in order
    status = SchemaStatus.AHEAD if ahead else SchemaStatus.BEHIND
    return SchemaState(status, expected, found)


def drift(database: Any) -> list[str]:
    """Differences between the live schema and ``Base.metadata``, as readable lines.

    A revision can match while the tables do not: somebody edited a model and did not
    generate a migration, or edited a migration by hand. Both look completely healthy until
    a query mentions the column that is not there.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from aureon.storage.postgres import tables  # noqa: F401  -- registers every table
    from aureon.storage.postgres.models import Base

    with database.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)

    lines: list[str] = []
    for entry in diff:
        # The alembic_version table is alembic's own and is never in our metadata.
        if _mentions_alembic_version(entry):
            continue
        lines.append(_render_diff(entry))
    return lines


def _mentions_alembic_version(entry: Any) -> bool:
    text = repr(entry)
    return "alembic_version" in text


def _render_diff(entry: Any) -> str:
    """One readable line per difference.

    Alembic's diff entries are tuples whose shape depends on the kind of change, so this
    renders the kind and the table rather than trying to unpack every variant -- a renderer
    that raised on an unfamiliar shape would turn a schema warning into a crash.
    """
    if isinstance(entry, tuple) and entry:
        kind = entry[0]
        if kind in {"add_table", "remove_table"}:
            return f"{kind}: {getattr(entry[1], 'name', entry[1])}"
        if kind in {"add_column", "remove_column"}:
            column = entry[3]
            return f"{kind}: {entry[2]}.{getattr(column, 'name', column)}"
        return f"{kind}: {entry[1:]!r}"
    return repr(entry)
