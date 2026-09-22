"""Setups and their events, on PostgreSQL (plan §10, §11, §12).

**One transaction moves a setup, and it is the shape §12 prescribes:**

    BEGIN
      SELECT ... FROM setups WHERE setup_id = ... FOR UPDATE   -- the lock
      the setup must exist
      the event must not already exist                          -- idempotence
      assert_setup_transition(current.state, event.to_state)    -- the gate
      INSERT the event
      UPDATE the setup
    COMMIT

**What changes from Firestore is not the logic but what the tests can prove.** The
Firestore repository had to check whether the injected client's ``transaction()`` was
Google's, and when it was not -- which was every unit test -- it ran the callable with **no
isolation at all**. Its own docstring said so, and said the isolation was only ever proved
in the emulator suite. Here ``FOR UPDATE`` is real in every test, so "two writers race and
one loses" is asserted on the same code path production runs (decision 350).

The lock is taken on the SETUP, not the event, and that ordering is the whole of the
correctness argument: two observers processing the same candle both block on the same row,
so exactly one of them evaluates the transition against the pre-move state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from aureon.models.base import to_utc, utc_now
from aureon.models.enums import (
    TERMINAL_SETUP_STATES,
    SetupState,
    assert_setup_transition,
)
from aureon.models.setup import Setup, SetupEvent
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


class MissingSetup(LookupError):
    """An event was recorded against a setup that does not exist."""


class SetupEventRepository(PostgresRepository):
    """Reads ``setup_events``. Writes go through ``SetupRepository.record``.

    Deliberately read-only: an event written without moving its setup would be a history
    that disagrees with the thing it is a history of, and the only way to make that
    impossible is for the write to live in the transaction that does both.
    """

    table = tables.SetupEvent.__table__

    def get(self, event_id: str) -> SetupEvent | None:
        row = self._row(event_id)
        return None if row is None else SetupEvent.model_validate(self._to_model_dict(row))

    def for_setup(self, setup_id: str, *, limit: int | None = None) -> list[SetupEvent]:
        """One setup's history, oldest first (§11's index).

        Oldest first because a timeline is read forwards; the card that shows the last few
        takes the tail. Ordered by ``created_at`` and then ``event_id`` so two events
        stamped in the same millisecond still come back in a stable order -- an unstable
        one would make the rendered timeline change between two reads of the same setup.
        """
        statement = (
            select(self.table)
            .where(self.table.c.setup_id == setup_id)
            .order_by(self.table.c.created_at, self.table.c.event_id)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return self._parse_all(self._rows(statement), SetupEvent, what="setup_event")

    def count_for_setup(self, setup_id: str) -> int:
        from sqlalchemy import func

        statement = select(func.count()).select_from(self.table).where(
            self.table.c.setup_id == setup_id
        )
        with self._db.connect() as connection:
            return int(connection.execute(statement).scalar_one())

    @staticmethod
    def _to_row(event: SetupEvent) -> dict[str, Any]:
        payload = event.model_dump(mode="json")
        return {
            "event_id": event.event_id,
            "schema_version": event.schema_version,
            "setup_id": event.setup_id,
            "event_type": event.event_type.value,
            "from_state": event.from_state.value,
            "to_state": event.to_state.value,
            "linked_detection_id": event.linked_detection_id,
            "market_time_utc": event.market_time.utc,
            "timezone": event.market_time.market_tz,
            "created_at": event.created_at,
            "context_snapshot": payload["context_snapshot"],
            "reason": event.reason,
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        return {
            "schema_version": data["schema_version"],
            "event_id": data["event_id"],
            "setup_id": data["setup_id"],
            "event_type": data["event_type"],
            "from_state": data["from_state"],
            "to_state": data["to_state"],
            "linked_detection_id": data["linked_detection_id"],
            "market_time": {
                "utc": data["market_time_utc"],
                "market_tz": data["timezone"],
            },
            "context_snapshot": data["context_snapshot"],
            "reason": data["reason"],
            "created_at": data["created_at"],
        }


class SetupRepository(PostgresRepository):
    """Reads, opens and moves ``setups``."""

    table = tables.Setup.__table__

    def __init__(self, database: Any) -> None:
        super().__init__(database)
        self.events = SetupEventRepository(database)

    # ── Opening ───────────────────────────────────────────────────────────────

    def open(self, setup: Setup) -> Setup:
        """Create a setup at its deterministic id, or return the one already there.

        Idempotent rather than raising, because the id is a pure function of what opened
        it: a re-processed candle produces the same setup, and refusing would turn a
        restart into an error the observer would have to special-case.
        """
        with self._db.transaction() as connection:
            existing = self._row(setup.setup_id, connection=connection)
            if existing is not None:
                return Setup.model_validate(self._to_model_dict(existing))
            self._insert_only(self._to_row(setup), connection=connection)
        return setup

    # ── The transaction (§12) ─────────────────────────────────────────────────

    def record(
        self,
        event: SetupEvent,
        *,
        linked_detection_id: str | None = None,
        invalidation_price: float | None = None,
        context_summary: Any | None = None,
        reference: Any | None = None,
        now: datetime | None = None,
    ) -> tuple[Setup, SetupEvent, bool]:
        """Append one event and move the setup, atomically. ``(setup, event, applied)``.

        ``applied`` is False when the event was already stored -- the idempotent path. A
        re-processed candle changes nothing and raises nothing.

        Everything a caller may want to change on the parent rides along as a keyword,
        because a separate update afterwards would be a second write outside the
        transaction, and a crash between the two would leave a setup whose state moved and
        whose invalidation price did not.
        """
        moment = to_utc(now or utc_now())

        with self._db.transaction() as connection:
            # The lock, and the ordering the whole correctness argument rests on: two
            # observers on the same candle both block here, so exactly one of them
            # evaluates the transition against the pre-move state.
            locked = self._locked_setup(event.setup_id, connection)

            stored = self.events._row(event.event_id, connection=connection)
            if stored is not None:
                # Idempotent: return what is there and do NOT re-apply the move. A version
                # that skipped only the event write would then run assert_transition from
                # the state the FIRST write produced, turning a restart into an error.
                if locked is None:
                    raise MissingSetup(
                        f"{event.setup_id}: the event exists and the setup does not"
                    )
                return (
                    Setup.model_validate(self._to_model_dict(locked)),
                    SetupEvent.model_validate(self.events._to_model_dict(stored)),
                    False,
                )

            if locked is None:
                raise MissingSetup(f"{event.setup_id}: no such setup; open it first")
            current = Setup.model_validate(self._to_model_dict(locked))

            if event.to_state is not current.state:
                # The gate every status write passes through (CLAUDE.md). A descriptive
                # event has to_state == state and never reaches this.
                assert_setup_transition(current.state, event.to_state)

            stored_event = event.model_copy(
                update={
                    "from_state": current.state,
                    "created_at": event.created_at or moment,
                }
            )
            moved = current.model_copy(
                update=self._parent_updates(
                    current,
                    stored_event,
                    moment,
                    linked_detection_id=linked_detection_id,
                    invalidation_price=invalidation_price,
                    context_summary=context_summary,
                    reference=reference,
                )
            )

            self.events._insert_only(
                self.events._to_row(stored_event), connection=connection
            )
            self._upsert(self._to_row(moved), connection=connection)
            return moved, stored_event, True

    def _locked_setup(self, setup_id: str, connection: Any) -> Any | None:
        """``SELECT ... FOR UPDATE``. The lock §12 requires.

        ``FOR UPDATE`` and not ``SKIP LOCKED``: a second writer on this setup must WAIT and
        then see the moved state, not skip past and conclude there was nothing to do.
        Skipping is right for the executor's claim, where any one of many requests will do;
        it is wrong here, where the two writers are about the same setup.
        """
        statement = (
            select(self.table).where(self.table.c.setup_id == setup_id).with_for_update()
        )
        return connection.execute(statement).mappings().first()

    @staticmethod
    def _parent_updates(
        current: Setup,
        event: SetupEvent,
        moment: datetime,
        *,
        linked_detection_id: str | None,
        invalidation_price: float | None,
        context_summary: Any | None,
        reference: Any | None,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {
            "state": event.to_state,
            "updated_at": moment,
            "last_event_id": event.event_id,
            "event_count": current.event_count + 1,
        }
        if linked_detection_id:
            updates["linked_detection_ids"] = current.with_linked(linked_detection_id)
        if invalidation_price is not None:
            updates["invalidation_price"] = invalidation_price
        if context_summary is not None:
            updates["context_summary"] = context_summary
        if reference is not None:
            updates["reference"] = reference
        if event.to_state is SetupState.CONFIRMED and current.confirmed_at is None:
            # First confirmation only. A setup that pulled back and confirmed again is the
            # same claim made once; re-stamping would move the moment a review measures
            # the outcome from (12 T-9).
            updates["confirmed_at"] = moment
        if event.to_state in TERMINAL_SETUP_STATES:
            updates["closed_at"] = moment
        return updates

    # ── Reading ───────────────────────────────────────────────────────────────

    def get(self, setup_id: str) -> Setup | None:
        row = self._row(setup_id)
        return None if row is None else Setup.model_validate(self._to_model_dict(row))

    def open_for_symbol(self, symbol: str) -> list[Setup]:
        """Setups on this symbol that have not reached a terminal state (§10's index)."""
        terminal = [state.value for state in TERMINAL_SETUP_STATES]
        statement = (
            select(self.table)
            .where(self.table.c.symbol == symbol)
            .where(self.table.c.state.notin_(terminal))
            .order_by(self.table.c.updated_at.desc())
        )
        return self._parse_all(self._rows(statement), Setup, what="setup")

    def in_period(self, start: datetime, end: datetime) -> list[Setup]:
        """Setups OPENED in ``[start, end)``. What a weekly review counts.

        Keyed on ``opened_at`` rather than ``updated_at``: a setup belongs to the period it
        appeared in, and one that was still moving a week later must not migrate into the
        next review and out of the one that first saw it.
        """
        statement = (
            select(self.table)
            .where(self.table.c.opened_at >= to_utc(start))
            .where(self.table.c.opened_at < to_utc(end))
            .order_by(self.table.c.opened_at)
        )
        return self._parse_all(self._rows(statement), Setup, what="setup")

    def changed_since(self, moment: datetime) -> list[Setup]:
        """Setups touched since an instant, for the notifier's sweep (12 T-11)."""
        statement = (
            select(self.table)
            .where(self.table.c.updated_at >= to_utc(moment))
            .order_by(self.table.c.updated_at)
        )
        return self._parse_all(self._rows(statement), Setup, what="setup")

    def for_market_date(self, symbol: str, market_date: str) -> list[Setup]:
        statement = (
            select(self.table)
            .where(self.table.c.symbol == symbol)
            .where(self.table.c.market_date == market_date)
            .order_by(self.table.c.opened_at)
        )
        return self._parse_all(self._rows(statement), Setup, what="setup")

    # ── Mapping ───────────────────────────────────────────────────────────────

    @staticmethod
    def _to_row(setup: Setup) -> dict[str, Any]:
        payload = setup.model_dump(mode="json")
        return {
            "setup_id": setup.setup_id,
            "schema_version": setup.schema_version,
            "account_scope": setup.account_scope,
            "symbol": setup.symbol,
            "timeframe": setup.timeframe.value,
            "family": setup.family.value,
            "direction_context": setup.direction_context.value,
            "state": setup.state.value,
            "market_date": setup.market_date,
            "invalidation_price": setup.invalidation_price,
            "opened_at": setup.opened_at,
            "updated_at": setup.updated_at,
            "confirmed_at": setup.confirmed_at,
            "closed_at": setup.closed_at,
            "last_event_id": setup.last_event_id,
            "event_count": setup.event_count,
            "setup_version": setup.setup_version,
            "anchor": payload["anchor"],
            # A tuple on the model, an array inside an object here: JSONB's top level is
            # typed, and a bare array would make the column's shape depend on its content.
            "linked_detection_ids": {"items": list(setup.linked_detection_ids)},
            "context_summary": payload["context_summary"],
            "reference": payload.get("reference"),
            "params_snapshot": payload["params_snapshot"],
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        return {
            "schema_version": data["schema_version"],
            "setup_id": data["setup_id"],
            "account_scope": data["account_scope"],
            "symbol": data["symbol"],
            "timeframe": data["timeframe"],
            "family": data["family"],
            "direction_context": data["direction_context"],
            "state": data["state"],
            "market_date": data["market_date"],
            "anchor": data["anchor"],
            "invalidation_price": data["invalidation_price"],
            "opened_at": data["opened_at"],
            "updated_at": data["updated_at"],
            "confirmed_at": data["confirmed_at"],
            "closed_at": data["closed_at"],
            "last_event_id": data["last_event_id"],
            "event_count": data["event_count"],
            "linked_detection_ids": data["linked_detection_ids"]["items"],
            "context_summary": data["context_summary"],
            "reference": data["reference"],
            "setup_version": data["setup_version"],
            "params_snapshot": data["params_snapshot"],
        }
