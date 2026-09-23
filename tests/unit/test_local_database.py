"""Local SQLite runtime storage tests."""

from __future__ import annotations

from sqlalchemy import text

from aureon.storage.local_database import LocalDatabase


def test_local_database_uses_wal_and_persists_between_connections(tmp_path) -> None:
    database = LocalDatabase(tmp_path / "aureon.db")
    database.ensure_schema()

    with database.connect() as connection:
        mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    assert str(mode).lower() == "wal"
    assert foreign_keys == 1

    with database.transaction() as connection:
        connection.execute(
            text(
                "INSERT INTO heartbeats "
                "(service, schema_version, updated_at, instance_id, detail) "
                "VALUES (:service, 1, :updated_at, NULL, :detail)"
            ),
            {
                "service": "local-storage-test",
                "updated_at": "2026-09-23 00:00:00",
                "detail": "{}",
            },
        )

    database.dispose()
    reopened = LocalDatabase(tmp_path / "aureon.db")
    with reopened.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM heartbeats WHERE service=:service"),
            {"service": "local-storage-test"},
        ).scalar_one()
    assert count == 1
    reopened.dispose()


def test_transaction_rolls_back_on_error(tmp_path) -> None:
    database = LocalDatabase(tmp_path / "aureon.db")
    with database.transaction() as connection:
        connection.execute(text("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY)"))

    try:
        with database.transaction() as connection:
            connection.execute(text("INSERT INTO probe (id) VALUES (1)"))
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM probe")).scalar_one() == 0
    database.dispose()


def test_local_trade_requests_keep_the_service_read_contract(tmp_path) -> None:
    from aureon.models.enums import TradeRequestStatus
    from aureon.storage.postgres.repositories.trade_requests import TradeRequestRepository
    from tests.postgres.factories import a_trade_request

    database = LocalDatabase(tmp_path / "aureon.db")
    database.ensure_schema()
    repository = TradeRequestRepository(database)

    repository.create(a_trade_request("requested-1"))
    repository.create(a_trade_request("requested-2"))

    rows = repository.list_by_status(TradeRequestStatus.REQUESTED, limit=1)
    assert len(rows) == 1
    assert rows[0].status is TradeRequestStatus.REQUESTED

    rows = repository.list_by_status(
        [TradeRequestStatus.REQUESTED, TradeRequestStatus.PENDING],
        limit=10,
    )
    assert [row.request_id for row in rows] == ["requested-1", "requested-2"]
    database.dispose()


def test_local_control_requests_keep_the_service_read_contract(tmp_path) -> None:
    from aureon.models.enums import ControlRequestStatus
    from aureon.storage.postgres.repositories.control_requests import ControlRequestRepository
    from tests.postgres.factories import a_control_request

    database = LocalDatabase(tmp_path / "aureon.db")
    database.ensure_schema()
    repository = ControlRequestRepository(database)

    repository.create(a_control_request("control-1"))
    repository.create(a_control_request("control-2"))

    rows = repository.list_by_status(ControlRequestStatus.REQUESTED, limit=1)
    assert len(rows) == 1
    assert rows[0].status is ControlRequestStatus.REQUESTED

    rows = repository.list_by_status([ControlRequestStatus.REQUESTED], limit=10)
    assert [row.control_id for row in rows] == ["control-1", "control-2"]
    database.dispose()
