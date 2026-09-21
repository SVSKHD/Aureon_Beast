"""Where named operational conditions live (11A, F-15).

One document per condition, keyed by what it is about, so a service that restarts mid-condition
finds the existing row rather than announcing it again.

## Read-then-write, not a transaction

Two services CAN legitimately raise the same named condition -- ``firestore_unavailable`` is
true for whichever process cannot write -- and the last writer wins. That is correct here and
would be wrong for a trade: the worst outcome of a lost race is one duplicate ops message, and
paying a transaction per poll for ten conditions across four processes would cost far more than
the duplicate is worth. Nothing gates on these documents (see ``aureon/models/ops.py``), which
is what makes the cheap answer the right one.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.ops import OpsEvent
from aureon.storage import paths

log = logging.getLogger(__name__)


class OpsEventRepository:
    """Reads and writes ``{prefix}_ops_events``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self, name: str, scope: str | None = None) -> OpsEvent | None:
        snapshot = self._client.document(paths.ops_event_path(name, scope)).get()
        if not getattr(snapshot, "exists", False):
            return None
        return OpsEvent.model_validate(snapshot.to_dict())

    def write(self, event: OpsEvent, *, now: datetime | None = None) -> OpsEvent:
        stamped = event.model_copy(update={"updated_at": to_utc(now or utc_now())})
        self._client.document(
            paths.ops_event_path(stamped.name, stamped.scope)
        ).set(stamped.model_dump(mode="json"))
        return stamped

    def all_events(self) -> list[OpsEvent]:
        """Every condition ever recorded, active first, then by name.

        Cleared conditions are kept and shown rather than deleted: "this has not happened
        since the deployment started" and "this happened twice this morning and cleared" are
        different things for an operator to know, and a collection holding only active rows
        cannot tell them apart.
        """
        found: list[OpsEvent] = []
        for doc in self._client.collection(paths.OPS_EVENTS).stream():
            try:
                found.append(OpsEvent.model_validate(doc.to_dict()))
            except Exception:  # noqa: BLE001 - one bad row must not hide the rest
                log.warning("unreadable ops event %s", getattr(doc, "id", "?"))
        return sorted(found, key=lambda e: (not e.active, e.name, e.scope or ""))
