"""Local SQLite application storage for Aureon.

This is the temporary local-first backend. It deliberately implements the same small
Database surface used by the SQL repositories so the domain/services do not care whether
next week's durable SQL target is SQLite or PostgreSQL.

SQLite is appropriate here because all Aureon processes live on one Windows machine.
WAL mode permits concurrent readers, BEGIN IMMEDIATE serialises short writer
transactions, and busy_timeout turns brief writer contention into waiting instead of an
immediate "database is locked" failure.

MT5 remains broker truth; this database is application truth; Parquet remains candle
archive truth. No cloud service is required.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, text

log = logging.getLogger(__name__)

PATH_ENV = "AUREON_LOCAL_DB_PATH"
DEFAULT_PATH = "data/aureon.db"
BUSY_TIMEOUT_MS = 5000


class DatabaseUnavailable(RuntimeError):
    """The local application database could not be reached or written."""


class LocalDatabase:
    """Process-local SQLAlchemy engine over one shared SQLite WAL database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._engine: Any | None = None
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"sqlite+pysqlite:///{self.path.as_posix()}"

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def engine(self) -> Any:
        with self._lock:
            if self._engine is None:
                self._engine = self._build_engine()
            return self._engine

    def _build_engine(self) -> Any:
        engine = create_engine(
            self.url,
            future=True,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_MS / 1000},
        )

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection: Any, connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

        return engine

    def dispose(self) -> None:
        with self._lock:
            engine, self._engine = self._engine, None
        if engine is not None:
            engine.dispose()

    @contextmanager
    def connect(self) -> Iterator[Any]:
        try:
            with self.engine.connect() as connection:
                yield connection
        except Exception as exc:
            raise DatabaseUnavailable(f"local database {self.path} is unavailable: {exc}") from exc

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """One short write transaction.

        BEGIN IMMEDIATE obtains SQLite's writer reservation before any read/modify/write
        sequence. This is what makes the existing SELECT-then-transition repositories safe
        across Aureon's separate local processes even though SQLite ignores FOR UPDATE.
        """
        connection = self.engine.connect()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            finally:
                connection.close()
            raise
        else:
            connection.close()

    def probe(self) -> None:
        with self.connect() as connection:
            connection.execute(text("SELECT 1"))

    def transaction_probe(self) -> None:
        with self.transaction() as connection:
            connection.execute(text("SELECT 1"))

    def ensure_schema(self) -> None:
        """Create missing local tables and apply small additive SQLite upgrades.

        create_all creates tables but deliberately does not ALTER existing ones. The local
        database survives code updates, so additive columns must be applied explicitly or an
        existing database keeps the old shape forever.
        """
        from aureon.storage.postgres import tables  # noqa: F401
        from aureon.storage.postgres.models import Base

        Base.metadata.create_all(self.engine)
        self._ensure_additive_columns()

    def _ensure_additive_columns(self) -> None:
        """Apply additive-only local schema upgrades required by the current code."""
        with self.engine.begin() as connection:
            columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(setups)").fetchall()
            }
            if "agent_confluence" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE setups "
                    "ADD COLUMN agent_confluence JSON NOT NULL DEFAULT '{}'"
                )
                log.info("upgraded local schema: setups.agent_confluence")

            detection_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(detections)"
                ).fetchall()
            }
            if "evidence" not in detection_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE detections "
                    "ADD COLUMN evidence JSON NOT NULL DEFAULT '{}'"
                )
                log.info("upgraded local schema: detections.evidence")


            training_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(training_examples)"
                ).fetchall()
            }
            additions = {
                "twenty_dollar_reached": "BOOLEAN",
                "forty_dollar_reached": "BOOLEAN",
                "time_to_twenty_seconds": "FLOAT",
                "time_to_forty_seconds": "FLOAT",
                "max_favourable_move_price": "FLOAT",
                "extension_after_six_price": "FLOAT",
            }
            for name, sql_type in additions.items():
                if name not in training_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE training_examples ADD COLUMN {name} {sql_type}"
                    )
                    log.info("upgraded local schema: training_examples.%s", name)

    def wait_until_ready(self, **_: Any) -> float:
        self.probe()
        return 0.0


_database: LocalDatabase | None = None
_database_lock = threading.Lock()


def local_db_path(env: dict[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path((source.get(PATH_ENV) or DEFAULT_PATH).strip() or DEFAULT_PATH)


def get_database() -> LocalDatabase:
    global _database
    with _database_lock:
        if _database is None:
            _database = LocalDatabase(local_db_path())
            _database.ensure_schema()
        return _database


def set_database(database: LocalDatabase | None) -> None:
    global _database
    with _database_lock:
        _database = database


__all__ = [
    "BUSY_TIMEOUT_MS",
    "DEFAULT_PATH",
    "DatabaseUnavailable",
    "LocalDatabase",
    "PATH_ENV",
    "get_database",
    "local_db_path",
    "set_database",
]
