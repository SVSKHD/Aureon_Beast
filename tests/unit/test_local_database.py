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
