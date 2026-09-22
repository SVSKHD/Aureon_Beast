"""Setups and their events, and the transaction that moves them (plan §10-§12).

This file is why the migration is worth doing. The Firestore repository's own docstring
admitted that its unit tests ran the read-check-write with **no isolation at all**, because
the transaction wrapper only worked against Google's transaction object -- so the
atomicity was asserted nowhere except an emulator suite. Here ``FOR UPDATE`` is real in
every test below, on the same code path production runs.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aureon.models.enums import (
    SetupEventType,
    SetupState,
    TransitionError,
)
from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.detections import DetectionRepository
from aureon.storage.postgres.repositories.setups import MissingSetup, SetupRepository
from tests.postgres.contention import interleave
from tests.postgres.factories import CLOSE, a_detection, a_setup, an_event

#: Short aliases; the enum names are long enough that the calls below wrap badly without them.
E = SetupEventType
S = SetupState

pytestmark = pytest.mark.postgres


@pytest.fixture
def repo(schema: Database) -> SetupRepository:
    return SetupRepository(schema)


# ── Opening ───────────────────────────────────────────────────────────────────


def test_a_setup_survives_the_database_unchanged(repo: SetupRepository) -> None:
    written = repo.open(a_setup())
    assert repo.get("s1") == written


def test_opening_twice_returns_the_first_rather_than_raising(repo: SetupRepository) -> None:
    """The id is a pure function of what opened it, so a re-processed candle produces the
    same setup. Refusing would turn a restart into an error the observer must special-case."""
    first = repo.open(a_setup())
    again = repo.open(a_setup(anchor=first.anchor.model_copy(update={"price": 9999.0})))
    assert again.anchor.price == 2400.0, "the second open overwrote the first"
    assert repo.get("s1") == first


# ── The transaction (§12) ─────────────────────────────────────────────────────


def test_recording_an_event_moves_the_setup_and_stores_the_event(
    repo: SetupRepository,
) -> None:
    repo.open(a_setup())
    setup, event, applied = repo.record(an_event())

    assert applied is True
    assert setup.state is SetupState.WATCH
    assert setup.event_count == 1
    assert setup.last_event_id == "e1"
    assert repo.events.get("e1") == event
    assert repo.get("s1") == setup


def test_the_from_state_is_taken_from_the_locked_setup_not_the_caller(
    repo: SetupRepository,
) -> None:
    """A caller cannot know what the setup's state was; only the transaction can.

    So ``from_state`` is overwritten from the row it locked. A version that trusted the
    caller would record a history that disagrees with the setup it describes, and the
    disagreement would only appear in a timeline a human read weeks later.
    """
    repo.open(a_setup())
    repo.record(an_event("e1", to_state=S.WATCH))
    _, event, _ = repo.record(
        an_event("e2", to_state=S.DEVELOPING, event_type=E.DEVELOPING)
    )
    assert event.from_state is SetupState.WATCH, "it trusted the caller's from_state"


def test_an_illegal_transition_is_refused_and_writes_nothing(
    repo: SetupRepository,
) -> None:
    """CLAUDE.md: every state change passes ``assert_transition``.

    And the refusal must leave NOTHING behind -- if the event survived a rejected
    transition, the setup would have a history entry for a move that never happened.
    """
    repo.open(a_setup())
    with pytest.raises(TransitionError):
        repo.record(
            an_event("e1", to_state=S.COMPLETED, event_type=E.COMPLETED)
        )
    assert repo.events.get("e1") is None
    setup = repo.get("s1")
    assert setup is not None and setup.state is SetupState.OBSERVING
    assert setup.event_count == 0


def test_an_event_against_a_missing_setup_is_refused(repo: SetupRepository) -> None:
    with pytest.raises(MissingSetup):
        repo.record(an_event("e1", "no-such-setup"))
    assert repo.events.get("e1") is None


# ── Idempotence ───────────────────────────────────────────────────────────────


def test_replaying_the_same_event_changes_nothing(repo: SetupRepository) -> None:
    """The event id is deterministic over (setup, candle close, type), so a re-processed
    candle addresses the same event. ``applied`` is False and nothing moves."""
    repo.open(a_setup())
    first, _, applied_first = repo.record(an_event())
    second, _, applied_second = repo.record(an_event())

    assert applied_first is True
    assert applied_second is False
    assert second.event_count == 1, "the replay counted twice"
    assert second.state is first.state
    assert repo.events.count_for_setup("s1") == 1


def test_a_replay_does_not_re_run_the_transition_gate(repo: SetupRepository) -> None:
    """The subtle half of idempotence, and the one a partial fix breaks.

    A version that skipped only the event INSERT would then evaluate
    ``assert_transition`` from the state the first write produced -- WATCH → WATCH, which
    is not a legal edge -- so a restart would raise instead of doing nothing.
    """
    repo.open(a_setup())
    repo.record(an_event())
    setup, event, applied = repo.record(an_event())  # must not raise
    assert applied is False
    assert event.event_id == "e1"
    assert setup.state is SetupState.WATCH


# ── Two writers ───────────────────────────────────────────────────────────────


class _Signalling(SetupRepository):
    """A repository that announces when it has taken its lock, and is still inside it.

    Subclassed rather than hooked into production code: the signal happens after
    ``super()._locked_setup`` has issued the real ``SELECT ... FOR UPDATE``, so the second
    writer starts while this one provably holds the row.
    """

    def __init__(self, database: Database, signal) -> None:  # type: ignore[no-untyped-def]
        super().__init__(database)
        self._signal = signal

    def _locked_setup(self, setup_id: str, connection):  # type: ignore[no-untyped-def]
        row = super()._locked_setup(setup_id, connection)
        self._signal()
        return row


def test_two_writers_on_one_setup_do_not_both_apply(schema: Database) -> None:
    """§12's actual guarantee, with a race that really overlaps (decision 352).

    The first writer holds its ``FOR UPDATE`` lock while the second tries to take it. With
    the lock, the second WAITS -- not skips -- then re-reads the state the first wrote, so
    its transition is evaluated against reality. Exactly one move lands.

    Without the lock the second reads the pre-move state and both apply, which is how one
    setup ends up with two events claiming the same ``from_state`` and a history that
    contradicts the setup it describes.
    """
    SetupRepository(schema).open(a_setup())

    outcome = interleave(
        lambda signal: _Signalling(schema, signal).record(
            an_event("a", to_state=S.WATCH, event_type=E.WATCH_STARTED)
        ),
        lambda: SetupRepository(schema).record(
            an_event("b", to_state=S.DEVELOPING, event_type=E.DEVELOPING)
        ),
    )

    applied = [r for r in outcome.results.values() if r[2] is True]
    assert len(applied) + len(outcome.failed) == 2, outcome

    final = SetupRepository(schema).get("s1")
    events = SetupRepository(schema).events.for_setup("s1")
    assert final is not None
    assert final.event_count == len(events), "the counter and the history disagree"
    assert final.last_event_id == events[-1].event_id

    # The assertion the missing lock breaks: no two events may claim the same starting state.
    froms = [event.from_state for event in events]
    assert len(froms) == len(set(froms)), f"two events share a from_state: {froms}"

    # And the second writer must have seen the first's move, not the state before it.
    if len(events) == 2:
        assert events[1].from_state is events[0].to_state, (
            "the second writer read a state that the first had already replaced"
        )


def test_two_writers_replaying_the_same_event_apply_it_once(schema: Database) -> None:
    """The idempotent path, under a real overlap.

    Two observers recovering after a restart both replay the same candle. Exactly one may
    insert the event; the other must take the idempotent branch rather than colliding on the
    primary key and surfacing an IntegrityError to a service with no reason to expect one.
    """
    SetupRepository(schema).open(a_setup())

    outcome = interleave(
        lambda signal: _Signalling(schema, signal).record(an_event()),
        lambda: SetupRepository(schema).record(an_event()),
    )

    assert outcome.failed == [], f"a replay raised: {outcome.errors}"
    applied = sorted(result[2] for result in outcome.results.values())
    assert applied == [False, True], applied
    assert SetupRepository(schema).events.count_for_setup("s1") == 1


# ── What rides along in the same transaction ──────────────────────────────────


def test_the_parent_updates_land_with_the_event(repo: SetupRepository) -> None:
    """A separate update afterwards would be a second write outside the transaction, and a
    crash between the two would leave a setup whose state moved and whose invalidation
    price did not."""
    repo.open(a_setup())
    setup, _, _ = repo.record(an_event(), invalidation_price=2395.0)
    assert setup.invalidation_price == 2395.0
    stored = repo.get("s1")
    assert stored is not None and stored.invalidation_price == 2395.0


def test_a_linked_detection_is_added_without_losing_the_others(
    schema: Database,
) -> None:
    repo = SetupRepository(schema)
    detections = DetectionRepository(schema)
    detections.upsert(a_detection("d1"))
    detections.upsert(a_detection("d2"))

    repo.open(a_setup())
    repo.record(an_event("e1"), linked_detection_id="d1")
    setup, _, _ = repo.record(
        an_event("e2", to_state=S.DEVELOPING, event_type=E.DEVELOPING),
        linked_detection_id="d2",
    )
    assert sorted(setup.linked_detection_ids) == ["d1", "d2"]


def test_confirmed_at_is_stamped_once_and_never_re_stamped(repo: SetupRepository) -> None:
    """12 T-9. A setup that re-confirms is the same claim, made once.

    Re-stamping would move the moment a review measures the outcome from, which would
    change the measured result of a setup nobody touched.

    The path is ``CONFIRMED → FAKEOUT_RISK → CONFIRMED``, which is the only route back into
    CONFIRMED that the state machine allows -- ``PULLBACK`` leads to CONTINUATION, not back
    to a fresh confirmation. Worth stating, because the first version of this test assumed
    the pullback route, and the machine refused it: a setup that pulls back has not stopped
    being confirmed, so there is nothing to re-confirm. A fakeout-risk setup HAS, which is
    exactly why the guard is reachable and needed.
    """
    repo.open(a_setup())
    repo.record(an_event("e1"))
    repo.record(an_event("e2", to_state=S.DEVELOPING, event_type=E.DEVELOPING))
    confirmed, _, _ = repo.record(
        an_event("e3", to_state=S.CONFIRMED, event_type=E.CONFIRMED)
    )
    first_stamp = confirmed.confirmed_at
    assert first_stamp is not None

    repo.record(
        an_event("e4", to_state=S.FAKEOUT_RISK, event_type=E.FAKEOUT_RISK, minutes=10)
    )
    again, _, _ = repo.record(
        an_event("e5", to_state=S.CONFIRMED, event_type=E.CONFIRMED, minutes=15)
    )
    assert again.state is S.CONFIRMED
    assert again.confirmed_at == first_stamp, "the second confirmation re-stamped it"


def test_a_terminal_state_stamps_closed_at(repo: SetupRepository) -> None:
    repo.open(a_setup())
    invalidated, _, _ = repo.record(
        an_event("e1", to_state=S.INVALIDATED, event_type=E.INVALIDATED)
    )
    assert invalidated.closed_at is not None
    assert invalidated.confirmed_at is None, "it never confirmed"


# ── The history ───────────────────────────────────────────────────────────────


def test_events_come_back_oldest_first(repo: SetupRepository) -> None:
    """A timeline is read forwards; the card that shows the last few takes the tail."""
    repo.open(a_setup())
    repo.record(an_event("e1", minutes=5))
    repo.record(
        an_event("e2", to_state=S.DEVELOPING, event_type=E.DEVELOPING, minutes=10)
    )
    assert [e.event_id for e in repo.events.for_setup("s1")] == ["e1", "e2"]


def test_one_setups_events_are_not_anothers(repo: SetupRepository) -> None:
    repo.open(a_setup("s1"))
    repo.open(a_setup("s2"))
    repo.record(an_event("e1", "s1"))
    repo.record(an_event("e2", "s2"))
    assert [e.event_id for e in repo.events.for_setup("s1")] == ["e1"]
    assert [e.event_id for e in repo.events.for_setup("s2")] == ["e2"]


def test_the_event_repository_cannot_write(repo: SetupRepository) -> None:
    """An event written without moving its setup would be a history that disagrees with
    the thing it is a history of. The only way to make that impossible is for the write to
    live in the transaction that does both -- so the reader offers no public write."""
    offered = {name for name in dir(repo.events) if not name.startswith("_")}
    assert not ({"upsert", "record", "insert", "create", "delete"} & offered), sorted(offered)


# ── Queries ───────────────────────────────────────────────────────────────────


def test_open_for_symbol_excludes_terminal_setups(repo: SetupRepository) -> None:
    repo.open(a_setup("live"))
    repo.open(a_setup("done"))
    repo.record(
        an_event("e1", "done", to_state=S.INVALIDATED, event_type=E.INVALIDATED)
    )
    assert [s.setup_id for s in repo.open_for_symbol("XAUUSD")] == ["live"]


def test_a_period_counts_setups_by_when_they_opened(repo: SetupRepository) -> None:
    """A setup belongs to the period it appeared in. Keying on ``updated_at`` would let one
    that was still moving a week later migrate into the next review and out of the one
    that first saw it."""
    repo.open(a_setup("s1"))
    repo.record(an_event("e1", minutes=60))
    found = repo.in_period(CLOSE - timedelta(minutes=1), CLOSE + timedelta(minutes=1))
    assert [s.setup_id for s in found] == ["s1"]


def test_changed_since_sees_a_setup_that_moved(repo: SetupRepository) -> None:
    """The notifier's sweep (12 T-11).

    The clock is supplied rather than left to ``utc_now()``: the fixture's instants are in
    2026 and the machine running the test is not, so a real ``utc_now()`` would stamp
    ``updated_at`` BEFORE the window and the sweep would correctly find nothing -- a test
    that passed for the wrong reason, or in this case failed for one.
    """
    repo.open(a_setup())
    assert repo.changed_since(CLOSE) == []
    repo.record(an_event(), now=CLOSE + timedelta(minutes=5))
    assert [s.setup_id for s in repo.changed_since(CLOSE)] == ["s1"]
    # And a window that starts after the move sees nothing.
    assert repo.changed_since(CLOSE + timedelta(minutes=10)) == []
