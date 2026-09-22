"""Ops events, assessments and trade notes (11A F-15, 9D).

Three tables that record what the system noticed, what the measured history says, and what
a trader thought. Nothing here moves money, and nothing here is on the money path. Price
alerts moved to ``alerts.py``: they have real transitions and two writers racing over them,
which is a different kind of module from this one.

**One rule spans them and is worth stating once.** ``assessments`` carries ``history_source``
and ``real_days`` as COLUMNS rather than inside a payload, because C-13 requires code that
treats synthetic history as not-real to be able to FILTER on them. A provenance flag readable
only after deserialising the row is one that gets forgotten -- and a hit rate measured over a
generated random walk is a statement about ``scripts/gen_fixtures.py``, not about gold.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from aureon.models.assessment import Assessment, TradeNote
from aureon.models.base import to_utc, utc_now
from aureon.models.enums import HistorySource
from aureon.models.identity import new_note_id
from aureon.models.ops import OpsEvent
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


def ops_event_id(name: str, scope: str | None = None) -> str:
    """``{name}`` or ``{name}__{scope}`` (11A, F-15).

    Derived from what the condition is ABOUT rather than generated, so a service that
    restarts mid-condition finds the existing row and does not re-announce -- which is the
    difference between one alert and one per restart.
    """
    if not name:
        raise ValueError("name must not be empty")
    return f"{name}__{scope.upper()}" if scope else name


class OpsEventRepository(PostgresRepository):
    """11A F-15. A named condition, and whether it is currently true."""

    table = tables.OpsEvent.__table__

    def write(self, event: OpsEvent, *, now: datetime | None = None) -> OpsEvent:
        """Upsert a condition at the id DERIVED from its name and scope.

        Derived rather than taken from ``event.event_id``, which is what the Firestore
        version did and is the only thing that makes ``get(name, scope)`` able to find what
        ``write`` stored: a model carrying an id that disagrees with its own name would
        otherwise write a row nothing can read back.

        ``onsets`` is carried on the model rather than incremented here: the service that
        decides a condition has newly become true is the one that knows, and a counter
        incremented by the storage layer would count re-announcements as recurrences.
        """
        stamped = event.model_copy(update={"updated_at": to_utc(now or utc_now())})
        self._upsert(self._to_row(stamped))
        return stamped

    def get(self, name: str, scope: str | None = None) -> OpsEvent | None:
        row = self._row(ops_event_id(name, scope))
        return None if row is None else OpsEvent.model_validate(self._to_model_dict(row))

    def active(self) -> list[OpsEvent]:
        """Everything currently true, newest first. What ``/ops`` renders."""
        statement = (
            select(self.table)
            .where(self.table.c.active.is_(True))
            .order_by(self.table.c.updated_at.desc())
        )
        return self._parse_all(self._rows(statement), OpsEvent, what="ops_event")

    def all_events(self) -> list[OpsEvent]:
        """Every condition ever recorded, active first, then by name.

        Cleared conditions are kept and shown rather than deleted: "this has not happened
        since the deployment started" and "this happened twice this morning and cleared"
        are different things for an operator to know, and a table holding only active rows
        cannot tell them apart.
        """
        statement = select(self.table).order_by(
            self.table.c.active.desc(), self.table.c.name, self.table.c.scope
        )
        return self._parse_all(self._rows(statement), OpsEvent, what="ops_event")

    @staticmethod
    def _to_row(event: OpsEvent) -> dict[str, Any]:
        return {
            "event_id": ops_event_id(event.name, event.scope),
            "schema_version": event.schema_version,
            "name": event.name,
            "scope": event.scope,
            "service": event.service,
            "active": event.active,
            "since": event.since,
            "updated_at": event.updated_at,
            "onsets": event.onsets,
            "detail": event.detail,
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        return dict(row)


class AssessmentRepository(PostgresRepository):
    """9D. A measured-only readout. Never a prediction."""

    table = tables.Assessment.__table__

    def store(self, assessment: Assessment) -> Assessment:
        self._upsert(self._to_row(assessment))
        return assessment

    def get(self, assessment_id: str) -> Assessment | None:
        row = self._row(assessment_id)
        return None if row is None else Assessment.model_validate(self._to_model_dict(row))

    def for_detection(self, detection_id: str) -> list[Assessment]:
        """Every readout ever produced for one detection, oldest first."""
        statement = (
            select(self.table)
            .where(self.table.c.detection_id == detection_id)
            .order_by(self.table.c.created_at, self.table.c.assessment_id)
        )
        return self._parse_all(self._rows(statement), Assessment, what="assessment")

    def latest_for_detection(self, detection_id: str) -> Assessment | None:
        found = self.for_detection(detection_id)
        return found[-1] if found else None

    def latest_for_symbol(self, symbol: str) -> Assessment | None:
        """The most recent readout for a symbol, for ``/execute``'s info line (9D-4).

        ``ORDER BY created_at DESC LIMIT 1`` rather than the Firestore version's read of
        every row for the symbol followed by a sort in Python. Same answer; one row.
        """
        statement = (
            select(self.table)
            .where(self.table.c.symbol == symbol)
            .order_by(self.table.c.created_at.desc(), self.table.c.assessment_id.desc())
            .limit(1)
        )
        found = self._parse_all(self._rows(statement), Assessment, what="assessment")
        return found[0] if found else None

    def in_period(
        self, start: datetime, end: datetime, *, symbol: str | None = None
    ) -> list[Assessment]:
        statement = (
            select(self.table)
            .where(self.table.c.created_at >= to_utc(start))
            .where(self.table.c.created_at < to_utc(end))
        )
        if symbol is not None:
            statement = statement.where(self.table.c.symbol == symbol)
        statement = statement.order_by(self.table.c.created_at)
        return self._parse_all(self._rows(statement), Assessment, what="assessment")

    def real_history_only(self, start: datetime, end: datetime) -> list[Assessment]:
        """C-13. Assessments whose cohort was measured over REAL broker bars.

        A filter rather than a post-read check, and that is the whole reason
        ``history_source`` is a column: a readout built on replayed fixture bars and one built
        on bars a broker served are the same arithmetic over incomparable data. Anything not
        explicitly REAL is excluded, which includes the ``unknown`` the Firestore export
        stamps on rows that predate the field (C-13) -- "nobody recorded it" is not evidence.
        """
        statement = (
            select(self.table)
            .where(self.table.c.created_at >= to_utc(start))
            .where(self.table.c.created_at < to_utc(end))
            .where(self.table.c.history_source == HistorySource.REAL.value)
            .order_by(self.table.c.created_at)
        )
        return self._parse_all(self._rows(statement), Assessment, what="assessment")

    @staticmethod
    def _to_row(assessment: Assessment) -> dict[str, Any]:
        payload = assessment.model_dump(mode="json")
        return {
            "assessment_id": assessment.assessment_id,
            "schema_version": assessment.schema_version,
            "detection_id": assessment.detection_id,
            "symbol": assessment.symbol,
            "rule_id": assessment.rule_id,
            "n": assessment.n,
            "insufficient": assessment.insufficient,
            "disagrees_with_detection": assessment.disagrees_with_detection,
            # C-13: columns, so code that must treat synthetic history as not-real can filter.
            "history_source": (
                assessment.history_source.value if assessment.history_source else None
            ),
            "real_days": assessment.real_days,
            "created_at": assessment.created_at,
            "trend_read": payload["trend_read"],
            "cohort_filter": payload["cohort_filter"],
            "confirmations": {"items": payload["confirmations"]},
            "tp_estimates": {"items": payload["tp_estimates"]},
            "sl_estimates": {"items": payload["sl_estimates"]},
            "paired": payload.get("paired"),
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        for key in ("confirmations", "tp_estimates", "sl_estimates"):
            data[key] = data[key]["items"]
        return data


class TradeNoteRepository(PostgresRepository):
    """9D. A trader's own words about a trade.

    Its own table rather than a child of the trade, so a note cannot be mistaken for a field
    write on a CLOSED trade (§58) by anything that iterates a record's parts. The assessment
    service is forbidden from reading these at all, and a boundary test says so: a note is a
    human's opinion, and measured statistics must not be conditioned on one.
    """

    table = tables.TradeNote.__table__

    def add(
        self,
        trade_id: str,
        *,
        author: str,
        text: str,
        now: datetime | None = None,
    ) -> TradeNote:
        """Record one note. Never touches the trade row (§58)."""
        note = TradeNote(
            note_id=new_note_id(),
            trade_id=trade_id,
            author=str(author),
            text=text.strip(),
            at=to_utc(now or utc_now()),
        )
        self._upsert(self._to_row(note))
        return note

    def store(self, note: TradeNote) -> TradeNote:
        """Write a note built by the caller. For a replay or an import, not for ``/note``."""
        self._upsert(self._to_row(note))
        return note

    def get(self, note_id: str) -> TradeNote | None:
        row = self._row(note_id)
        return None if row is None else TradeNote.model_validate(dict(row))

    def for_trade(self, trade_id: str) -> list[TradeNote]:
        statement = (
            select(self.table)
            .where(self.table.c.trade_id == trade_id)
            .order_by(self.table.c.at)
        )
        return self._parse_all(self._rows(statement), TradeNote, what="trade_note")

    def for_trades(self, trade_ids: list[str]) -> dict[str, list[TradeNote]]:
        """Notes for a period's trades, grouped by trade.

        One query rather than the Firestore version's read per trade: every trade id is in
        the same ``IN``, and a trade with no notes still gets its empty list, so a caller
        that iterates the mapping does not have to know which ones were missing.
        """
        if not trade_ids:
            return {}
        statement = (
            select(self.table)
            .where(self.table.c.trade_id.in_(trade_ids))
            .order_by(self.table.c.at, self.table.c.note_id)
        )
        grouped: dict[str, list[TradeNote]] = {trade_id: [] for trade_id in trade_ids}
        for note in self._parse_all(self._rows(statement), TradeNote, what="trade_note"):
            grouped.setdefault(note.trade_id, []).append(note)
        return grouped

    def in_period(self, start: datetime, end: datetime) -> list[TradeNote]:
        statement = (
            select(self.table)
            .where(self.table.c.at >= to_utc(start))
            .where(self.table.c.at < to_utc(end))
            .order_by(self.table.c.at)
        )
        return self._parse_all(self._rows(statement), TradeNote, what="trade_note")

    @staticmethod
    def _to_row(note: TradeNote) -> dict[str, Any]:
        return {
            "note_id": note.note_id,
            "schema_version": note.schema_version,
            "trade_id": note.trade_id,
            "author": note.author,
            "text": note.text,
            "at": note.at,
        }
