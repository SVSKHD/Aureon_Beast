"""Once on onset, once on recovery, never in between (11A, F-15).

The whole value of this register is the *absence* of messages. A condition that persists for
six hours must produce two lines, not six hours of identical ones — an operator who gets the
latter mutes the channel, and a muted channel is strictly worse than no channel because it
looks like coverage.

So almost every test here counts messages rather than reading them, and the ones that matter
most assert a count of **zero**.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.services.ops_events import (
    BY_NAME,
    SPECS,
    OpsRegister,
    UnknownOpsEvent,
    render_ops,
)
from aureon.storage.ops_repository import OpsEventRepository

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
SYMBOL = "XAUUSD"


class Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class Posted:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, message: str) -> None:
        self.messages.append(message)


@pytest.fixture
def register(firestore):
    """A register over the in-memory store, with a pinned clock."""

    def build(*, service: str = "observer", clock: Clock | None = None):
        return OpsRegister(
            OpsEventRepository(firestore),
            service=service,
            post=Posted(),
            now=clock or Clock(),
        )

    return build


# ── The rule ──────────────────────────────────────────────────────────────────


def test_a_condition_that_persists_is_announced_once(register) -> None:
    """The property the whole design exists for."""
    ops = register()
    clock = ops._now

    first = ops.observe("observer_stale", active=True, detail="no candle in 11m")
    assert first.edge == "onset"

    for _ in range(20):
        clock.advance(seconds=30)
        assert ops.observe("observer_stale", active=True).edge is None

    assert len(ops.post.messages) == 1


def test_recovery_is_announced_once_and_then_stays_quiet(register) -> None:
    ops = register()
    ops.observe("observer_stale", active=True)
    recovery = ops.observe("observer_stale", active=False)
    assert recovery.edge == "recovery"

    for _ in range(5):
        assert ops.observe("observer_stale", active=False).edge is None
    assert len(ops.post.messages) == 2


def test_a_condition_that_never_becomes_true_says_nothing(register) -> None:
    """A register polled every second all day, on a healthy system, posts nothing."""
    ops = register()
    for _ in range(100):
        assert ops.observe("observer_stale", active=False).edge is None
    assert ops.post.messages == []


def test_a_second_onset_is_announced_and_counted(register) -> None:
    """A condition that flaps is a different problem from one that started once and stayed,
    and both read as "active" without the count."""
    ops = register()
    ops.observe("outbox_backlog", active=True)
    ops.observe("outbox_backlog", active=False)
    second = ops.observe("outbox_backlog", active=True)

    assert second.edge == "onset"
    assert "onset #2" in second.message
    assert len(ops.post.messages) == 3


def test_since_records_when_the_state_began_not_when_it_was_last_seen(
    register, firestore
) -> None:
    """An onset three hours old must read as three hours old. A field that ticked forward on
    every poll would always read as new."""
    ops = register()
    clock = ops._now
    ops.observe("monitor_stale", active=True)
    started = clock.now

    for _ in range(6):
        clock.advance(minutes=30)
        ops.observe("monitor_stale", active=True)

    stored = OpsEventRepository(firestore).get("monitor_stale")
    assert stored.since == started


def test_a_persisting_condition_costs_no_further_writes(register, firestore) -> None:
    """One write per TRANSITION, not per poll.

    Ten conditions polled every second across four processes would otherwise be forty
    Firestore writes a second, for a value that has not changed — the same reasoning the
    ``system_state`` throttle rests on. ``since`` already answers "how long has this been
    true", so there is nothing a refresh would add.
    """
    ops = register()
    clock = ops._now
    ops.observe("monitor_stale", active=True)
    after_onset = firestore.writes

    for _ in range(50):
        clock.advance(seconds=1)
        ops.observe("monitor_stale", active=True)
    assert firestore.writes == after_onset, "a persisting condition wrote again"

    ops.observe("monitor_stale", active=False)
    assert firestore.writes == after_onset + 1


def test_a_changed_detail_is_recorded_without_re_announcing(register, firestore) -> None:
    """The backlog growing from 60 to 900 is worth showing on `/ops` and is not worth a
    second message: the condition is the same one."""
    ops = register()
    clock = ops._now
    ops.observe("outbox_backlog", active=True, detail="60 undelivered")
    started = clock.now

    clock.advance(hours=3)
    assert ops.observe("outbox_backlog", active=True, detail="900 undelivered").edge is None

    assert len(ops.post.messages) == 1
    stored = OpsEventRepository(firestore).get("outbox_backlog")
    assert stored.detail == "900 undelivered"
    # And the refresh must NOT move `since`. This is the path where it would be easy to:
    # the row is being rewritten anyway, and stamping `moment` on it would make a
    # three-hour-old onset read as new on every poll where the detail happened to change.
    assert stored.since == started


def test_a_restart_mid_condition_does_not_re_announce(register, firestore) -> None:
    """The reason the state is a document rather than a variable.

    A new process genuinely does not know whether the condition was already active. Reading
    the row tells it, so a deploy during an outage does not re-alert.
    """
    first = register()
    first.observe("firestore_unavailable", active=True)
    assert len(first.post.messages) == 1

    after_restart = register()
    assert after_restart.observe("firestore_unavailable", active=True).edge is None
    assert after_restart.post.messages == []

    # ...and the recovery still lands, in the new process.
    assert after_restart.observe("firestore_unavailable", active=False).edge == "recovery"
    assert len(after_restart.post.messages) == 1


# ── Every named condition ─────────────────────────────────────────────────────


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_every_named_condition_has_an_onset_and_a_recovery(spec, register) -> None:
    """One test per event, as the phase asks. A spec added without its sentences, or an event
    that cannot round-trip, fails here rather than at 03:00."""
    ops = register()
    scope = SYMBOL if spec.scoped else None

    onset = ops.observe(spec.name, active=True, scope=scope, detail="threshold crossed")
    assert onset.edge == "onset"
    assert spec.onset in onset.message

    assert ops.observe(spec.name, active=True, scope=scope).edge is None

    recovery = ops.observe(spec.name, active=False, scope=scope)
    assert recovery.edge == "recovery"
    assert spec.recovery in recovery.message

    assert len(ops.post.messages) == 2


def test_all_ten_conditions_the_phase_names_exist() -> None:
    assert set(BY_NAME) == {
        "observer_stale",
        "executor_stale",
        "monitor_stale",
        "firestore_unavailable",
        "outbox_backlog",
        "reconciliation_ambiguous",
        "archive_write_failed",
        "symbol_feed_stale",
        "mt5_reconnect",
        "live_account_detected",
    }


def test_the_momentary_looking_events_still_have_a_recovery() -> None:
    """``mt5_reconnect`` is phrased as "currently reconnecting", not "a reconnect happened".

    A moment with no recovery would tell an operator a reconnect started and never that it
    worked, which is the half of the story that matters at 03:00.
    """
    assert "back" in BY_NAME["mt5_reconnect"].recovery
    assert "practice" in BY_NAME["live_account_detected"].recovery


# ── Scoping ───────────────────────────────────────────────────────────────────


def test_one_symbols_feed_going_dark_does_not_clear_the_others(register) -> None:
    """A single event for both symbols would clear the moment either recovered — reporting
    healthy while one was still dark."""
    ops = register()
    ops.observe("symbol_feed_stale", active=True, scope="XAUUSD")
    ops.observe("symbol_feed_stale", active=True, scope="XAGUSD")
    assert len(ops.post.messages) == 2

    ops.observe("symbol_feed_stale", active=False, scope="XAUUSD")
    assert len(ops.post.messages) == 3

    # Silver is still dark: observing it again changes nothing.
    assert ops.observe("symbol_feed_stale", active=True, scope="XAGUSD").edge is None


def test_a_scoped_event_without_a_scope_is_refused(register) -> None:
    with pytest.raises(ValueError, match="per-scope"):
        register().observe("symbol_feed_stale", active=True)


def test_an_unscoped_event_with_a_scope_is_refused(register) -> None:
    with pytest.raises(ValueError, match="not a scoped event"):
        register().observe("observer_stale", active=True, scope=SYMBOL)


def test_an_unknown_event_name_is_refused(register) -> None:
    """A register accepting any string would grow a second vocabulary of near-duplicates
    ("observer_stale", "observer_is_stale") that no runbook could document."""
    with pytest.raises(UnknownOpsEvent, match="not a known ops event"):
        register().observe("observer_is_a_bit_slow", active=True)


# ── Reporting must never stop a service ───────────────────────────────────────


def test_an_unwritable_register_does_not_raise(register, firestore) -> None:
    """An observer that died because it could not record that it was slow would be a worse
    outcome than the slowness."""
    ops = register()
    firestore.fail_next = 99
    result = ops.observe("archive_write_failed", active=True, detail="disk full")
    assert result.edge == "onset"
    assert len(ops.post.messages) == 1, "the message still went out"


def test_a_failing_post_does_not_raise(register) -> None:
    ops = register()

    def explode(_message: str) -> None:
        raise RuntimeError("403 forbidden")

    ops.post = explode
    assert ops.observe("mt5_reconnect", active=True).edge == "onset"


# ── /ops rendering ────────────────────────────────────────────────────────────


def test_the_readout_shows_active_first_with_an_age(register, firestore) -> None:
    ops = register()
    clock = ops._now
    ops.observe("outbox_backlog", active=True, detail="312 undelivered")
    ops.observe("mt5_reconnect", active=True)
    ops.observe("mt5_reconnect", active=False)
    clock.advance(minutes=45)

    lines = render_ops(OpsEventRepository(firestore).all_events(), now=clock.now)
    assert lines[0].startswith("🔴")
    assert "outbox_backlog" in lines[0]
    assert "for 45m" in lines[0]
    assert "312 undelivered" in lines[0]
    assert any(line.startswith("🟢") and "mt5_reconnect" in line for line in lines)


def test_a_cleared_condition_says_how_often_it_fired(register, firestore) -> None:
    """"This flapped twice this morning and cleared" and "this has never happened" are
    different things to know, and both read as "fine" without the count."""
    ops = register()
    for _ in range(2):
        ops.observe("outbox_backlog", active=True)
        ops.observe("outbox_backlog", active=False)

    lines = render_ops(OpsEventRepository(firestore).all_events(), now=NOW)
    assert "2× since deploy" in lines[0]


def test_an_empty_register_says_so_rather_than_rendering_nothing(firestore) -> None:
    """A blank readout is indistinguishable from a broken command."""
    lines = render_ops(OpsEventRepository(firestore).all_events())
    assert len(lines) == 1
    assert "No operational condition" in lines[0]
