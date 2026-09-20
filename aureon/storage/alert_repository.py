"""``/remind`` price alerts (9C).

Two writers, and they must not tread on each other. Discord arms and cancels; the **observer**
fires and expires, because firing needs the quotes the observer is already reading and Discord
may not call the broker (CLAUDE.md).

## Firing is transactional, and that is the whole point

An alert must be answered **once**. Two observer instances polling the same quote would both
see the level crossed, and a read-then-write would have both freeze a snapshot and both tell
Discord. So ``fire`` reads the document inside a transaction, refuses unless it is still
ARMED, and writes the FIRED status with its snapshot in the same commit. The loser's
transaction aborts because its read set changed -- the same discipline the executor's claim
uses for money, applied here to a message.

Every status change goes through ``assert_price_alert_transition`` (CLAUDE.md), so a cancelled
alert cannot be fired and a fired one cannot be re-armed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from aureon.models.alerts import (
    DEFAULT_ALERT_TTL_HOURS,
    MAX_ARMED_ALERTS_PER_USER,
    PriceAlert,
)
from aureon.models.base import to_utc, utc_now
from aureon.models.enums import (
    PriceAlertStatus,
    assert_price_alert_transition,
)
from aureon.models.identity import new_alert_id
from aureon.storage import paths

log = logging.getLogger(__name__)

#: Re-exported so a caller that already holds this repository need not reach for the models
#: package for an id. The definition lives in ``aureon.models.identity`` with the others.
__all__ = ["AlertClaimRejected", "AlertRejected", "PriceAlertRepository", "new_alert_id"]


class AlertRejected(Exception):
    """The alert cannot be armed, or cannot be changed as asked."""


class AlertClaimRejected(Exception):
    """Someone else already answered or withdrew this alert."""


class PriceAlertRepository:
    """Reads and writes ``{prefix}_alerts``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # ── Reading ───────────────────────────────────────────────────────────────

    def get(self, alert_id: str) -> PriceAlert | None:
        snapshot = self._client.document(paths.alert_path(alert_id)).get()
        if not getattr(snapshot, "exists", False):
            return None
        return PriceAlert.model_validate(snapshot.to_dict())

    def armed(self, *, symbol: str | None = None, user_id: str | None = None) -> list[PriceAlert]:
        """Every ARMED alert, optionally narrowed.

        Filtered in Python after a single-field query, for the reason the reviews are: a
        composite index would have to be declared and deployed for a collection that holds
        tens of documents.
        """
        query = self._client.collection(paths.ALERTS)
        query = _where(query, "status", "==", PriceAlertStatus.ARMED.value)
        found = [PriceAlert.model_validate(doc.to_dict()) for doc in query.stream()]
        if symbol is not None:
            wanted = symbol.upper()
            found = [a for a in found if a.symbol.upper() == wanted]
        if user_id is not None:
            found = [a for a in found if a.requested_by == str(user_id)]
        return sorted(found, key=lambda a: (a.symbol, a.level, a.alert_id))

    def for_user(self, user_id: str) -> list[PriceAlert]:
        """Every alert of one user, armed or not -- what ``/remind list`` shows."""
        query = _where(
            self._client.collection(paths.ALERTS), "requested_by", "==", str(user_id)
        )
        found = [PriceAlert.model_validate(doc.to_dict()) for doc in query.stream()]
        return sorted(found, key=lambda a: (a.status.value, a.symbol, a.level))

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
        """
        moment = to_utc(now or utc_now())
        existing = self.armed(user_id=alert.requested_by)
        if len(existing) >= max_per_user:
            raise AlertRejected(
                f"{alert.requested_by} already has {len(existing)} armed alerts "
                f"(the limit is {max_per_user}). Cancel one with `/remind cancel`."
            )
        stored = alert.model_copy(
            update={
                "status": PriceAlertStatus.ARMED,
                "created_at": moment,
                "expires_at": alert.expires_at or moment + timedelta(hours=ttl_hours),
            }
        )
        self._client.document(paths.alert_path(stored.alert_id)).set(
            stored.model_dump(mode="json")
        )
        log.info(
            "armed alert %s: %s %s %s", stored.alert_id, stored.symbol, stored.side,
            stored.level,
        )
        return stored

    # ── Answering ─────────────────────────────────────────────────────────────

    @staticmethod
    def _run(transaction: Any, fn: Callable[..., Any], *args: Any) -> Any:
        from google.cloud import firestore

        return firestore.transactional(fn)(transaction, *args)

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

        def txn(transaction: Any) -> PriceAlert:
            ref = self._client.document(paths.alert_path(alert_id))
            doc = ref.get(transaction=transaction)
            if not getattr(doc, "exists", False):
                raise AlertClaimRejected(f"no alert {alert_id}")
            current = PriceAlert.model_validate(doc.to_dict())
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
            transaction.set(ref, fired.model_dump(mode="json"))
            return fired

        return self._run(self._client.transaction(), txn)

    def cancel(self, alert_id: str, *, actor: str, now: datetime | None = None) -> PriceAlert:
        """ARMED → CANCELLED, and only by the user who armed it (§71)."""
        moment = to_utc(now or utc_now())

        def txn(transaction: Any) -> PriceAlert:
            ref = self._client.document(paths.alert_path(alert_id))
            doc = ref.get(transaction=transaction)
            if not getattr(doc, "exists", False):
                raise AlertClaimRejected(f"no alert {alert_id}")
            current = PriceAlert.model_validate(doc.to_dict())
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
            transaction.set(ref, cancelled.model_dump(mode="json"))
            return cancelled

        _ = moment
        return self._run(self._client.transaction(), txn)

    def expire_due(self, *, now: datetime | None = None) -> list[str]:
        """Expire every ARMED alert past its ``expires_at``. Returns the ids (9C).

        Run by the observer on candle close rather than by a timer, so expiry happens on the
        same clock as everything else the observer records and needs no second scheduler.
        """
        moment = to_utc(now or utc_now())
        expired: list[str] = []
        for alert in self.armed():
            if alert.expires_at is None or alert.expires_at > moment:
                continue
            try:
                self._expire(alert.alert_id, now=moment)
                expired.append(alert.alert_id)
            except AlertClaimRejected:
                # Fired or cancelled between the read and the write. Nothing to do, and
                # certainly nothing to overwrite.
                continue
        return expired

    def _expire(self, alert_id: str, *, now: datetime) -> PriceAlert:
        def txn(transaction: Any) -> PriceAlert:
            ref = self._client.document(paths.alert_path(alert_id))
            doc = ref.get(transaction=transaction)
            if not getattr(doc, "exists", False):
                raise AlertClaimRejected(f"no alert {alert_id}")
            current = PriceAlert.model_validate(doc.to_dict())
            if current.status is not PriceAlertStatus.ARMED:
                raise AlertClaimRejected(f"alert {alert_id} is {current.status.value}")
            assert_price_alert_transition(current.status, PriceAlertStatus.EXPIRED)
            done = current.model_copy(update={"status": PriceAlertStatus.EXPIRED})
            transaction.set(ref, done.model_dump(mode="json"))
            return done

        _ = now
        return self._run(self._client.transaction(), txn)


def _where(query: Any, field: str, op: str, value: Any) -> Any:
    """``where`` across client versions, as the other repositories do."""
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter

        return query.where(filter=FieldFilter(field, op, value))
    except Exception:  # noqa: BLE001 - the in-memory double has the old signature
        return query.where(field, op, value)
