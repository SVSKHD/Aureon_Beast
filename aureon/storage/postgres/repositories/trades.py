"""Trades, on PostgreSQL (§49-§53, §58, plan §18).

MT5 is the truth about positions; this table is Aureon's record of what MT5 said. So every
method here RECORDS rather than decides, and the two rules that matter are both about not
destroying what was already recorded.

**A position already stored is left alone, not overwritten.** The stored row carries
excursion figures accumulated over hours of ticks, and a naive overwrite from a fresh broker
read would erase them -- silently, because the overwrite looks like a successful sync.

**A terminal trade is history (§58).** The transition table catches an illegal EDGE, but it
cannot see a SAME-status write carrying updates: ``CLOSED → CLOSED`` is not a transition, so
nothing in the table stops it rewriting a settled trade's ``realized_pnl``. That would make
the P&L, and every review built on it, unfalsifiable. Only the observational stamps
(``last_reconciled_at``, ``last_synced_at``) may still be written, because they say when it
was last looked at rather than what happened.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select

from aureon.models.audit import AuditRecord
from aureon.models.base import to_utc, utc_now
from aureon.models.enums import (
    TERMINAL_TRADE_STATUSES,
    TradeStatus,
    TransitionError,
    assert_trade_transition,
)
from aureon.models.trade import Trade, TradeManagementEvent
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.audit import AuditRepository
from aureon.storage.postgres.repositories.base import PostgresRepository

log = logging.getLogger(__name__)

#: The label written into ``audit_logs.collection``, derived from the table rather
#: than spelled as a literal. Two reasons: the label can never drift from the table it
#: names, and §83's guard against bare collection literals keeps covering this module
#: (decision 356).
COLLECTION = tables.Trade.__tablename__

#: §58. The only fields writable on a terminal trade. They record when it was last LOOKED
#: at, not what happened -- so writing them cannot change a settled outcome.
RECONCILIATION_ONLY_FIELDS: frozenset[str] = frozenset(
    {"last_reconciled_at", "last_synced_at"}
)


class TradeTransitionRejected(RuntimeError):
    """An attempted trade status change is not a legal edge."""


class TerminalWriteRejected(RuntimeError):
    """An attempt to write to a trade whose status is terminal."""


def trade_id_for(position_id: int, *, account_scope: str) -> str:
    """Deterministic trade id from the broker's position id.

    Keyed on the position rather than randomly, so the monitor seeing the same position on
    two polls -- or after a restart -- upserts one row instead of accumulating duplicates.
    ``account_scope`` is included for the reason detections carry it: two accounts can
    legitimately hold the same position id.
    """
    return f"{account_scope}__{position_id}"


class TradeRepository(PostgresRepository):
    """Reads and writes ``trades`` plus append-only management history."""

    table = tables.Trade.__table__
    management_events_table = tables.TradeManagementEvent.__table__

    def __init__(self, database: Any, *, account_scope: str = "primary") -> None:
        super().__init__(database)
        self.account_scope = account_scope
        self.audit = AuditRepository(database)

    # ── Writing ───────────────────────────────────────────────────────────────

    def upsert_open(self, trade: Trade) -> Trade:
        """Record a position the broker reports as open. Idempotent.

        A position already recorded is LEFT ALONE rather than overwritten -- see the module
        docstring. The monitor calls this every poll, so this is the common path, not an
        edge case.
        """
        with self._db.transaction() as connection:
            existing = self._locked(trade.trade_id, connection)
            if existing is not None:
                return Trade.model_validate(self._to_model_dict(existing))
            self._upsert(self._to_row(trade), connection=connection)
            self._audit(
                connection,
                actor="monitor",
                action="trade.open",
                trade_id=trade.trade_id,
                from_status=None,
                to_status=trade.status,
                detail={
                    "symbol": trade.symbol,
                    "volume": trade.volume,
                    "source": trade.source.value,
                    "mt5_position_id": trade.mt5_position_id,
                },
            )
            return trade

    def transition(
        self,
        trade_id: str,
        new_status: TradeStatus,
        *,
        updates: dict[str, Any] | None = None,
        actor: str = "monitor",
        reason: str | None = None,
        reconciliation: bool = False,
        now: datetime | None = None,
    ) -> Trade:
        """Move a trade's status, or update it in place when the status is unchanged."""
        moment = to_utc(now or utc_now())

        with self._db.transaction() as connection:
            row = self._locked(trade_id, connection)
            if row is None:
                raise TradeTransitionRejected(f"no such trade {trade_id}")
            current = Trade.model_validate(self._to_model_dict(row))

            if current.status is new_status and not updates:
                return current  # nothing to write

            if current.status is not new_status:
                # A status CHANGE is the transition table's business, terminal or not:
                # CLOSED → OPEN is an illegal edge and must keep saying so.
                try:
                    assert_trade_transition(current.status, new_status)
                except TransitionError as exc:
                    raise TradeTransitionRejected(str(exc)) from exc
            elif current.status in TERMINAL_TRADE_STATUSES:
                # The gap the table cannot see: a SAME-status write carrying updates.
                disallowed = sorted(set(updates or ()) - RECONCILIATION_ONLY_FIELDS)
                if disallowed:
                    raise TerminalWriteRejected(
                        f"{trade_id} is {current.status.value}, which is terminal; "
                        f"refusing to write {disallowed}. Only "
                        f"{sorted(RECONCILIATION_ONLY_FIELDS)} may still be written."
                    )

            moved = current.model_copy(update={"status": new_status, **(updates or {})})
            self._upsert(self._to_row(moved), connection=connection)
            self._audit(
                connection,
                actor=actor,
                # The OUTCOME, not the verb: ``trade.closed`` is what an operator greps
                # for, and what §60's assertions name. ``to_status`` carries it too, but
                # the action is the index. The Firestore repository wrote
                # ``f"trade.{new_status.value}"`` and the port flattened it (decision 376).
                action=f"trade.{new_status.value}",
                trade_id=trade_id,
                from_status=current.status,
                to_status=new_status,
                reason=reason,
                reconciliation=reconciliation,
            )
            _ = moment
            return moved

    def update_excursion(
        self, trade_id: str, excursion: Any, *, now: datetime | None = None
    ) -> Trade | None:
        """Write updated excursion figures without changing the status.

        Separate from ``transition`` and deliberately NOT audited: excursions update
        continuously while a position is open, and auditing every tick-driven revision would
        bury the transitions that matter in noise.
        """
        _ = to_utc(now or utc_now())
        with self._db.transaction() as connection:
            row = self._locked(trade_id, connection)
            if row is None:
                return None
            current = Trade.model_validate(self._to_model_dict(row))
            if current.status is TradeStatus.CLOSED:
                # A closed trade's excursions are final; a late tick must not extend them.
                return current
            updated = current.model_copy(update={"excursion": excursion})
            self._upsert(self._to_row(updated), connection=connection)
            return updated

    def update_management(
        self,
        trade_id: str,
        *,
        management: object | None = None,
        guardian: object | None = None,
    ) -> Trade | None:
        """Persist current Agent14/16 state without changing broker truth."""
        with self._db.transaction() as connection:
            row = self._locked(trade_id, connection)
            if row is None:
                return None
            current = Trade.model_validate(self._to_model_dict(row))
            if current.status is TradeStatus.CLOSED:
                return current
            updates: dict[str, Any] = {}
            if management is not None:
                updates["management"] = management
            if guardian is not None:
                updates["guardian"] = guardian
            if not updates:
                return current
            updated = current.model_copy(update=updates)
            self._upsert(self._to_row(updated), connection=connection)
            return updated

    def append_management_event(
        self,
        event: TradeManagementEvent,
    ) -> TradeManagementEvent:
        """Persist one idempotent, append-only management observation."""
        payload = event.model_dump(mode="json")
        self._upsert(
            {
                "event_id": event.event_id,
                "schema_version": event.schema_version,
                "trade_id": event.trade_id,
                "observed_at": event.observed_at,
                "source": event.source,
                "action": event.action,
                "current_move": event.current_move,
                "peak_move": event.peak_move,
                "giveback": event.giveback,
                "protected_move": event.protected_move,
                "trail_price": event.trail_price,
                "continuation_score": event.continuation_score,
                "continuation_total": event.continuation_total,
                "exit_price": event.exit_price,
                "realized_move": event.realized_move,
                "exit_reason": event.exit_reason,
            },
            table=self.management_events_table,
        )
        _ = payload
        return event

    def management_events(self, trade_id: str) -> list[TradeManagementEvent]:
        statement = (
            select(self.management_events_table)
            .where(self.management_events_table.c.trade_id == trade_id)
            .order_by(self.management_events_table.c.observed_at)
        )
        return [
            TradeManagementEvent.model_validate(dict(row))
            for row in self._rows(statement)
        ]

    # ── Reading ───────────────────────────────────────────────────────────────

    def get(self, trade_id: str) -> Trade | None:
        row = self._row(trade_id)
        return None if row is None else Trade.model_validate(self._to_model_dict(row))

    def get_by_position(self, position_id: int) -> Trade | None:
        """By the broker's position id, which is how MT5 identifies one.

        A real query on a unique index, rather than rebuilding the deterministic id: the id
        is derived from ``account_scope`` too, and a caller holding only a position id from
        the broker should not have to know this repository's scope to look it up.
        """
        statement = select(self.table).where(self.table.c.mt5_position_id == position_id)
        rows = self._rows(statement)
        if not rows:
            return None
        return Trade.model_validate(self._to_model_dict(rows[0]))

    def list_by_status(self, status: TradeStatus, *, symbol: str | None = None) -> list[Trade]:
        statement = select(self.table).where(self.table.c.status == status.value)
        if symbol is not None:
            statement = statement.where(self.table.c.symbol == symbol)
        statement = statement.order_by(self.table.c.open_time_utc)
        return self._parse_all(self._rows(statement), Trade, what="trade")

    def open_trades(self) -> list[Trade]:
        """Every position not yet closed. The monitor's working set.

        Includes PARTIALLY_CLOSED: a position with volume left is still a position, and
        omitting it would leave the monitor blind to the remainder.
        """
        statement = (
            select(self.table)
            .where(
                self.table.c.status.in_(
                    [TradeStatus.OPEN.value, TradeStatus.PARTIALLY_CLOSED.value]
                )
            )
            .order_by(self.table.c.open_time_utc)
        )
        return self._parse_all(self._rows(statement), Trade, what="trade")

    def in_period(self, start: datetime, end: datetime) -> list[Trade]:
        """Trades OPENED in ``[start, end)``. What a review counts."""
        statement = (
            select(self.table)
            .where(self.table.c.open_time_utc >= to_utc(start))
            .where(self.table.c.open_time_utc < to_utc(end))
            .order_by(self.table.c.open_time_utc)
        )
        return self._parse_all(self._rows(statement), Trade, what="trade")

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _locked(self, trade_id: str, connection: Any) -> Any | None:
        statement = (
            select(self.table).where(self.table.c.trade_id == trade_id).with_for_update()
        )
        return connection.execute(statement).mappings().first()

    def _audit(
        self,
        connection: Any,
        *,
        actor: str,
        action: str,
        trade_id: str,
        from_status: TradeStatus | None,
        to_status: TradeStatus | None,
        reason: str | None = None,
        reconciliation: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.audit.append(
            AuditRecord(
                audit_id=uuid.uuid4().hex,
                actor=actor,
                action=action,
                collection=COLLECTION,
                document_id=trade_id,
                from_status=from_status.value if from_status else None,
                to_status=to_status.value if to_status else None,
                reason=reason,
                reconciliation=reconciliation,
                detail=detail or {},
            ),
            connection=connection,
        )

    # ── mapping ───────────────────────────────────────────────────────────────

    @staticmethod
    def _to_row(trade: Trade) -> dict[str, Any]:
        payload = trade.model_dump(mode="json")
        return {
            "trade_id": trade.trade_id,
            "schema_version": trade.schema_version,
            "mt5_position_id": trade.mt5_position_id,
            "trade_request_id": trade.trade_request_id,
            "source": trade.source.value,
            "symbol": trade.symbol,
            "direction": trade.direction.value,
            "volume": trade.volume,
            "open_price": trade.open_price,
            "open_time_utc": trade.open_time.utc,
            "timezone": trade.open_time.market_tz,
            "sl": trade.sl,
            "tp": trade.tp,
            "magic": trade.magic,
            "status": trade.status.value,
            "closed_volume": trade.closed_volume,
            "close_price": trade.close_price,
            "close_time_utc": trade.close_time.utc if trade.close_time else None,
            "close_reason": trade.close_reason,
            "close_reason_raw": trade.close_reason_raw,
            "realized_pnl": trade.realized_pnl,
            "commission": trade.commission,
            "swap": trade.swap,
            "detection_id": trade.detection_id,
            "link_type": trade.link_type.value if trade.link_type else None,
            "deal_ids": {"items": list(trade.deal_ids)},
            "excursion": payload["excursion"],
            "management": payload.get("management"),
            "guardian": payload.get("guardian"),
            "last_reconciled_at": trade.last_reconciled_at,
            "last_synced_at": trade.last_synced_at,
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        timezone = data.pop("timezone")
        open_time_utc = data.pop("open_time_utc")
        close_time_utc = data.pop("close_time_utc")
        data["deal_ids"] = data["deal_ids"]["items"]
        data["open_time"] = {"utc": open_time_utc, "market_tz": timezone}
        data["close_time"] = (
            None if close_time_utc is None else {"utc": close_time_utc, "market_tz": timezone}
        )
        return data
