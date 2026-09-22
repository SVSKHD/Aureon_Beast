"""``/remind`` price alerts, on PostgreSQL (9C).

Two writers, and they must not tread on each other. Discord arms and cancels; the
**observer** fires and expires, because firing needs the quotes the observer is already
reading and Discord may not call the broker (CLAUDE.md).

## Firing is transactional, and that is the whole point

An alert must be answered **once**. Two observer instances polling the same quote would
both see the level crossed, and a read-then-write would have both freeze a snapshot and
both tell Discord. So ``fire`` takes ``SELECT ... FOR UPDATE`` on the row, refuses unless it
is still ARMED, and writes FIRED with its snapshot in the same commit. The loser blocks,
then sees FIRED and raises ``AlertClaimRejected`` -- the same discipline the executor's
claim uses for money, applied here to a message.

What the migration changes is what can be PROVED. The Firestore version's isolation came
from ``firestore.transactional``, which the in-memory double did not implement, so every
unit test of ``fire`` ran with none at all. Here the lock is real in every test, and
``tests/postgres/contention.py`` forces the overlap that a thread-pair alone does not
(decision 352).

Every status change goes through ``assert_price_alert_transition`` (CLAUDE.md), so a
cancelled alert cannot be fired and a fired one cannot be re-armed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from aureon.models.alerts import (
    DEFAULT_ALERT_TTL_HOURS,
    MAX_ARMED_ALERTS_PER_USER,
    PriceAlert,
)
from aureon.models.base import to_utc, utc_now
from aureon.models.enums import PriceAlertStatus, assert_price_alert_transition
from aureon.models.identity import new_alert_id
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository

log = logging.getLogger(__name__)

#: Re-exported so a caller that already holds this repository need not reach for the models
#: package for an id. The definition lives in ``aureon.models.identity`` with the others.
__all__ = ["AlertClaimRejected", "AlertRejected", "PriceAlertRepository", "new_alert_id"]


class AlertRejected(Exception):
    """The alert cannot be armed, or cannot be changed as asked."""


class AlertClaimRejected(Exception):
    """Someone else already answered or withdrew this alert."""


class PriceAlertRepository(PostgresRepository):
    """Reads and writes ``alerts``."""

    table = tables.PriceAlert.__table__

    # ── Reading ───────────────────────────────────────────────────────────────

    def get(self, alert_id: str) -> PriceAlert | None:
        row = self._row(alert_id)
        return None if row is None else PriceAlert.model_validate(dict(row))

    def armed(
        self, *, symbol: str | None = None, user_id: str | None = None
    ) -> list[PriceAlert]:
        """Every ARMED alert, optionally narrowed.

        Narrowed in SQL rather than in Python, which is the one thing this read gains from
        the migration: the Firestore version queried on status alone and filtered the rest
        in memory because a composite index would have had to be declared and deployed for
        a collection holding tens of documents. An index is free here and the observer runs
        this on every candle close for every symbol.
        """
        statement = select(self.table).where(
            self.table.c.status == PriceAlertStatus.ARMED.value
        )
        if symbol is not None:
            statement = statement.where(self.table.c.symbol == symbol.upper())
        if user_id is not None:
            statement = statement.where(self.table.c.requested_by == str(user_id))
        statement = statement.order_by(
            self.table.c.symbol, self.table.c.level, self.table.c.alert_id
        )
        return self._parse_all(self._rows(statement), PriceAlert, what="alert")

    def fired_since(self, moment: datetime) -> list[PriceAlert]:
        """Alerts that fired at or after ``moment`` (9C)."""
        statement = (
            select(self.table)
            .where(self.table.c.status == PriceAlertStatus.FIRED.value)
            .where(self.table.c.fired_at >= to_utc(moment))
            .order_by(self.table.c.fired_at)
        )
        return self._parse_all(self._rows(statement), PriceAlert, what="alert")

    def for_user(self, user_id: str) -> list[PriceAlert]:
        """Every alert of one user, armed or not -- what ``/remind list`` shows."""
        statement = (
            select(self.table)
            .where(self.table.c.requested_by == str(user_id))
            .order_by(self.table.c.status, self.table.c.symbol, self.table.c.level)
        )
        return self._parse_all(self._rows(statement), PriceAlert, what="alert")

    # ── Arming ────────────────────────────────────────────────────────────────

    def arm(
        self,
        alert: PriceAlert,
        *,
        ttl_hours: float = DEFAULT_ALERT_TTL_HOURS,
        max_per_user: int = MAX_ARMED_ALERTS_PER_USER,
        now: datetime | None = None,
    ) -> PriceAlert:
        """Store a new ARMED alert, refusing a user who already has too many (9C).

        The cap is checked here rather than in the command, so it holds however the alert
        was created. It is not a resource limit: twenty levels is more than anyone watches,
        and the hundredth armed alert makes the channel useless for the ones that matter.

        The count and the insert share one transaction, which the Firestore version could
        not do: there the cap was a read followed by an unrelated write, so a user issuing
        two ``/remind`` commands at once could pass the check twice. Here the COUNT runs in
        the same transaction as the INSERT.
        """
        moment = to_utc(now or utc_now())
        stored = alert.model_copy(
            update={
                "status": PriceAlertStatus.ARMED,
                "created_at": moment,
                "expires_at": alert.expires_at or moment + timedelta(hours=ttl_hours),
            }
        )
        with self._db.transaction() as connection:
            existing = self._armed_for_user_locked(str(alert.requested_by), connection)
            if len(existing) >= max_per_user:
                raise AlertRejected(
                    f"{alert.requested_by} already has {len(existing)} armed alerts "
                    f"(the limit is {max_per_user}). Cancel one with `/remind cancel`."
                )
            self._upsert(_to_row(stored), connection=connection)
        log.info(
            "armed alert %s: %s %s %s",
            stored.alert_id,
            stored.symbol,
            stored.side,
            stored.level,
        )
        return stored

    def _armed_for_user_locked(self, user_id: str, connection: Any) -> list[Any]:
        """This user's armed alerts, under the lock that makes counting them meaningful.

        An **advisory** lock keyed on the user and not ``SELECT ... FOR UPDATE``, because a
        row lock cannot protect a COUNT. Measured: two concurrent arms both took
        ``FOR UPDATE`` over the user's existing rows, the second blocked as intended, and
        then it still counted only the rows visible to its own statement snapshot -- the
        first transaction's freshly inserted alert was a phantom, and both passed a cap of
        two with three alerts (decision 359). The lock has to be on the QUESTION, "how many
        does this user have", not on the rows that happen to answer it today.
        """
        self._advisory_lock(connection, f"alerts:{user_id}")
        statement = (
            select(self.table)
            .where(self.table.c.status == PriceAlertStatus.ARMED.value)
            .where(self.table.c.requested_by == user_id)
        )
        return list(connection.execute(statement).mappings().all())

    # ── Answering ─────────────────────────────────────────────────────────────

    def fire(
        self,
        alert_id: str,
        *,
        price: float,
        snapshot: dict[str, object],
        now: datetime | None = None,
    ) -> PriceAlert:
        """ARMED → FIRED with its snapshot, in one transaction (9C).

        Raises ``AlertClaimRejected`` when the alert is no longer armed, which is how a
        second observer instance -- or a cancel that landed a moment earlier -- loses
        without either of them sending a message.
        """
        moment = to_utc(now or utc_now())
        with self._db.transaction() as connection:
            current = self._locked(alert_id, connection)
            if current.status is not PriceAlertStatus.ARMED:
                raise AlertClaimRejected(
                    f"alert {alert_id} is {current.status.value}, not armed"
                )
            assert_price_alert_transition(current.status, PriceAlertStatus.FIRED)
            fired = current.model_copy(
                update={
                    "status": PriceAlertStatus.FIRED,
                    "fired_at": moment,
                    "fired_price": price,
                    # Frozen here, by the process that has the indicators. Discord renders
                    # this copy and never recomputes it (9C).
                    "fired_snapshot": dict(snapshot),
                }
            )
            self._upsert(_to_row(fired), connection=connection)
            return fired

    def cancel(
        self, alert_id: str, *, actor: str, now: datetime | None = None
    ) -> PriceAlert:
        """ARMED → CANCELLED, and only by the user who armed it (§71)."""
        _ = now
        with self._db.transaction() as connection:
            current = self._locked(alert_id, connection)
            if str(current.requested_by) != str(actor):
                raise AlertRejected(
                    f"{actor} did not arm alert {alert_id}, so cannot cancel it"
                )
            if current.status is not PriceAlertStatus.ARMED:
                raise AlertClaimRejected(
                    f"alert {alert_id} is already {current.status.value}"
                )
            assert_price_alert_transition(current.status, PriceAlertStatus.CANCELLED)
            cancelled = current.model_copy(
                update={
                    "status": PriceAlertStatus.CANCELLED,
                    "cancelled_by": str(actor),
                    "fired_at": None,
                }
            )
            self._upsert(_to_row(cancelled), connection=connection)
            return cancelled

    def expire_due(self, *, now: datetime | None = None) -> list[str]:
        """Expire every ARMED alert past its ``expires_at``. Returns the ids (9C).

        Run by the observer on candle close rather than by a timer, so expiry happens on
        the same clock as everything else the observer records and needs no second
        scheduler.
        """
        moment = to_utc(now or utc_now())
        expired: list[str] = []
        for alert in self.armed():
            if alert.expires_at is None or alert.expires_at > moment:
                continue
            try:
                self._expire(alert.alert_id)
                expired.append(alert.alert_id)
            except AlertClaimRejected:
                # Fired or cancelled between the scan and the write. Nothing to do, and
                # certainly nothing to overwrite.
                continue
        return expired

    def _expire(self, alert_id: str) -> PriceAlert:
        with self._db.transaction() as connection:
            current = self._locked(alert_id, connection)
            if current.status is not PriceAlertStatus.ARMED:
                raise AlertClaimRejected(f"alert {alert_id} is {current.status.value}")
            assert_price_alert_transition(current.status, PriceAlertStatus.EXPIRED)
            done = current.model_copy(update={"status": PriceAlertStatus.EXPIRED})
            self._upsert(_to_row(done), connection=connection)
            return done

    def _locked(self, alert_id: str, connection: Any) -> PriceAlert:
        """The alert, locked for the rest of this transaction, or ``AlertClaimRejected``.

        ``FOR UPDATE`` and not ``SKIP LOCKED``: two observers answering the SAME alert must
        serialise, so the loser sees FIRED rather than skipping the row and concluding
        there was nothing to answer.
        """
        statement = (
            select(self.table)
            .where(self.table.c.alert_id == alert_id)
            .with_for_update()
        )
        row = connection.execute(statement).mappings().first()
        if row is None:
            raise AlertClaimRejected(f"no alert {alert_id}")
        return PriceAlert.model_validate(dict(row))


def _to_row(alert: PriceAlert) -> dict[str, Any]:
    payload = alert.model_dump(mode="json")
    return {
        "alert_id": alert.alert_id,
        "schema_version": alert.schema_version,
        "symbol": alert.symbol,
        "level": alert.level,
        "side": alert.side,
        "status": alert.status.value,
        "requested_by": alert.requested_by,
        "note": alert.note,
        "cancelled_by": alert.cancelled_by,
        "created_at": alert.created_at,
        "expires_at": alert.expires_at,
        "fired_at": alert.fired_at,
        "fired_price": alert.fired_price,
        "fired_snapshot": payload["fired_snapshot"],
    }
