"""The preflight ``migrations`` row (plan §35, C-7, decision 346).

Written because a plant that made the row PASS on a database AHEAD of the code survived
everything: the row had no test at all. It had been verified by hand in a REPL, which is
not a test -- it is a memory of one.

The row is the last thing between an operator and starting a build against the wrong
schema, and the two failure directions have OPPOSITE remedies, so every branch is asserted
including the wording of the remedy.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from aureon.config.config import AureonConfig
from aureon.services.checks import Status
from aureon.services.preflight import Preflight
from aureon.storage.postgres import schema
from aureon.storage.postgres.schema import SchemaState, SchemaStatus


class _FakeDatabase:
    """Never connects. The comparison itself is stubbed per test."""

    disposed = False

    def dispose(self) -> None:
        self.disposed = True


def _preflight(**kwargs: Any) -> Preflight:
    return Preflight(
        AureonConfig(),
        provider_factory=lambda: None,
        client_factory=lambda: None,
        **kwargs,
    )


@pytest.fixture
def postgres_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``AUREON_STORAGE_BACKEND=postgres``, so the row is live rather than skipped."""
    monkeypatch.setenv("AUREON_STORAGE_BACKEND", "postgres")
    yield


@pytest.fixture
def stub_compare(monkeypatch: pytest.MonkeyPatch):
    """Replace the revision comparison and the drift walk, so no server is needed."""

    def install(state: SchemaState, *, differences: list[str] | None = None) -> None:
        monkeypatch.setattr(schema, "compare", lambda database, **_: state)
        monkeypatch.setattr(schema, "drift", lambda database: differences or [])

    return install


# ── Before S-4 there is nothing to check ──────────────────────────────────────


def test_the_row_skips_while_the_backend_is_still_firestore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decision 346. A FAIL here would be preflight failing on a correct system.

    Which is how an operator learns to ignore preflight -- and then misses the row that
    matters.
    """
    monkeypatch.delenv("AUREON_STORAGE_BACKEND", raising=False)
    result = _preflight(database_factory=lambda: _FakeDatabase()).check_migrations()
    assert result.status is Status.SKIP
    assert "not postgres" in result.detail


# ── The happy path ────────────────────────────────────────────────────────────


def test_a_current_schema_passes(postgres_backend, stub_compare) -> None:
    stub_compare(SchemaState(SchemaStatus.CURRENT, "0001", "0001"))
    result = _preflight(database_factory=lambda: _FakeDatabase()).check_migrations()
    assert result.status is Status.PASS
    assert "current" in result.detail


def test_the_database_is_disposed_whatever_the_verdict(postgres_backend, stub_compare) -> None:
    """A preflight that leaked a connection per run would hold one open on a machine where
    the operator is about to restart the database."""
    stub_compare(SchemaState(SchemaStatus.CURRENT, "0001", "0001"))
    database = _FakeDatabase()
    _preflight(database_factory=lambda: database).check_migrations()
    assert database.disposed

    database = _FakeDatabase()
    stub_compare(SchemaState(SchemaStatus.BEHIND, "0002", "0001"))
    _preflight(database_factory=lambda: database).check_migrations()
    assert database.disposed


# ── C-7: both directions fail, with opposite remedies ─────────────────────────


def test_a_database_behind_the_code_fails_and_says_to_migrate(
    postgres_backend, stub_compare
) -> None:
    stub_compare(SchemaState(SchemaStatus.BEHIND, "0002", "0001"))
    result = _preflight(database_factory=lambda: _FakeDatabase()).check_migrations()
    assert result.status is Status.FAIL
    assert result.remedy is not None and "migrate.py upgrade" in result.remedy


def test_a_database_ahead_of_the_code_fails_and_says_NOT_to_migrate(
    postgres_backend, stub_compare
) -> None:
    """The plant that survived. C-7's dangerous direction.

    An older build against a newer schema reads the tables it knows, writes the columns it
    knows, and leaves every column added since silently NULL -- on ``trade_requests``, a
    broker ticket or failure code that never gets recorded, in a run that reports success.
    Telling that operator to migrate would be telling them to do nothing and believe it
    worked, so the remedy must say the opposite.
    """
    stub_compare(SchemaState(SchemaStatus.AHEAD, "0001", "0099"))
    result = _preflight(database_factory=lambda: _FakeDatabase()).check_migrations()
    assert result.status is Status.FAIL
    assert result.remedy is not None
    assert "do NOT migrate" in result.remedy
    assert "matches the schema" in result.remedy


def test_a_never_migrated_database_fails(postgres_backend, stub_compare) -> None:
    stub_compare(SchemaState(SchemaStatus.EMPTY, "0001", None))
    result = _preflight(database_factory=lambda: _FakeDatabase()).check_migrations()
    assert result.status is Status.FAIL
    assert result.remedy is not None and "upgrade" in result.remedy


# ── Drift: the revision matches and the tables do not ─────────────────────────


def test_a_matching_revision_with_mismatched_tables_fails(
    postgres_backend, stub_compare
) -> None:
    """A model changed without a migration, or a migration hand-edited.

    Everything looks healthy -- the revision is right -- until a query mentions the column
    that is not there. The detail names the difference so the fix is one command away.
    """
    stub_compare(
        SchemaState(SchemaStatus.CURRENT, "0001", "0001"),
        differences=["add_column: trade_requests.failure_code"],
    )
    result = _preflight(database_factory=lambda: _FakeDatabase()).check_migrations()
    assert result.status is Status.FAIL
    assert "trade_requests.failure_code" in result.detail
    assert result.remedy is not None and "autogenerate" in result.remedy


# ── The database is not there at all ──────────────────────────────────────────


def test_an_unreachable_database_fails_and_says_to_start_the_service(
    postgres_backend, stub_compare
) -> None:
    """Distinguished from a schema verdict: the remedy is to start PostgreSQL, not to
    migrate a database that is not answering."""
    stub_compare(SchemaState(SchemaStatus.UNKNOWN, "0001", None, "not reachable"))
    result = _preflight(database_factory=lambda: _FakeDatabase()).check_migrations()
    assert result.status is Status.FAIL
    assert result.remedy is not None and "PostgreSQL service" in result.remedy


def test_a_broken_database_url_is_reported_rather_than_raised(postgres_backend) -> None:
    """Preflight exists to REPORT a broken configuration, not to crash on one.

    A factory that raised would take the whole preflight down and the operator would never
    see the rows after this one -- including the MT5 ones they were probably looking for.
    """

    def explode() -> Any:
        raise ValueError("AUREON_DATABASE_URL is not set")

    result = _preflight(database_factory=explode).check_migrations()
    assert result.status is Status.FAIL
    assert "not set" in result.detail
    assert result.remedy is not None and "AUREON_DATABASE_URL" in result.remedy


# ── It is wired into the run ───────────────────────────────────────────────────


def test_the_row_is_part_of_the_preflight_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A check nobody calls is a check that does not exist.

    The S-1 lesson (decision 339): a guard tested at its definition and never at its
    installation can be switched off silently. ``run()`` is the installation.
    """
    monkeypatch.delenv("AUREON_STORAGE_BACKEND", raising=False)
    preflight = _preflight(database_factory=lambda: _FakeDatabase())
    names = [check.__name__ for check in _run_order(preflight)]
    assert "check_migrations" in names


def _run_order(preflight: Preflight) -> list[Any]:
    """The checks ``run()`` iterates, read out of its own source.

    Read rather than executed because running the real preflight needs a terminal and a
    client; the question here is only whether the row is in the list.
    """
    import ast
    import inspect

    source = inspect.getsource(type(preflight).run)
    tree = ast.parse(source.lstrip())
    found: list[Any] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("check_"):
            found.append(getattr(type(preflight), node.attr))
    return found
