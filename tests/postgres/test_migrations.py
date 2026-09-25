"""``0001_initial`` against a real PostgreSQL (plan §6, C-5, C-7).

The migration is the one artefact whose correctness cannot be argued from the source: it
either produces the schema the models describe or it does not, and only the server can say.
So these run alembic for real, on a database created and dropped for the purpose.

They skip without a server, like everything in this directory (C-10).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from aureon.storage.postgres import schema
from aureon.storage.postgres.database import Database
from aureon.storage.postgres.models import Base
from aureon.storage.postgres.schema import SchemaStatus

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated(database: Database) -> Iterator[Database]:
    """A database with the migration applied, torn down afterwards.

    Torn down by dropping the tables rather than by ``alembic downgrade``, because there is
    no downgrade -- that is C-7, and it is asserted below.
    """
    from alembic import command

    # Start from nothing, regardless of what ran before. The repository tests create the
    # same tables with ``metadata.create_all``, and a session-scoped database is shared, so
    # a migration run on top of them would fail with "relation already exists" -- a
    # failure that depends on test ORDER and so appears and disappears between runs.
    with database.transaction() as connection:
        Base.metadata.drop_all(connection)
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))

    command.upgrade(schema.alembic_config(database.url), "head")
    yield database
    with database.transaction() as connection:
        Base.metadata.drop_all(connection)
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))


# ── The migration produces the schema the models describe ─────────────────────


def test_the_migration_creates_every_table(migrated: Database) -> None:
    from sqlalchemy import inspect

    with migrated.connect() as connection:
        found = set(inspect(connection).get_table_names())
    expected = set(Base.metadata.tables)
    assert expected <= found, f"the migration did not create: {sorted(expected - found)}"


def test_the_migrated_schema_matches_the_models(migrated: Database) -> None:
    """The check that makes the migration trustworthy rather than merely present.

    A migration hand-edited, or a model changed without regenerating one, leaves the
    revision looking perfect while the column a query needs is absent. This compares the
    live schema against ``Base.metadata`` and fails with the specific difference.
    """
    assert schema.drift(migrated) == []


def test_the_revision_is_stamped(migrated: Database) -> None:
    assert schema.database_revision(migrated) == schema.EXPECTED_REVISION
    assert schema.compare(migrated).status is SchemaStatus.CURRENT


def test_every_timestamp_column_is_timestamptz_in_the_server(migrated: Database) -> None:
    """Asked of ``information_schema`` rather than of SQLAlchemy.

    A column the migration created without a zone would round-trip perfectly in Python and
    be wrong by the server's offset for every row ever written.
    """
    with migrated.connect() as connection:
        bad = connection.execute(
            text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type LIKE 'timestamp%' "
                "AND data_type <> 'timestamp with time zone'"
            )
        ).all()
    assert bad == [], f"zone-less timestamp columns: {bad}"


def test_the_money_tables_are_all_present(migrated: Database) -> None:
    """C-7's four exist from revision one; nothing may ever drop them."""
    from sqlalchemy import inspect

    with migrated.connect() as connection:
        found = set(inspect(connection).get_table_names())
    for table in ("trade_requests", "trades", "audit_logs", "control_requests"):
        assert table in found


def test_the_claim_index_exists_on_trade_requests(migrated: Database) -> None:
    """§15's claim is ``WHERE status = ... ORDER BY confirmed_at FOR UPDATE SKIP LOCKED``.

    Without the index that is a sequential scan taking a row lock, which under two
    executors is the difference between skipping a locked row and waiting behind it.
    """
    with migrated.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'trade_requests'")
            ).all()
        }
    assert "ix_trade_requests_claim" in names


# ── An upsert at the deterministic id is idempotent ───────────────────────────


def _a_detection(detection_id: str) -> dict:
    """A complete, valid detection row.

    Spelled out rather than built from the pydantic model on purpose: these tests are about
    the TABLE, and going through the model would make them pass or fail for reasons that
    have nothing to do with the schema.
    """
    return {
        "detection_id": detection_id,
        "schema_version": 1,
        "account_scope": "primary",
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "agent_name": "ema_cross",
        "agent_version": "2.2.0",
        "event_key": "bullish_cross",
        "direction": None,
        "candle_close_utc": datetime(2026, 9, 22, 9, 5, tzinfo=UTC),
        "candle_time_utc": datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
        "timezone": "Europe/Athens",
        "market_date": "2026-09-22",
        "price": 2000.5,
        "sequence_today": 1,
        "sequence_session": 1,
        "session": "LONDON",
        "session_config_version": "1.0.0",
        "agent_params_snapshot": {},
        "indicators": None,
        "levels": {},
        "evidence": {},
        "volume_profile_ref": None,
        "volatility": None,
        "mtf": None,
    }


def test_a_second_write_at_the_same_id_collides(migrated: Database) -> None:
    """The primary key IS the idempotency guarantee (§12).

    The outbox may deliver the same detection twice after an ambiguous first attempt. That
    is safe precisely because the id is a pure function of the candle, so the retry
    addresses the same row -- and the DATABASE enforces it, not the caller's discipline.
    """
    from sqlalchemy.exc import IntegrityError

    table = Base.metadata.tables["detections"]
    row = _a_detection("abc123")
    with migrated.transaction() as connection:
        connection.execute(table.insert().values(**row))

    with pytest.raises(IntegrityError):
        with migrated.transaction() as connection:
            connection.execute(table.insert().values(**row))


def test_a_second_evaluation_for_the_same_rule_collides(migrated: Database) -> None:
    """§9's unique (detection_id, rule_id): one answer per rule, not a growing pile."""
    from sqlalchemy.exc import IntegrityError

    detections = Base.metadata.tables["detections"]
    with migrated.transaction() as connection:
        connection.execute(detections.insert().values(**_a_detection("parent")))

    table = Base.metadata.tables["detection_evaluations"]
    base = {
        "schema_version": 1,
        # A real parent: ``detection_id`` is NOT NULL and carries a foreign key, because an
        # evaluation of a detection that does not exist is an outcome with nothing to be an
        # outcome OF.
        "detection_id": "parent",
        "rule_id": "XAU_OUTCOME_V2",
        "evaluation_rule_id": None,
        "reference_price": "close",
        "reference_value": None,
        "context_tags": {},
        "horizons": {},
        "updated_at": None,
    }
    with migrated.transaction() as connection:
        connection.execute(table.insert().values(id="one__XAU_OUTCOME_V2", **base))

    with pytest.raises(IntegrityError):
        with migrated.transaction() as connection:
            # A DIFFERENT primary key, the SAME (detection_id, rule_id). Without the unique
            # constraint this would succeed and the detection would have two answers for
            # one rule, with nothing to say which is current.
            connection.execute(table.insert().values(id="two__XAU_OUTCOME_V2", **base))


def test_an_evaluation_cannot_reference_a_detection_that_does_not_exist(
    migrated: Database,
) -> None:
    """The foreign key, asserted rather than assumed.

    An orphaned evaluation is an outcome attributed to nothing, and it would be counted by
    every review that walks evaluations.
    """
    from sqlalchemy.exc import IntegrityError

    table = Base.metadata.tables["detection_evaluations"]
    with pytest.raises(IntegrityError):
        with migrated.transaction() as connection:
            connection.execute(
                table.insert().values(
                    id="orphan__XAU_OUTCOME_V2",
                    schema_version=1,
                    detection_id="no-such-detection",
                    rule_id="XAU_OUTCOME_V2",
                    evaluation_rule_id=None,
                    reference_price="close",
                    reference_value=None,
                    context_tags={},
                    horizons={},
                    updated_at=None,
                )
            )


def test_a_string_timestamp_is_refused_with_a_message_that_names_the_cause(
    migrated: Database,
) -> None:
    """Found by writing the test above with ISO strings (decision 345).

    The guard assumed a datetime, so a string produced ``AttributeError: 'str' object has
    no attribute 'tzinfo'`` from inside SQLAlchemy's bind processing -- naming neither the
    column nor the cause. It matters because the outbox stores JSON payloads, so a
    repository handing one straight to a column is a thing that will happen.
    """
    from sqlalchemy.exc import StatementError

    from aureon.storage.postgres.models import NaiveTimestamp

    table = Base.metadata.tables["detections"]
    row = _a_detection("stringy")
    row["candle_close_utc"] = "2026-09-22T09:05:00+00:00"

    with pytest.raises(StatementError) as raised:
        with migrated.transaction() as connection:
            connection.execute(table.insert().values(**row))
    assert isinstance(raised.value.orig, NaiveTimestamp), raised.value.orig
    assert "str" in str(raised.value.orig)
    assert "Parse it first" in str(raised.value.orig)


# ── C-7: no destructive downgrade ─────────────────────────────────────────────


def test_alembic_downgrade_is_refused(migrated: Database) -> None:
    """Run through alembic itself, not by calling ``downgrade()`` directly.

    The unit test calls the function; this proves the refusal survives the path an operator
    would actually take, which is ``alembic downgrade base`` on a live database. The tables
    must still be there afterwards.
    """
    from alembic import command
    from sqlalchemy import inspect

    with pytest.raises(RuntimeError, match="no downgrade"):
        command.downgrade(schema.alembic_config(migrated.url), "base")

    with migrated.connect() as connection:
        found = set(inspect(connection).get_table_names())
    assert "trade_requests" in found, "the refusal did not protect the money tables"
    assert schema.database_revision(migrated) == schema.EXPECTED_REVISION


# ── The empty database ────────────────────────────────────────────────────────


def test_an_unmigrated_database_reports_empty(database: Database) -> None:
    """Deliberately does NOT use the ``migrated`` fixture."""
    with database.transaction() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    state = schema.compare(database)
    assert state.status is SchemaStatus.EMPTY
    assert "never been migrated" in state.render()
