"""Setup state and its event history, written together (12, T-6).

One operation matters here and the rest is arrangement around it: **record**, which moves a
setup's state and appends the event that explains the move, inside a single Firestore
transaction. Two writes, one atom.

## Why it has to be one transaction

The document is a summary and the events are the history. If they can disagree, both become
useless: a setup sitting in CONFIRMED with no CONFIRMED event is a state nobody can explain, and
a CONFIRMED event on a setup still reading DEVELOPING is a history that contradicts the thing it
is meant to describe. Either failure is silent — nothing raises, the card renders, the review
counts it — which is exactly the class of bug a transaction exists to remove.

The same reasoning §60 applies to audit rows on the execution path: not afterwards, in the same
transaction, because a crash between two writes loses the account of a change that did happen.

## Idempotent on the event id, not on a counter

``setup_event_id`` is a hash over (setup, candle close, event type). The write uses
create-if-absent, so a candle re-processed after a restart, or replayed from the archive,
addresses the document that is already there and the whole operation becomes a no-op — including
the state change, because the transaction reads the event first and returns early.

That last part is the subtle half. A repository that skipped only the event write would still
apply the state change a second time, and the second application would hit
``assert_transition`` from the state the first one produced — turning a harmless re-processing
into a raised ``TransitionError`` on every restart.

## What is deliberately not here

No detection writes. A setup advancing REFERENCES the detection that advanced it and never edits
it: detections are immutable (CLAUDE.md), and the reference is one-way for that reason. No broker
anything — this module is on the observation side, and a boundary test checks that the setup
engine above it imports nothing from ``aureon.execution``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.enums import TERMINAL_SETUP_STATES, SetupState, assert_setup_transition
from aureon.models.setup import Setup, SetupEvent
from aureon.storage import paths

log = logging.getLogger(__name__)


class SetupRepository:
    """Reads and writes ``{prefix}_setups`` and its ``events`` sub-collection."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # ── reads ─────────────────────────────────────────────────────────────────

    def get(self, setup_id: str) -> Setup | None:
        snapshot = self._client.document(paths.setup_path(setup_id)).get()
        if not getattr(snapshot, "exists", False):
            return None
        return Setup.model_validate(snapshot.to_dict())

    def get_event(self, setup_id: str, event_id: str) -> SetupEvent | None:
        snapshot = self._client.document(
            paths.setup_event_path(setup_id, event_id)
        ).get()
        if not getattr(snapshot, "exists", False):
            return None
        return SetupEvent.model_validate(snapshot.to_dict())

    def events(self, setup_id: str, *, limit: int | None = None) -> list[SetupEvent]:
        """A setup's events, OLDEST first.

        Oldest first because this is a history and a history reads forwards; a card that wants
        the last few takes them off the end. ``limit`` is applied after the ordering by the
        query, never by slicing here -- slicing a newest-first page to get the oldest N is the
        mistake the fake's own comment warns about.
        """
        query = self._client.collection(paths.setup_events_path(setup_id)).order_by(
            "market_time.utc"
        )
        if limit is not None:
            query = query.limit(limit)
        found: list[SetupEvent] = []
        for document in query.stream():
            try:
                found.append(SetupEvent.model_validate(document.to_dict()))
            except Exception:  # noqa: BLE001 - one bad row must not hide the rest
                log.warning(
                    "unreadable setup event %s/%s",
                    setup_id,
                    getattr(document, "id", "?"),
                    exc_info=True,
                )
        return found

    def open_setups(
        self, *, symbol: str, market_date: str | None = None
    ) -> list[Setup]:
        """Every setup for this symbol that is not in a terminal state.

        What the engine reads at the top of each candle to find what it is already tracking.
        Filtered on ``state`` with a ``not-in`` rather than on a boolean "closed" field, so the
        state machine stays the single source of truth about what terminal means -- a second
        field would be a second answer, and the two would disagree the first time one was
        forgotten.
        """
        query = self._client.collection(paths.SETUPS).where("symbol", "==", symbol.upper())
        if market_date is not None:
            query = query.where("market_date", "==", market_date)
        query = query.where(
            "state", "not-in", [state.value for state in sorted(TERMINAL_SETUP_STATES)]
        )
        found: list[Setup] = []
        for document in query.stream():
            try:
                found.append(Setup.model_validate(document.to_dict()))
            except Exception:  # noqa: BLE001
                log.warning(
                    "unreadable setup %s", getattr(document, "id", "?"), exc_info=True
                )
        return sorted(found, key=lambda one: one.opened_at)

    # ── writes ────────────────────────────────────────────────────────────────

    def open(self, setup: Setup, *, now: datetime | None = None) -> Setup:
        """Create the setup if it is not already there; return whichever one exists.

        Create-if-absent rather than ``set``: the id is deterministic, so the engine derives the
        same one every candle, and a ``set`` would overwrite a setup that had already advanced
        to CONFIRMED with a fresh OBSERVING one. That is not a hypothetical -- it is what happens
        on the second candle of every setup's life.
        """
        existing = self.get(setup.setup_id)
        if existing is not None:
            return existing
        stamped = setup.model_copy(update={"updated_at": to_utc(now or utc_now())})
        reference = self._client.document(paths.setup_path(setup.setup_id))
        try:
            reference.create(stamped.model_dump(mode="json"))
        except Exception as exc:  # noqa: BLE001
            if not _already_exists(exc):
                raise
            # Another process (or this one, racing itself across a restart) created it first.
            # Its version is as valid as ours and is further along by definition.
            return self.get(setup.setup_id) or stamped
        return stamped

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
        """Append one event and move the setup, atomically. Returns ``(setup, event, applied)``.

        ``applied`` is False when the event was already there, which is the idempotent path: a
        re-processed candle changes nothing and raises nothing.

        Everything the caller may want to update on the parent rides along as a keyword, because
        a separate ``update`` call afterwards would be a second write outside the transaction --
        and a crash between the two would leave a setup whose state moved and whose invalidation
        price did not.
        """
        moment = to_utc(now or utc_now())
        setup_reference = self._client.document(paths.setup_path(event.setup_id))
        event_reference = self._client.document(
            paths.setup_event_path(event.setup_id, event.event_id)
        )

        def apply(transaction: Any) -> tuple[Setup, SetupEvent, bool]:
            existing_event = event_reference.get(transaction=transaction)
            if getattr(existing_event, "exists", False):
                # Idempotent: return what is stored, and do NOT re-apply the state change. A
                # version that skipped only the event write would hit assert_transition from
                # the state the first write produced, turning a restart into a raised error.
                stored_setup = setup_reference.get(transaction=transaction)
                current = (
                    Setup.model_validate(stored_setup.to_dict())
                    if getattr(stored_setup, "exists", False)
                    else None
                )
                if current is None:
                    raise MissingSetup(
                        f"{event.setup_id}: the event exists and the setup does not"
                    )
                return current, SetupEvent.model_validate(existing_event.to_dict()), False

            snapshot = setup_reference.get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                raise MissingSetup(f"{event.setup_id}: no such setup; open it first")
            current = Setup.model_validate(snapshot.to_dict())

            if event.to_state is not current.state:
                # The gate every status write in this system passes through (CLAUDE.md). A
                # descriptive event has to_state == state, so it never reaches this.
                assert_setup_transition(current.state, event.to_state)

            stored_event = event.model_copy(
                update={
                    "from_state": current.state,
                    "created_at": event.created_at or moment,
                }
            )
            updates: dict[str, Any] = {
                "state": event.to_state,
                "updated_at": moment,
                "last_event_id": stored_event.event_id,
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
            if event.to_state in TERMINAL_SETUP_STATES:
                updates["closed_at"] = moment
            moved = current.model_copy(update=updates)

            transaction.create(event_reference, stored_event.model_dump(mode="json"))
            transaction.set(setup_reference, moved.model_dump(mode="json"))
            return moved, stored_event, True

        return self._run(self._transaction(), apply)

    # ── transaction plumbing ──────────────────────────────────────────────────

    def _transaction(self) -> Any:
        return self._client.transaction()

    @staticmethod
    def _run(transaction: Any, fn: Callable[..., Any], *args: Any) -> Any:
        """Run ``fn`` inside a Firestore transaction, if this client has real ones.

        ``google.cloud.firestore.transactional`` retries the callable if its read set changed,
        which is what makes the read-check-write above atomic against a second observer. That
        wrapper drives Google's own transaction protocol, so it only works on Google's transaction
        object; handed anything else it fails on an attribute nobody would recognise
        (``_read_only``).

        So the capability is checked rather than assumed, and a client whose ``transaction()``
        is not Google's gets the callable run directly. Be clear about what that costs: the
        logic above -- the idempotent early return, the transition gate, the ``from_state``
        overwrite -- is then exercised with no isolation at all. That is worth having in a unit
        test, because it is a great deal of reasoning to leave untested until an emulator is up.
        What it does NOT prove is atomicity, and it must never be mistaken for proving it:
        ``tests/failure_injection`` is where two writers race for real.
        """
        if not hasattr(transaction, "_read_only"):
            return fn(transaction, *args)
        from google.cloud import firestore
        return firestore.transactional(fn)(transaction, *args)


class MissingSetup(LookupError):
    """An event was recorded against a setup that does not exist."""


def _already_exists(exc: BaseException) -> bool:
    """Whether this is Firestore's "document already exists".

    Matched by NAME, the way the notification repository does it, so this module stays importable
    without ``google.api_core`` -- and so the in-memory double can raise its own exception of the
    same name and exercise the same branch.
    """
    seen: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(seen) < 10:
        seen.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return any("AlreadyExists" in name for name in seen)


def latest_state(events: Sequence[SetupEvent]) -> SetupState | None:
    """The state the last state-changing event left, or ``None`` for no events.

    Used by the verifier and by tests to check the document against its own history. Descriptive
    events are skipped, because they do not change the state and the last one is usually a
    proximity observation.
    """
    for event in reversed(list(events)):
        if not event.event_type.is_watch_event:
            return event.to_state
    return None
