"""The engine, the transaction boundary and the timestamp guard, against a real server.

These are integration tests on purpose. A fake connection would happily agree that a
rollback rolled back; the whole value of §12's and §15's atomicity is that PostgreSQL
enforces it, so the tests that matter have to ask PostgreSQL.

They skip without a server (C-10). ``tests/unit/test_storage_backend.py`` covers
everything that can be decided without one -- URL validation, redaction, the startup
wait -- so a skip here does not leave that logic unexercised.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, select, text

from aureon.storage.postgres.database import Database, DatabaseUnavailable
from aureon.storage.postgres.models import NaiveTimestamp, UtcTimestamp

pytestmark = pytest.mark.postgres


# ── The connection ────────────────────────────────────────────────────────────


def test_a_probe_succeeds_against_a_live_server(database: Database) -> None:
    database.probe()  # raises on failure; returning is the assertion


def test_a_transaction_probe_succeeds(database: Database) -> None:
    """§35's own row. Passes ``SELECT 1`` and still fails on a read-only database."""
    database.transaction_probe()


def test_the_name_is_the_test_database(database: Database) -> None:
    """C-9, from the other end: whatever resolved the URL, this is where we landed."""
    assert database.name == "aureon_test"


def test_an_unreachable_server_raises_database_unavailable() -> None:
    """§27 needs ONE exception type for "the database did not answer".

    Port 1 rather than a bad host: a DNS failure and a refused connection arrive through
    different SQLAlchemy exceptions, and the refused one is what a stopped Windows service
    actually produces.
    """
    unreachable = Database("postgresql+psycopg://aureon:x@127.0.0.1:1/aureon_test")
    try:
        with pytest.raises(DatabaseUnavailable):
            unreachable.probe()
    finally:
        unreachable.dispose()


def test_a_query_error_keeps_its_own_type(database: Database) -> None:
    """The other half of that rule: a bug in Aureon must not look like an outage.

    If a misspelled column arrived as ``DatabaseUnavailable``, §27's fail-closed path would
    catch it, report "the local database is unavailable", and an operator would restart a
    perfectly healthy service looking for a fault that was in a query.
    """
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError):
        with database.connect() as connection:
            connection.execute(text("SELECT no_such_function_at_all()"))


# ── The transaction boundary ──────────────────────────────────────────────────


@pytest.fixture
def scratch(database: Database):
    """A table to write to, dropped afterwards.

    Built with a throwaway ``MetaData`` rather than the real ``Base`` so these tests cannot
    be affected by, or affect, the schema S-2 introduces.
    """
    metadata = MetaData()
    table = Table(
        "s1_scratch",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("at", UtcTimestamp),
    )
    with database.transaction() as connection:
        metadata.create_all(connection)
    yield table
    with database.transaction() as connection:
        metadata.drop_all(connection)


def test_a_transaction_commits_on_success(database: Database, scratch: Table) -> None:
    with database.transaction() as connection:
        connection.execute(scratch.insert().values(id=1, at=datetime.now(UTC)))
    with database.connect() as connection:
        rows = connection.execute(select(scratch.c.id)).all()
    assert [row.id for row in rows] == [1]


def test_a_transaction_rolls_back_when_the_body_raises(
    database: Database, scratch: Table
) -> None:
    """The rollback has to cover an exception from the CALLER's body, not just a query.

    §12's setup write validates the transition *in Python*, between the locking read and
    the insert. If a ``TransitionError`` there left the insert committed, an illegal
    transition would be rejected and recorded at the same time.
    """

    class Refused(RuntimeError):
        pass

    with pytest.raises(Refused):
        with database.transaction() as connection:
            connection.execute(scratch.insert().values(id=2, at=datetime.now(UTC)))
            raise Refused("the guard said no")

    with database.connect() as connection:
        rows = connection.execute(select(scratch.c.id)).all()
    assert rows == [], "the insert survived a rolled-back transaction"


# ── The timestamp guard ───────────────────────────────────────────────────────


def test_a_naive_timestamp_is_refused_on_the_way_in(
    database: Database, scratch: Table
) -> None:
    """CLAUDE.md's tz-aware rule, enforced where it cannot be bypassed.

    Until now it lived in the pydantic models, so it held for anything that went through a
    model and not for a repository building an UPDATE from a plain ``datetime``.

    The assertion is on the WRAPPED form, which is the one a caller actually sees:
    SQLAlchemy catches anything a bind processor raises and re-raises it as a
    ``StatementError`` carrying the original on ``.orig``. Asserting ``NaiveTimestamp``
    directly would be asserting something no caller can catch (decision 333).
    """
    from sqlalchemy.exc import StatementError

    with pytest.raises(StatementError) as raised:
        with database.transaction() as connection:
            connection.execute(scratch.insert().values(id=3, at=datetime(2026, 9, 22, 9, 0)))
    assert isinstance(raised.value.orig, NaiveTimestamp), raised.value.orig
    assert "tz-aware" in str(raised.value.orig)


def test_the_refusal_rolls_the_whole_statement_back(
    database: Database, scratch: Table
) -> None:
    """A rejected timestamp must not leave a half-written row.

    The guard fires inside bind-parameter processing, before the INSERT is sent -- so
    nothing should reach the table. Worth asserting rather than assuming: a version that
    coerced the value instead of raising would also pass the test above if the message were
    checked loosely, and would leave a row with a silently shifted time.
    """
    from sqlalchemy.exc import StatementError

    with pytest.raises(StatementError):
        with database.transaction() as connection:
            connection.execute(scratch.insert().values(id=5, at=datetime.now(UTC)))
            connection.execute(scratch.insert().values(id=6, at=datetime(2026, 9, 22, 9, 0)))

    with database.connect() as connection:
        rows = connection.execute(select(scratch.c.id)).all()
    assert rows == [], "the good row before the refused one survived"


def test_an_aware_timestamp_comes_back_as_utc(database: Database, scratch: Table) -> None:
    """Stored from Athens, read back in UTC, same instant.

    The value matters: 12:00 in Athens is 09:00 UTC, and a layer that dropped the offset
    would return 12:00 and look right.
    """
    athens = datetime(2026, 9, 22, 12, 0, tzinfo=ZoneInfo("Europe/Athens"))
    with database.transaction() as connection:
        connection.execute(scratch.insert().values(id=4, at=athens))
    with database.connect() as connection:
        stored = connection.execute(select(scratch.c.at)).scalar_one()
    assert stored == athens
    assert stored.tzinfo is UTC
    assert stored.hour == 9, f"the offset was dropped: {stored}"


def test_the_column_is_timestamptz_in_the_server(database: Database, scratch: Table) -> None:
    """The schema half of the same claim, asked of the server rather than of SQLAlchemy.

    A column declared ``timestamp`` without a zone would pass every test above -- psycopg
    would hand back what it was given -- and would silently shift every stored time by the
    server's offset. So the assertion is on ``information_schema``.
    """
    with database.connect() as connection:
        data_type = connection.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 's1_scratch' AND column_name = 'at'"
            )
        ).scalar_one()
    assert data_type == "timestamp with time zone", data_type


def test_a_column_declared_without_a_zone_is_caught_on_the_first_read(
    database: Database,
) -> None:
    """The read-side half of the guard, which nothing else here exercises.

    This is the mistake the direction exists for: a column declared ``timestamp`` instead of
    ``timestamptz`` while the mapping says ``UtcTimestamp``. Every write succeeds, every read
    succeeds, and every value is wrong by the server's offset -- so the only moment it can be
    caught cheaply is the first row read back.

    The table is created with raw DDL so the column really is zone-less in the server, which
    is the only way to produce the condition; declaring it through SQLAlchemy would give a
    ``timestamptz`` and prove nothing.
    """
    metadata = MetaData()
    naive_table = Table(
        "s1_naive",
        metadata,
        Column("id", Integer, primary_key=True),
        # The MAPPING claims a zone; the DDL below does not give it one.
        Column("at", UtcTimestamp),
    )
    with database.transaction() as connection:
        connection.execute(
            text("CREATE TABLE s1_naive (id integer PRIMARY KEY, at timestamp without time zone)")
        )
        connection.execute(
            text("INSERT INTO s1_naive (id, at) VALUES (1, '2026-09-22 09:00:00')")
        )
    try:
        with pytest.raises(NaiveTimestamp) as raised:
            with database.connect() as connection:
                connection.execute(select(naive_table.c.at)).scalar_one()
        assert "timestamptz" in str(raised.value), "the message must name the fix"
    finally:
        with database.transaction() as connection:
            connection.execute(text("DROP TABLE IF EXISTS s1_naive"))


def test_the_read_is_correct_whatever_the_server_timezone_is(
    database: Database, scratch: Table
) -> None:
    """The conversion must not depend on the server's ``TimeZone`` setting.

    This is not hypothetical: a PostgreSQL installed on Windows takes its ``TimeZone`` from
    the machine, so the production server will very likely be on ``Europe/Athens`` or
    whatever the box is set to, while every machine the tests ran on was on UTC. On a UTC
    server psycopg hands back UTC and ``astimezone(UTC)`` is indistinguishable from
    ``replace(tzinfo=UTC)`` -- which is why a plant swapping them survived until this test
    existed (decision 338).

    With a non-UTC session the difference is the whole offset: the wrong one returns 12:00
    labelled UTC for an instant that is 09:00 UTC.
    """
    athens = datetime(2026, 9, 22, 12, 0, tzinfo=ZoneInfo("Europe/Athens"))
    with database.transaction() as connection:
        connection.execute(scratch.insert().values(id=7, at=athens))

    with database.connect() as connection:
        # A non-UTC session, as a Windows install would give by default.
        connection.execute(text("SET TIME ZONE 'America/New_York'"))
        stored = connection.execute(select(scratch.c.at)).scalar_one()

    assert stored == athens, "the instant changed"
    assert stored.tzinfo is UTC
    assert (stored.hour, stored.minute) == (9, 0), (
        f"the server's zone leaked into the value: {stored}"
    )
