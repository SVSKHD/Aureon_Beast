"""What Discord has already said (9C).

One document per message, keyed by what the message is **about** (``{kind}__{ref_id}``). That
key is the entire mechanism: ``claim`` creates the document only if it does not exist, so the
caller that wins may post and every later attempt -- including one after a restart, which is
exactly when a duplicate would otherwise be sent -- finds it there and says nothing.

## Claim before posting, not after

The order matters and the safe direction is not obvious. Claiming first can lose a message: the
process dies between the claim and the post, and nothing retries. Posting first can DUPLICATE
one: the process dies between the post and the record, restarts, and posts again.

A lost detection embed costs a human a glance at `/status`. A duplicated one trains them to
ignore the channel, and a duplicated *price alert* tells them twice that a level they are
watching has been crossed -- which is a reason to act, twice. So: claim first, and record the
failure if the post then fails, where an operator can see it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aureon.models.alerts import Notification
from aureon.models.base import to_utc, utc_now
from aureon.models.enums import NotificationKind, NotificationStatus
from aureon.storage import paths

log = logging.getLogger(__name__)


class NotificationRepository:
    """Reads and writes ``{prefix}_notifications``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self, kind: NotificationKind | str, ref_id: str) -> Notification | None:
        snapshot = self._client.document(paths.notification_path(str(kind), ref_id)).get()
        if not getattr(snapshot, "exists", False):
            return None
        return Notification.model_validate(snapshot.to_dict())

    def already_sent(self, kind: NotificationKind | str, ref_id: str) -> bool:
        """Whether this thing has been announced. A FAILED record still counts.

        A failed send is not retried (see the module docstring): the operator sees the row,
        and the detection itself is in Firestore either way. Retrying would eventually
        double-post the one message a human reacts to.
        """
        return self.get(kind, ref_id) is not None

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

        ``create`` rather than ``set``: Firestore refuses a create over an existing
        document, which is what makes this a claim rather than an overwrite.
        """
        moment = to_utc(now or utc_now())
        record = Notification(
            notification_id=paths.notification_id(str(kind), ref_id),
            kind=NotificationKind(str(kind)),
            symbol=symbol,
            ref_id=ref_id,
            channel_id=str(channel_id),
            sent_at=moment,
        )
        ref = self._client.document(paths.notification_path(str(kind), ref_id))
        try:
            ref.create(record.model_dump(mode="json"))
        except Exception as exc:  # noqa: BLE001 - AlreadyExists across client versions
            if _is_already_exists(exc):
                return None
            raise
        return record

    def record_message(
        self, kind: NotificationKind | str, ref_id: str, *, message_id: str
    ) -> Notification | None:
        """Remember the Discord message this notification became, so it can be EDITED later.

        A separate write after the post rather than part of the claim, because the message id does
        not exist until Discord has accepted the message -- and the claim has to happen BEFORE the
        post or it is not a claim.

        The window between the two is the one 9C already accepted: a process that claimed, posted,
        and died before this write leaves a card in the channel that will never be edited again.
        That is visible (the card stops updating) rather than silent, and it is a better failure
        than the alternative -- claiming after posting, which double-posts under a race.

        A string, never an int: a Discord snowflake exceeds 2^53 and a JSON round trip through a
        float would corrupt one in a way nobody would notice until an edit hit the wrong message.
        """
        ref = self._client.document(paths.notification_path(str(kind), ref_id))
        snapshot = ref.get()
        if not getattr(snapshot, "exists", False):
            return None
        record = Notification.model_validate(snapshot.to_dict())
        updated = record.model_copy(update={"message_id": str(message_id)})
        ref.set(updated.model_dump(mode="json"))
        return updated

    def mark_failed(
        self, kind: NotificationKind | str, ref_id: str, *, message: str
    ) -> None:
        """Record that the post failed, for the operator rather than for a retry."""
        ref = self._client.document(paths.notification_path(str(kind), ref_id))
        snapshot = ref.get()
        if not getattr(snapshot, "exists", False):
            return
        record = Notification.model_validate(snapshot.to_dict())
        ref.set(
            record.model_copy(
                update={
                    "status": NotificationStatus.FAILED,
                    "failure_message": message[:500],
                }
            ).model_dump(mode="json")
        )
        log.error("notification %s failed: %s", record.notification_id, message)

    def recent(self, limit: int = 50) -> list[Notification]:
        """Whatever the collection holds, newest id last. For diagnostics only."""
        docs = list(self._client.collection(paths.NOTIFICATIONS).limit(limit).stream())
        return [Notification.model_validate(doc.to_dict()) for doc in docs]


def _is_already_exists(exc: BaseException) -> bool:
    """Whether this exception means "the document was already there".

    Matched by name rather than by class so the in-memory double used in the unit tests can
    raise its own error: importing ``google.api_core`` here would make a Firestore package a
    hard dependency of a module the doubles exercise.
    """
    name = type(exc).__name__
    return "AlreadyExists" in name or "Conflict" in name
