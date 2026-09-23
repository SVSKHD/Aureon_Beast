"""What every repository shares, and the two rules none of them may break.

**A repository holds a ``Database``, not a connection.** Connections come from it per
operation, so a repository is cheap to construct, safe to keep on a service for the
process's lifetime, and cannot hold a transaction open across calls -- which is how a
long-lived object ends up blocking the executor's claim.

**A write is an upsert at a deterministic id, never an insert-if-absent.** §12's ids are
pure functions of the candle that produced them, and the outbox may deliver the same
payload twice after an ambiguous first attempt. ``ON CONFLICT DO UPDATE`` makes the retry
idempotent at the database rather than relying on every caller to check first -- and a
check-then-insert would be a race between the check and the insert anyway.

Reads return domain models. The mapping lives in each repository, because the direction
that matters -- row to model -- is where a column rename has to be noticed, and a generic
mapper would turn that into a silent ``None``.
"""

from __future__ import annotations

import logging
import zlib
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Table, text

from aureon.models.base import to_utc
from aureon.storage.postgres.database import Database

log = logging.getLogger(__name__)

T = TypeVar("T")


#: The first half of every advisory lock key this package takes, so a lock of ours can never
#: collide with one taken by something else sharing the database. 0x4155 is "AU".
ADVISORY_NAMESPACE = 0x4155


def advisory_key(name: str) -> int:
    """A stable ``int4`` for one lock name.

    A pure function of the name rather than a number somebody chose, so a second lock is
    added by naming it. CRC32 shifted into the signed range, because that is what
    ``pg_advisory_xact_lock(int4, int4)`` takes; a collision between two of our own names
    would cost one unnecessary wait, never a missed lock.
    """
    return zlib.crc32(name.encode()) - 2**31


class RepositoryError(RuntimeError):
    """A write that could not be performed for a reason the caller should see.

    Distinct from ``DatabaseUnavailable``: that one means the database did not answer, and
    §27 fails the money path closed on it. This one means the database answered and said
    no, which is a different conversation.
    """


class PostgresRepository:
    """Base for every repository. Holds the database and the table it owns."""

    #: Set by each subclass. Named rather than passed so a repository cannot be pointed at
    #: the wrong table by a caller.
    table: Table

    def __init__(self, database: Database) -> None:
        self._db = database

    @property
    def database(self) -> Database:
        """The backing database.

        Exposed because the services that coordinate two repositories in ONE transaction
        -- the setup engine writing a setup and its event, the executor claiming and
        auditing -- need the shared boundary, and reaching into a private attribute to get
        it is the thing the boundary tests already forbid.
        """
        return self._db

    # ── Writing ───────────────────────────────────────────────────────────────

    def _upsert(
        self,
        values: dict[str, Any],
        *,
        key: Sequence[str] | None = None,
        connection: Any = None,
        table: Table | None = None,
    ) -> None:
        """``INSERT ... ON CONFLICT (pk) DO UPDATE``. Safe to repeat, byte for byte.

        ``connection`` is passed when the write belongs to a transaction the caller is
        already running -- §12's setup write and §15's claim both do several writes that
        must commit together or not at all. Left ``None``, the write gets its own
        transaction.

        ``table`` names a second table the repository owns, for the one case where a
        repository legitimately owns two: ``ReviewRepository`` writes ``daily_reviews`` and
        ``weekly_reviews``, which are one concept keyed two ways. Passing the table beats
        re-deriving the ON CONFLICT clause in the subclass, because a second copy of it is a
        second thing that can stop being an upsert.
        """
        target = self.table if table is None else table
        columns = [c.name for c in target.primary_key.columns] if key is None else list(key)
        dialect = self._db.engine.dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            from sqlalchemy.dialects.postgresql import insert
        statement = insert(target).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=columns,
            set_={name: statement.excluded[name] for name in values if name not in columns},
        )
        if connection is not None:
            connection.execute(statement)
            return
        with self._db.transaction() as own:
            own.execute(statement)

    def _insert_only(self, values: dict[str, Any], *, connection: Any = None) -> None:
        """``INSERT``, raising on a duplicate.

        For records that are facts rather than states: an audit row, a setup event. A
        second one at the same id is not a retry to absorb, it is a bug or a replay, and
        overwriting it would destroy the first.
        """
        statement = self.table.insert().values(**values)
        if connection is not None:
            connection.execute(statement)
            return
        with self._db.transaction() as own:
            own.execute(statement)

    def _advisory_lock(self, connection: Any, name: str) -> None:
        """Take a transaction-scoped advisory lock named ``name``.

        For the writes a ROW lock cannot protect, and there are two of them: a settings
        toggle on a deployment that has no settings row yet, and a per-user alert cap whose
        count must not miss a row another transaction is inserting. ``SELECT ... FOR
        UPDATE`` locks the rows it finds; it cannot lock a row that does not exist yet, and
        under READ COMMITTED a blocked scan does not see the inserts the transaction it
        waited for made (decision 359). Both were measured, and both let two writers
        through before this existed.

        Released by the commit or the rollback, so there is no path that leaks one.
        """
        if self._db.engine.dialect.name == "sqlite":
            # SQLite's BEGIN IMMEDIATE serialises writers for the duration of this short
            # transaction. There is no advisory-lock primitive to take in addition.
            return
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
            {"namespace": ADVISORY_NAMESPACE, "key": advisory_key(name)},
        )

    # ── Reading ───────────────────────────────────────────────────────────────

    def _row(
        self, identifier: str, *, connection: Any = None, table: Table | None = None
    ) -> Any | None:
        """One row by primary key, or ``None``. ``table`` as in ``_upsert``."""
        from sqlalchemy import select

        target = self.table if table is None else table
        column = list(target.primary_key.columns)[0]
        statement = select(target).where(column == identifier)
        if connection is not None:
            return connection.execute(statement).mappings().first()
        with self._db.connect() as own:
            return own.execute(statement).mappings().first()

    def _rows(self, statement: Any, *, connection: Any = None) -> list[Any]:
        if connection is not None:
            return list(connection.execute(statement).mappings().all())
        with self._db.connect() as own:
            return list(own.execute(statement).mappings().all())

    def exists(self, identifier: str) -> bool:
        return self._row(identifier) is not None

    # ── Mapping helpers ───────────────────────────────────────────────────────

    def _parse_all(self, rows: Iterable[Any], model: type[T], *, what: str) -> list[T]:
        """Validate every row, logging and skipping the ones that will not parse.

        One unreadable row must not lose a whole period: a review that raised on a single
        malformed record would report nothing at all, which reads as "a quiet day" rather
        than as a fault. The same choice the Firestore repositories made, for the same
        reason.
        """
        parsed: list[T] = []
        for row in rows:
            try:
                parsed.append(model.model_validate(self._to_model_dict(row)))
            except Exception:  # noqa: BLE001 - one bad row must not lose the rest
                log.exception("unreadable %s row", what)
        return parsed

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        """A row mapping as a plain dict. Overridden where columns and fields differ."""
        return dict(row)

    @staticmethod
    def _market_time(moment: datetime, timezone: str) -> dict[str, Any]:
        """Rebuild a ``MarketTime`` from the two columns that store one.

        The rendering is NOT stored (decision 341), so it is reconstructed here from the
        instant and the zone -- which is the only place the two can be recombined without
        a chance of them disagreeing.
        """
        return {"utc": to_utc(moment), "market_tz": timezone}
