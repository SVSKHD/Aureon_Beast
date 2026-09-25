"""Trade (position) persistence (§49-§53, §60).

Every status change goes through ``assert_trade_transition`` and writes an audit row in
the same transaction, for the same reason the trade requests do (§60): written
afterwards, a crash loses the record of a change that did happen.

## Trades are observations, not decisions

Aureon never decides a trade closed; it observes that it did. So the repository's job is
to record what the broker says, idempotently -- ``upsert`` is keyed on the MT5 position
id, which means the monitor can re-see the same position on every poll without creating a
second document.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from aureon.models.audit import AuditRecord
from aureon.models.base import to_utc, utc_now
from aureon.models.enums import (
    TERMINAL_TRADE_STATUSES,
    TradeStatus,
    TransitionError,
    assert_trade_transition,
)
from aureon.models.trade import Trade
from aureon.storage import paths
from aureon.storage.trade_request_repository import (
    RECONCILIATION_ONLY_FIELDS,
    TerminalWriteRejected,
    _where,
)

log = logging.getLogger(__name__)


class TradeTransitionRejected(RuntimeError):
    """An attempted trade status change is not a legal edge."""


def trade_id_for(position_id: int, *, account_scope: str) -> str:
    """Deterministic trade id from the broker's position id.

    Keyed on the position rather than randomly, so the monitor seeing the same position
    on two polls -- or after a restart -- upserts one document instead of accumulating
    duplicates. ``account_scope`` is included for the same reason detections carry it: two
    accounts can legitimately hold the same position id.
    """
    return f"{account_scope}__{position_id}"


class TradeRepository:
    """Reads and writes ``trades``."""

    def __init__(self, client: Any, *, account_scope: str = "primary") -> None:
        self._client = client
        self.account_scope = account_scope

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ref(self, trade_id: str):
        return self._client.document(paths.trade_path(trade_id))

    @staticmethod
    def _run(transaction: Any, fn: Callable[..., Any], *args: Any) -> Any:
        from google.cloud import firestore

        return firestore.transactional(fn)(transaction, *args)

    def _write_audit(
        self,
        transaction: Any,
        *,
        actor: str,
        action: str,
        trade_id: str,
        from_status: TradeStatus | None,
        to_status: TradeStatus | None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
        reconciliation: bool = False,
    ) -> None:
        record = AuditRecord(
            audit_id=uuid.uuid4().hex,
            actor=actor,
            action=action,
            collection=paths.TRADES,
            document_id=trade_id,
            from_status=from_status.value if from_status else None,
            to_status=to_status.value if to_status else None,
            reason=reason,
            detail=detail or {},
            reconciliation=reconciliation,
        )
        transaction.set(
            self._client.document(paths.audit_path(record.audit_id)),
            record.model_dump(mode="json"),
        )

    # ── Reading ───────────────────────────────────────────────────────────────

    def get(self, trade_id: str) -> Trade | None:
        snapshot = self._ref(trade_id).get()
        if not getattr(snapshot, "exists", False):
            return None
        return Trade.model_validate(snapshot.to_dict())

    def get_by_position(self, position_id: int) -> Trade | None:
        return self.get(trade_id_for(position_id, account_scope=self.account_scope))

    def list_by_status(
        self, status: TradeStatus | list[TradeStatus], *, limit: int = 200
    ) -> list[Trade]:
        """Trades in one or more statuses. The monitor loads OPEN and PARTIALLY_CLOSED."""
        statuses = [status] if isinstance(status, TradeStatus) else list(status)
        found: list[Trade] = []
        for one in statuses:
            query = _where(
                self._client.collection(paths.TRADES).limit(limit),
                "status",
                "==",
                one.value,
            )
            found.extend(Trade.model_validate(doc.to_dict()) for doc in query.stream())
        return found

    def open_trades(self) -> list[Trade]:
        return self.list_by_status([TradeStatus.OPEN, TradeStatus.PARTIALLY_CLOSED])

    # ── Writing ───────────────────────────────────────────────────────────────

    def upsert_open(self, trade: Trade) -> Trade:
        """Record a position the broker reports as open. Idempotent.

        A position already recorded is **left alone** rather than overwritten: the stored
        document may carry excursion figures accumulated over hours, and a naive
        overwrite from a fresh broker read would erase them.
        """

        def txn(transaction: Any) -> Trade:
            existing = self._ref(trade.trade_id).get(transaction=transaction)
            if getattr(existing, "exists", False):
                return Trade.model_validate(existing.to_dict())
            transaction.set(self._ref(trade.trade_id), trade.model_dump(mode="json"))
            self._write_audit(
                transaction,
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

        return self._run(self._client.transaction(), txn)

    def transition(
        self,
        trade_id: str,
        new_status: TradeStatus,
        *,
        updates: dict[str, Any] | None = None,
        actor: str = "monitor",
        reason: str | None = None,
        reconciliation: bool = False,
    ) -> Trade:
        """Move a trade's status, or update it in place if the status is unchanged."""

        def txn(transaction: Any) -> Trade:
            snapshot = self._ref(trade_id).get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                raise TradeTransitionRejected(f"no such trade {trade_id}")
            current = Trade.model_validate(snapshot.to_dict())

            if current.status is new_status and not updates:
                return current  # nothing to write

            if current.status is not new_status:
                # A status CHANGE is the transition table's business, terminal or not:
                # CLOSED -> OPEN is an illegal edge and must keep saying so.
                try:
                    assert_trade_transition(current.status, new_status)
                except TransitionError as exc:
                    raise TradeTransitionRejected(str(exc)) from exc
            elif current.status in TERMINAL_TRADE_STATUSES:
                # The gap the table cannot see: a SAME-status write carrying `updates`.
                # CLOSED is history, and this path could rewrite a settled trade's
                # realized_pnl -- making the P&L, and every review built on it,
                # unfalsifiable. Only the observational stamps may still be written;
                # they say when it was last looked at, not what happened (§58).
                disallowed = sorted(set(updates or ()) - RECONCILIATION_ONLY_FIELDS)
                if disallowed:
                    raise TerminalWriteRejected(
                        f"{trade_id} is {current.status.value}, which is terminal; "
                        f"refusing to write {disallowed}. Only "
                        f"{sorted(RECONCILIATION_ONLY_FIELDS)} may still be written."
                    )

            updated = current.model_copy(update={"status": new_status, **(updates or {})})
            transaction.set(self._ref(trade_id), updated.model_dump(mode="json"))
            self._write_audit(
                transaction,
                actor=actor,
                action=f"trade.{new_status.value}",
                trade_id=trade_id,
                from_status=current.status,
                to_status=new_status,
                reason=reason,
                detail={
                    k: v
                    for k, v in (updates or {}).items()
                    if isinstance(v, (str, int, float, bool, type(None)))
                },
                reconciliation=reconciliation,
            )
            return updated

        return self._run(self._client.transaction(), txn)

    def update_excursion(
        self, trade_id: str, excursion: Any, *, now: datetime | None = None
    ) -> Trade | None:
        """Write updated excursion figures without changing the status.

        Separate from ``transition`` and deliberately **not** audited: excursions update
        continuously while a position is open, and auditing every tick-driven revision
        would bury the transitions that matter in noise.
        """
        moment = to_utc(now or utc_now())

        def txn(transaction: Any) -> Trade | None:
            snapshot = self._ref(trade_id).get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                return None
            current = Trade.model_validate(snapshot.to_dict())
            if current.status is TradeStatus.CLOSED:
                # A closed trade's excursions are final; a late tick must not extend them.
                return current
            updated = current.model_copy(update={"excursion": excursion})
            transaction.set(self._ref(trade_id), updated.model_dump(mode="json"))
            return updated

        _ = moment
        return self._run(self._client.transaction(), txn)

    def update_management(
        self,
        trade_id: str,
        *,
        management: object | None = None,
        guardian: object | None = None,
    ) -> Trade | None:
        """Persist Agents 14/16 state without changing broker-observed trade status.

        Like excursions, these fields update frequently and are not transitions. CLOSED trades
        are immutable history, so late management reads are ignored.
        """

        def txn(transaction: Any) -> Trade | None:
            snapshot = self._ref(trade_id).get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                return None
            current = Trade.model_validate(snapshot.to_dict())
            if current.status is TradeStatus.CLOSED:
                return current
            updates: dict[str, object] = {}
            if management is not None:
                updates["management"] = management
            if guardian is not None:
                updates["guardian"] = guardian
            if not updates:
                return current
            updated = current.model_copy(update=updates)
            transaction.set(self._ref(trade_id), updated.model_dump(mode="json"))
            return updated

        return self._run(self._client.transaction(), txn)

    def closed_in_period(self, start: datetime, end: datetime) -> list[Trade]:
        """Trades whose broker-observed close time falls in the requested UTC interval."""

        lower, upper = to_utc(start), to_utc(end)
        found: list[Trade] = []
        for doc in self._client.collection(paths.TRADES).stream():
            try:
                trade = Trade.model_validate(doc.to_dict() or {})
            except Exception:  # noqa: BLE001
                log.exception("unreadable trade %s", doc.id)
                continue
            if trade.close_time is None:
                continue
            if lower <= trade.close_time.utc < upper:
                found.append(trade)
        return found

    def opened_in_period(self, start: datetime, end: datetime) -> list[Trade]:
        """Trades whose OPEN time falls in ``[start, end)``.

        By open time, not close: a trade belongs to the period it was entered in even if
        it closed later. Filing it by close would attribute a Monday decision to Tuesday.
        """
        lower, upper = to_utc(start), to_utc(end)
        found: list[Trade] = []
        for doc in self._client.collection(paths.TRADES).stream():
            try:
                trade = Trade.model_validate(doc.to_dict() or {})
            except Exception:  # noqa: BLE001
                log.exception("unreadable trade %s", doc.id)
                continue
            if lower <= trade.open_time.utc < upper:
                found.append(trade)
        return found
