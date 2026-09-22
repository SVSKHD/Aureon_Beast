"""Notifications, on PostgreSQL (9C, 12 T-11, plan §46).

One row per thing-said, at an id derived from what the message is ABOUT. That is the whole
exactly-once mechanism: a bot that dies between posting and recording finds the row on
restart instead of posting again.

**The claim is an INSERT that may collide, not a read-then-write.** ``claim`` inserts and
treats a unique violation as "someone else has it". A version that checked first and then
inserted would have a window between the two, and two notifier sweeps running a second apart
is exactly the thing that window exists to lose money -- or here, to post the same card
twice into a trading channel.

The Firestore version used ``create``, which refuses over an existing document. The
PostgreSQL equivalent is a primary-key violation caught in a SAVEPOINT, so the collision
does not poison the surrounding transaction (decision 351).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from aureon.models.alerts import Notification
from aureon.models.base import to_utc, utc_now
from aureon.models.enums import NotificationKind, NotificationStatus
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


def notification_id(kind: str, ref_id: str) -> str:
    """``{kind}__{ref_id}`` -- the id that makes a send exactly-once (9C).

    Derived from what the message is about rather than generated, and kept byte-identical to
    the Firestore shape so an export lands on the same primary key (C-12).
    """
    if not kind:
        raise ValueError("kind must not be empty")
    if not ref_id:
        raise ValueError("ref_id must not be empty")
    return f"{kind}__{ref_id}"


class NotificationRepository(PostgresRepository):
    """Claims, records and reads ``notifications``."""

    table = tables.Notification.__table__

    # ── The claim ─────────────────────────────────────────────────────────────

    def claim(
        self,
        kind: NotificationKind | str,
        ref_id: str,
        *,
        symbol: str,
        channel_id: str,
        now: datetime | None = None,
    ) -> Notification | None:
        """Take the right to post this once, or ``None`` if someone already has it.

        An INSERT, not an upsert: the collision IS the answer. Wrapped in a SAVEPOINT so a
        caller running this inside a larger transaction is not left with a broken one --
        PostgreSQL aborts the whole transaction on a constraint violation unless the failure
        happens inside a nested block.
        """
        from sqlalchemy.exc import IntegrityError

        moment = to_utc(now or utc_now())
        record = Notification(
            notification_id=notification_id(str(kind), ref_id),
            kind=NotificationKind(str(kind)),
            symbol=symbol,
            ref_id=ref_id,
            channel_id=str(channel_id),
            sent_at=moment,
        )
        with self._db.transaction() as connection:
            savepoint = connection.begin_nested()
            try:
                connection.execute(self.table.insert().values(self._to_row(record)))
                savepoint.commit()
            except IntegrityError:
                savepoint.rollback()
                return None
        return record

    # ── After the send ────────────────────────────────────────────────────────

    def record_message(
        self, kind: NotificationKind | str, ref_id: str, message_id: str
    ) -> Notification | None:
        """Store the Discord message id, so the card can be EDITED rather than reposted.

        On the notification and not on the setup (12 T-11): a message id is a fact about a
        notification, and putting it on the setup would make the observer's record depend on
        whether a chat client happened to be up.
        """
        return self._patch(kind, ref_id, {"message_id": message_id})

    def mark_failed(
        self, kind: NotificationKind | str, ref_id: str, failure_message: str
    ) -> Notification | None:
        """Record that the send failed.

        The row STAYS, deliberately. Deleting it would make the next sweep try again, and a
        send that fails because the message is malformed would then be retried for ever
        against a channel that keeps refusing it.
        """
        return self._patch(
            kind,
            ref_id,
            {
                "status": NotificationStatus.FAILED,
                "failure_message": failure_message,
            },
        )

    def _patch(
        self, kind: NotificationKind | str, ref_id: str, updates: dict[str, Any]
    ) -> Notification | None:
        row_id = notification_id(str(kind), ref_id)
        with self._db.transaction() as connection:
            statement = (
                select(self.table)
                .where(self.table.c.notification_id == row_id)
                .with_for_update()
            )
            row = connection.execute(statement).mappings().first()
            if row is None:
                return None
            current = Notification.model_validate(self._to_model_dict(row))
            updated = current.model_copy(update=updates)
            self._upsert(self._to_row(updated), connection=connection)
            return updated

    # ── Reading ───────────────────────────────────────────────────────────────

    def get(self, kind: NotificationKind | str, ref_id: str) -> Notification | None:
        row = self._row(notification_id(str(kind), ref_id))
        return None if row is None else Notification.model_validate(self._to_model_dict(row))

    def already_sent(self, kind: NotificationKind | str, ref_id: str) -> bool:
        """Whether this has been posted. Used to skip work, never to decide whether to post.

        The decision belongs to ``claim``, which is atomic; this is an optimisation that may
        be stale by the time the caller acts on it, and treating it as the gate would put
        the race straight back.
        """
        return self.get(kind, ref_id) is not None

    def recent(self, limit: int = 50) -> list[Notification]:
        statement = (
            select(self.table).order_by(self.table.c.sent_at.desc()).limit(limit)
        )
        return self._parse_all(self._rows(statement), Notification, what="notification")

    # ── Mapping ───────────────────────────────────────────────────────────────

    @staticmethod
    def _to_row(record: Notification) -> dict[str, Any]:
        return {
            "notification_id": record.notification_id,
            "schema_version": record.schema_version,
            "kind": record.kind.value,
            "symbol": record.symbol,
            "ref_id": record.ref_id,
            "channel_id": record.channel_id,
            "status": record.status.value,
            "sent_at": record.sent_at,
            "message_id": record.message_id,
            "failure_message": record.failure_message,
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        return dict(row)
