"""Heartbeats, per-symbol state, settings and symbol specs (plan §20-§22, §24).

Four small tables with one thing in common: each row is the CURRENT answer to a question,
not a record of an event. So every write here is an upsert at a known key, and three of the
four are not audited -- a heartbeat that changed is not news, and auditing one would bury
the transitions that matter under thousands of rows a day.

**``settings`` is the exception, and it is the important one.** ``trading_enabled`` lives
here and defaults to FALSE (§22), so a fresh deployment cannot trade until a human enables
it. ``set_trading_enabled`` writes the row and its ``AuditRecord`` in ONE transaction under
a row lock (§57, §60, §71): the kill switch is the one setting where a half-written change
is worse than a failed one, and where two toggles racing must not resolve by whichever
landed last.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from aureon.models.audit import AuditRecord
from aureon.models.base import to_utc, utc_now
from aureon.models.market import SymbolInfo
from aureon.models.settings import ExecutionSettings, NotificationSettings
from aureon.models.system import Heartbeat, SystemState
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.audit import AuditRepository
from aureon.storage.postgres.repositories.base import PostgresRepository

#: The two rows ``settings`` holds (decision 11). Re-exported from ``tables`` rather than
#: spelled here, so a typo is an AttributeError instead of a silently-created third settings
#: row that nothing ever reads -- and so the name has one definition, next to the table.
EXECUTION_SETTINGS = tables.EXECUTION_SETTINGS_ROW
NOTIFICATION_SETTINGS = tables.NOTIFICATION_SETTINGS_ROW


class SettingsVersionConflict(RuntimeError):
    """A caller asserted a settings version that is no longer current."""


def audit_id_for_settings(version: int) -> str:
    """Deterministic audit id for one settings version (§60).

    Deterministic so a retried call re-writes the same row instead of leaving two audit
    entries for one toggle -- which would make the trail claim two decisions where a
    human made one. ``_insert_only`` then turns the second attempt into a refusal
    rather than a silent duplicate.
    """
    return f"settings-execution-v{version:08d}"


def system_state_id(symbol: str, timeframe: Any) -> str:
    """``XAUUSD_M5``. One row per symbol and timeframe (§21, 9A).

    Per symbol rather than one row for everything: the write throttle is per row, so gold's
    candle close would otherwise suppress silver's state for the next few seconds, and a
    reader wanting one symbol should not have to read every symbol to get it.
    """
    frame = getattr(timeframe, "value", timeframe)
    if not symbol or not frame:
        raise ValueError(f"symbol and timeframe are both required, got {symbol!r} {frame!r}")
    return f"{symbol}_{frame}"


class HeartbeatRepository(PostgresRepository):
    """§67, decision 10. One row per service -- the source of truth for liveness.

    The copy embedded in ``system_state`` is derived for one-read status views and is
    never written independently.

    **Throttled, and the throttle stays.** It existed on Firestore to save writes that cost
    money; here a write costs nothing, but a service beating on every tick would still push
    a row's worth of WAL per tick and make the table's autovacuum work for no new
    information. ``force=True`` is what a candle close uses.
    """

    table = tables.Heartbeat.__table__

    def __init__(self, database: Any, *, min_interval_seconds: float = 5.0) -> None:
        super().__init__(database)
        self.min_interval_seconds = min_interval_seconds
        self._last: dict[str, datetime] = {}

    def beat(
        self,
        service: str,
        *,
        instance_id: str | None = None,
        detail: dict[str, Any] | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> bool:
        """Record a heartbeat, throttled. Returns True if written."""
        moment = to_utc(now or utc_now())
        previous = self._last.get(service)
        if not force and previous is not None:
            if (moment - previous).total_seconds() < self.min_interval_seconds:
                return False

        heartbeat = Heartbeat(
            service=service,
            updated_at=moment,
            instance_id=instance_id,
            detail=detail or {},
        )
        self._upsert(self._to_row(heartbeat))
        self._last[service] = moment
        return True

    def read(self, service: str) -> Heartbeat | None:
        row = self._row(service)
        return None if row is None else Heartbeat.model_validate(dict(row))

    def read_all(self) -> dict[str, Heartbeat]:
        """Every service's heartbeat, for ``/status`` and the dashboard.

        One query rather than the Firestore version's read per known service name: the
        table is the list, so a service added without touching ``paths.SERVICES`` still
        appears instead of being silently invisible.
        """
        found = self._parse_all(self._rows(select(self.table)), Heartbeat, what="heartbeat")
        return {record.service: record for record in found}

    @staticmethod
    def _to_row(record: Heartbeat) -> dict[str, Any]:
        return {
            "service": record.service,
            "schema_version": record.schema_version,
            "updated_at": record.updated_at,
            "instance_id": record.instance_id,
            "detail": record.model_dump(mode="json")["detail"],
        }


class SystemStateRepository(PostgresRepository):
    """§21, §59, 9A. The live snapshot, one row per (symbol, timeframe).

    One row per symbol, not one for everything, and the reason only appears with a second
    symbol: the write throttle is per row. With a shared row, gold's candle close resets
    the timer and silver's state is suppressed for the next few seconds -- so the symbol a
    reader is looking at can be stale because of a symbol they are not.

    Each row carries the global fields (heartbeats, trading_enabled) as well, so a reader
    of one symbol needs one read. Those are derived copies of rows that exist in their own
    right (``heartbeats`` is the source of truth, decision 10, and so is ``settings``), so
    duplicating them costs nothing and is never the truth anybody depends on.

    Almost everything is read back whole, so the body is JSONB and the four columns are
    what something filters or orders on.
    """

    table = tables.SymbolStateRow.__table__

    def __init__(self, database: Any, *, min_interval_seconds: float = 5.0) -> None:
        super().__init__(database)
        self.min_interval_seconds = min_interval_seconds
        #: (symbol, timeframe) -> last write. Per symbol, which is the whole point.
        self._last_writes: dict[tuple[str, str], datetime] = {}

    def write(
        self, state: SystemState, *, force: bool = False, now: datetime | None = None
    ) -> bool:
        """Write one row per symbol. True if ANY was written.

        ``force=True`` bypasses the throttle and is what a candle close uses: a new closed
        candle is genuinely new information and must not wait for a timer.

        A state carrying **no** symbols writes nothing and returns False. It has nothing to
        say that is not already a row of its own: its only other fields are derived copies
        of the heartbeat and settings rows.
        """
        moment = to_utc(now or utc_now())
        written = False
        for symbol_state in state.symbols:
            key = (symbol_state.symbol, symbol_state.timeframe.value)
            last = self._last_writes.get(key)
            if (
                not force
                and last is not None
                and (moment - last).total_seconds() < self.min_interval_seconds
            ):
                continue
            # The row holds exactly its own symbol. A reader of XAGUSD_M5 must not find
            # XAUUSD's panel inside it and have to work out which one is current.
            one = state.model_copy(update={"updated_at": moment, "symbols": (symbol_state,)})
            self._upsert(
                {
                    "id": system_state_id(symbol_state.symbol, symbol_state.timeframe),
                    "schema_version": one.schema_version,
                    "symbol": symbol_state.symbol,
                    "timeframe": symbol_state.timeframe.value,
                    "market_state": symbol_state.market_state.value,
                    "updated_at": moment,
                    "state": one.model_dump(mode="json"),
                }
            )
            self._last_writes[key] = moment
            written = True
        return written

    def read_symbol(self, symbol: str, timeframe: Any) -> SystemState | None:
        """One symbol's row, in one read. What ``/status symbol:X`` uses."""
        row = self._row(system_state_id(symbol, timeframe))
        return None if row is None else SystemState.model_validate(row["state"])

    def read(self) -> SystemState | None:
        """Every symbol, merged into one ``SystemState``.

        Kept so callers that want "whatever the observer knows" -- the quote lookups on
        Discord's confirmation screens -- are unchanged by the split. ``updated_at`` is the
        NEWEST of the rows read, and the global fields come from that same newest one: a
        merged freshness that took the oldest would make a quiet symbol look like a dead
        observer.
        """
        found = self._parse_all(
            self._rows(select(self.table).order_by(self.table.c.id)),
            SystemState,
            what=self.table.name,
        )
        if not found:
            return None
        newest = max(found, key=lambda one: one.updated_at)
        symbols = tuple(
            sorted(
                (s for state in found for s in state.symbols),
                key=lambda s: (s.symbol, s.timeframe.value),
            )
        )
        return newest.model_copy(update={"symbols": symbols})

    def updated_at(self, symbol: str, timeframe: Any) -> datetime | None:
        """When this row was last written -- the input to a staleness check (§59).

        A column rather than a field inside the snapshot, because "how old is this" has to
        be answerable without deserialising the whole state, and because it is the value a
        RESTARTED process needs: ``last_write`` below only knows about this process's own
        writes.
        """
        row = self._row(system_state_id(symbol, timeframe))
        return None if row is None else row["updated_at"]

    @property
    def last_write(self) -> datetime | None:
        """The most recent write across every symbol, by THIS process."""
        return max(self._last_writes.values()) if self._last_writes else None

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        return dict(row["state"])


class ExecutionSettingsRepository(PostgresRepository):
    """The single ``settings`` row named ``execution`` (§22, §56, §57, decision 11).

    ``trading_enabled`` is here and not in the environment on purpose: it is the switch a
    human flips in Discord in a hurry. The live-account gate is the OTHER layer and stays
    an environment variable, because that one is a line somebody wrote on the machine while
    looking at which terminal was open. Collapsing the two would defeat both (§23).
    """

    table = tables.Setting.__table__

    #: Derived from the table, not spelled, for decision 356's reason: an audit trail that
    #: names a table the rows are not in is a trail nobody can follow.
    COLLECTION = tables.Setting.__tablename__

    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self.audit = AuditRepository(database)

    def read(self) -> ExecutionSettings | None:
        row = self._row(EXECUTION_SETTINGS)
        return None if row is None else ExecutionSettings.model_validate(row["value"])

    def read_or_default(self) -> ExecutionSettings:
        """Settings, or the fail-closed default if they cannot be read.

        Defaults rather than ``None`` or an exception: a deployment that has never written
        settings must still have a complete, safe answer -- and ``ExecutionSettings``
        defaults ``trading_enabled`` to False, so "no settings yet" means "cannot trade".
        The opposite default would turn an absent row into an open trading bot.

        A read that RAISES is a different matter and does not pass through here. §27 fails
        the money path closed on a database that did not answer, and swallowing
        ``DatabaseUnavailable`` into "trading is off" would hide an outage behind a setting
        (S-5 is where that path is built and tested).
        """
        return self.read() or ExecutionSettings()

    def write(self, settings: ExecutionSettings) -> ExecutionSettings:
        """Store settings as given. Does NOT audit -- ``set_trading_enabled`` does."""
        self._upsert(_settings_row(EXECUTION_SETTINGS, settings, settings.settings_version))
        return settings

    def set_trading_enabled(
        self,
        enabled: bool,
        *,
        actor: str,
        reason: str | None = None,
        now: datetime | None = None,
        if_version: int | None = None,
    ) -> ExecutionSettings:
        """Flip the trading switch and audit it, in ONE transaction (§57, §60, §71).

        This is the kill switch, so a write that half-happens is worse than one that fails.

        * **One transaction.** The settings write and its ``AuditRecord`` commit together.
          Separately, the switch could flip with no audit row -- a gap in the §60 trail on
          the one setting that decides whether real money can move -- or an audit row could
          land for a write that failed, which is worse, because it claims something
          happened that did not.
        * **A version check under a row lock.** The row is read ``FOR UPDATE``, so two
          concurrent toggles serialise and the second sees the first's version. Without it,
          a disable racing an enable is last-write-wins, and the direction that loses might
          be the safe one.

        ``if_version`` lets a caller assert what it believed it was changing. Passing a
        stale value raises ``SettingsVersionConflict`` rather than overwriting -- for a UI
        that read the settings, rendered a button, and got a press seconds later.
        """
        moment = to_utc(now or utc_now())
        with self._db.transaction() as connection:
            row = self._locked(connection)
            # Fail-closed default, same as read_or_default: an absent row must not read as
            # "trading was already on".
            current = (
                ExecutionSettings() if row is None
                else ExecutionSettings.model_validate(row["value"])
            )

            if if_version is not None and current.settings_version != if_version:
                raise SettingsVersionConflict(
                    f"settings are at version {current.settings_version}, not "
                    f"{if_version}; something changed them since you read them"
                )

            updated = current.model_copy(
                update={
                    "trading_enabled": enabled,
                    "settings_version": current.settings_version + 1,
                    "updated_at": moment,
                    "updated_by": actor,
                    "disabled_reason": None if enabled else reason,
                }
            )
            self._upsert(
                _settings_row(EXECUTION_SETTINGS, updated, updated.settings_version),
                connection=connection,
            )
            self.audit.append(
                AuditRecord(
                    # Deterministic, not random: the id is a function of what changed, so a
                    # caller that retries the whole call after an ambiguous failure writes
                    # the same audit row rather than leaving two for one human decision.
                    audit_id=audit_id_for_settings(updated.settings_version),
                    at=moment,
                    actor=actor,
                    action="trading.enable" if enabled else "trading.disable",
                    collection=self.COLLECTION,
                    document_id=EXECUTION_SETTINGS,
                    from_status=str(current.trading_enabled),
                    to_status=str(enabled),
                    reason=reason,
                ),
                connection=connection,
            )
            return updated

    def _locked(self, connection: Any) -> Any | None:
        """Take the settings lock, then read the row. May legitimately return ``None``.

        An **advisory** lock and not ``SELECT ... FOR UPDATE``, because on a fresh
        deployment there is no row to lock and ``FOR UPDATE`` over zero rows locks nothing
        (decision 359). Measured: two first-ever toggles both saw no row, both computed
        version 1, and the only thing that stopped the second from silently replacing the
        first was the audit insert colliding on its deterministic id. A safety net is not
        a lock.

        ``pg_advisory_xact_lock`` does not care whether the row exists, and it is released
        by the commit or the rollback either way. Once it is held, a plain ``SELECT`` reads
        the state the previous holder committed, so ``FOR UPDATE`` here would add a second
        mechanism that only works in the case the first already covers.
        """
        self._advisory_lock(connection, f"settings:{EXECUTION_SETTINGS}")
        statement = select(self.table).where(self.table.c.name == EXECUTION_SETTINGS)
        return connection.execute(statement).mappings().first()


class NotificationSettingsRepository(PostgresRepository):
    """The ``notifications`` settings row -- which agents Discord announces (9C).

    Its own class rather than a method on ``ExecutionSettingsRepository`` because the two
    rows have different blast radii: one decides whether real money can move, the other
    whether a message is posted. Holding them apart means a surface that only needs the
    second cannot reach the first.
    """

    table = tables.Setting.__table__

    def read_or_default(self) -> NotificationSettings:
        """The stored settings, or the shipped defaults.

        Defaults rather than silence on a missing row: a deployment that has never written
        this should still announce the four obvious agents, and an operator who wants
        silence has ``detections_enabled``.
        """
        row = self._row(NOTIFICATION_SETTINGS)
        return (
            NotificationSettings()
            if row is None
            else NotificationSettings.model_validate(row["value"])
        )

    def write(self, settings: NotificationSettings) -> NotificationSettings:
        self._upsert(_settings_row(NOTIFICATION_SETTINGS, settings, 1))
        return settings


def _settings_row(name: str, settings: Any, version: int) -> dict[str, Any]:
    return {
        "name": name,
        "schema_version": getattr(settings, "schema_version", 1),
        "settings_version": version,
        "updated_at": settings.updated_at or utc_now(),
        "updated_by": settings.updated_by,
        "value": settings.model_dump(mode="json"),
    }


class SymbolRepository(PostgresRepository):
    """Decision 79. Broker metadata published for Discord.

    A convenience copy and never an authority: the execution guard re-reads the live symbol
    at execution time, because a stored ``stops_level`` that went stale would be used to
    validate an order the broker then rejects.
    """

    table = tables.SymbolSpec.__table__

    def publish(self, info: SymbolInfo, *, now: datetime | None = None) -> str:
        """Store one symbol's spec, returning its row id."""
        self._upsert(
            {
                "symbol": info.symbol,
                "schema_version": 1,
                "updated_at": to_utc(now or utc_now()),
                "spec": info.model_dump(mode="json"),
            }
        )
        return info.symbol

    def get(self, symbol: str) -> SymbolInfo | None:
        row = self._row(symbol)
        return None if row is None else SymbolInfo.model_validate(row["spec"])

    def published_at(self, symbol: str) -> datetime | None:
        """When this spec was last published -- how a reader judges whether it is stale.

        A column rather than a field inside the spec, so staleness is answerable without
        deserialising, and so it cannot disagree with the row it describes.
        """
        row = self._row(symbol)
        return None if row is None else row["updated_at"]
