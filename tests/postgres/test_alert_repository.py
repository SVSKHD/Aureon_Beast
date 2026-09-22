"""Price alerts on a real server (9C, S-3b).

An alert must be answered **once**, and two processes can legitimately try: the observer
fires, Discord cancels. So the tests that matter here are the overlapped ones -- the same
discipline decision 352 forced on the setup transaction, applied to a message rather than
to money.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aureon.models.enums import PriceAlertStatus, TransitionError
from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.alerts import (
    AlertClaimRejected,
    AlertRejected,
    PriceAlertRepository,
)
from tests.postgres.contention import interleave
from tests.postgres.factories import CLOSE, an_alert

pytestmark = pytest.mark.postgres


@pytest.fixture
def repo(schema: Database) -> PriceAlertRepository:
    return PriceAlertRepository(schema)


# ── Arming ────────────────────────────────────────────────────────────────────


def test_an_alert_survives_the_database_unchanged(repo: PriceAlertRepository) -> None:
    armed = repo.arm(an_alert(), now=CLOSE)

    assert repo.get("a1") == armed


def test_arming_sets_the_status_and_the_clock(repo: PriceAlertRepository) -> None:
    armed = repo.arm(an_alert(), ttl_hours=6.0, now=CLOSE)

    assert armed.status is PriceAlertStatus.ARMED
    assert armed.created_at == CLOSE
    assert armed.expires_at == CLOSE + timedelta(hours=6)


def test_an_explicit_expiry_is_not_overwritten(repo: PriceAlertRepository) -> None:
    wanted = CLOSE + timedelta(days=2)
    armed = repo.arm(an_alert(expires_at=wanted), ttl_hours=6.0, now=CLOSE)

    assert armed.expires_at == wanted


def test_a_user_over_the_cap_is_refused(repo: PriceAlertRepository) -> None:
    """Not a resource limit: twenty levels is more than anyone watches, and the hundredth
    armed alert makes the channel useless for the ones that matter."""
    repo.arm(an_alert("a1"), max_per_user=2, now=CLOSE)
    repo.arm(an_alert("a2"), max_per_user=2, now=CLOSE)

    with pytest.raises(AlertRejected):
        repo.arm(an_alert("a3"), max_per_user=2, now=CLOSE)


def test_the_cap_counts_only_armed_alerts(repo: PriceAlertRepository) -> None:
    repo.arm(an_alert("a1"), max_per_user=1, now=CLOSE)
    repo.cancel("a1", actor="trader")

    repo.arm(an_alert("a2"), max_per_user=1, now=CLOSE)  # must not raise


def test_the_cap_is_per_user(repo: PriceAlertRepository) -> None:
    repo.arm(an_alert("a1"), max_per_user=1, now=CLOSE)

    repo.arm(an_alert("a2", requested_by="someone_else"), max_per_user=1, now=CLOSE)


def test_two_racing_arms_cannot_both_pass_the_cap(schema: Database) -> None:
    """The Firestore version could not do this: there the count was a read followed by an
    unrelated write, so a user issuing two ``/remind`` commands at once passed the check
    twice. Here the COUNT and the INSERT share one transaction under one lock.
    """
    PriceAlertRepository(schema).arm(an_alert("a0"), max_per_user=2, now=CLOSE)

    outcome = interleave(
        lambda signal: _Signalling(schema, signal).arm(
            an_alert("a1"), max_per_user=2, now=CLOSE
        ),
        lambda: PriceAlertRepository(schema).arm(
            an_alert("a2"), max_per_user=2, now=CLOSE
        ),
    )

    assert [type(e).__name__ for e in outcome.errors.values()] == ["AlertRejected"], outcome
    assert len(PriceAlertRepository(schema).armed()) == 2


class _Signalling(PriceAlertRepository):
    """Announces when it holds its lock, still inside its transaction."""

    def __init__(self, database: Database, signal) -> None:  # type: ignore[no-untyped-def]
        super().__init__(database)
        self._signal = signal

    def _locked(self, alert_id: str, connection):  # type: ignore[no-untyped-def]
        current = super()._locked(alert_id, connection)
        self._signal()
        return current

    def _armed_for_user_locked(self, user_id: str, connection):  # type: ignore[no-untyped-def]
        rows = super()._armed_for_user_locked(user_id, connection)
        self._signal()
        return rows


# ── Reading ───────────────────────────────────────────────────────────────────


def test_armed_narrows_by_symbol(repo: PriceAlertRepository) -> None:
    repo.arm(an_alert("a1", symbol="XAUUSD"), now=CLOSE)
    repo.arm(an_alert("a2", symbol="XAGUSD"), now=CLOSE)

    assert [a.alert_id for a in repo.armed(symbol="XAGUSD")] == ["a2"]


def test_armed_narrows_by_user(repo: PriceAlertRepository) -> None:
    repo.arm(an_alert("a1"), now=CLOSE)
    repo.arm(an_alert("a2", requested_by="other"), now=CLOSE)

    assert [a.alert_id for a in repo.armed(user_id="other")] == ["a2"]


def test_armed_excludes_everything_that_is_not_armed(repo: PriceAlertRepository) -> None:
    repo.arm(an_alert("a1"), now=CLOSE)
    repo.arm(an_alert("a2"), now=CLOSE)
    repo.cancel("a2", actor="trader")

    assert [a.alert_id for a in repo.armed()] == ["a1"]


def test_for_user_shows_answered_alerts_too(repo: PriceAlertRepository) -> None:
    """What ``/remind list`` shows: a cancelled alert is still something the user set."""
    repo.arm(an_alert("a1"), now=CLOSE)
    repo.arm(an_alert("a2"), now=CLOSE)
    repo.cancel("a2", actor="trader")

    assert sorted(a.alert_id for a in repo.for_user("trader")) == ["a1", "a2"]


def test_fired_since_excludes_earlier_and_unanswered_alerts(
    repo: PriceAlertRepository,
) -> None:
    repo.arm(an_alert("old"), now=CLOSE)
    repo.arm(an_alert("new"), now=CLOSE)
    repo.arm(an_alert("still_armed"), now=CLOSE)
    repo.fire("old", price=2410.0, snapshot={}, now=CLOSE)
    later = CLOSE + timedelta(minutes=10)
    repo.fire("new", price=2411.0, snapshot={}, now=later)

    assert [a.alert_id for a in repo.fired_since(later)] == ["new"]


def test_an_unknown_alert_reads_as_none(repo: PriceAlertRepository) -> None:
    assert repo.get("nobody") is None


# ── Firing ────────────────────────────────────────────────────────────────────


def test_firing_freezes_the_snapshot_the_observer_had(repo: PriceAlertRepository) -> None:
    """Frozen by the process that has the indicators. Discord renders this copy and never
    recomputes it (9C)."""
    repo.arm(an_alert(), now=CLOSE)
    fired = repo.fire("a1", price=2410.5, snapshot={"rsi": 71.2}, now=CLOSE)

    assert fired.status is PriceAlertStatus.FIRED
    assert fired.fired_price == 2410.5
    assert fired.fired_snapshot == {"rsi": 71.2}
    stored = repo.get("a1")
    assert stored is not None and stored.fired_snapshot == {"rsi": 71.2}


def test_firing_a_cancelled_alert_is_refused(repo: PriceAlertRepository) -> None:
    repo.arm(an_alert(), now=CLOSE)
    repo.cancel("a1", actor="trader")

    with pytest.raises(AlertClaimRejected):
        repo.fire("a1", price=2410.0, snapshot={}, now=CLOSE)


def test_firing_an_absent_alert_is_refused(repo: PriceAlertRepository) -> None:
    with pytest.raises(AlertClaimRejected):
        repo.fire("nobody", price=2410.0, snapshot={}, now=CLOSE)


def test_two_observers_firing_one_alert_send_one_message(schema: Database) -> None:
    """Decision 352's overlap, on the path that decides whether a trader is told once or
    twice. Without the lock both read ARMED, both freeze a snapshot and both post.
    """
    PriceAlertRepository(schema).arm(an_alert(), now=CLOSE)

    outcome = interleave(
        lambda signal: _Signalling(schema, signal).fire(
            "a1", price=2410.0, snapshot={"who": "first"}, now=CLOSE
        ),
        lambda: PriceAlertRepository(schema).fire(
            "a1", price=2410.9, snapshot={"who": "second"}, now=CLOSE
        ),
    )

    assert len(outcome.results) == 1, outcome
    assert [type(e).__name__ for e in outcome.errors.values()] == ["AlertClaimRejected"]


def test_a_cancel_racing_a_fire_leaves_one_answer(schema: Database) -> None:
    """The two writers are different processes doing different things, which is exactly
    the case a status check alone would get wrong."""
    PriceAlertRepository(schema).arm(an_alert(), now=CLOSE)

    outcome = interleave(
        lambda signal: _Signalling(schema, signal).fire(
            "a1", price=2410.0, snapshot={}, now=CLOSE
        ),
        lambda: PriceAlertRepository(schema).cancel("a1", actor="trader"),
    )

    assert len(outcome.results) == 1, outcome
    stored = PriceAlertRepository(schema).get("a1")
    assert stored is not None
    assert stored.status in (PriceAlertStatus.FIRED, PriceAlertStatus.CANCELLED)


# ── Cancelling ────────────────────────────────────────────────────────────────


def test_only_the_user_who_armed_it_may_cancel(repo: PriceAlertRepository) -> None:
    repo.arm(an_alert(), now=CLOSE)

    with pytest.raises(AlertRejected):
        repo.cancel("a1", actor="somebody_else")

    stored = repo.get("a1")
    assert stored is not None and stored.status is PriceAlertStatus.ARMED


def test_cancelling_twice_is_refused(repo: PriceAlertRepository) -> None:
    repo.arm(an_alert(), now=CLOSE)
    repo.cancel("a1", actor="trader")

    with pytest.raises(AlertClaimRejected):
        repo.cancel("a1", actor="trader")


def test_a_cancel_records_who_did_it(repo: PriceAlertRepository) -> None:
    repo.arm(an_alert(), now=CLOSE)
    cancelled = repo.cancel("a1", actor="trader")

    assert cancelled.cancelled_by == "trader"
    assert cancelled.fired_at is None


# ── Expiring ──────────────────────────────────────────────────────────────────


def test_expire_due_answers_only_alerts_past_their_time(repo: PriceAlertRepository) -> None:
    repo.arm(an_alert("soon"), ttl_hours=1.0, now=CLOSE)
    repo.arm(an_alert("later"), ttl_hours=48.0, now=CLOSE)

    assert repo.expire_due(now=CLOSE + timedelta(hours=2)) == ["soon"]
    stored = repo.get("later")
    assert stored is not None and stored.status is PriceAlertStatus.ARMED


def test_an_expired_alert_is_marked_rather_than_deleted(repo: PriceAlertRepository) -> None:
    repo.arm(an_alert(), ttl_hours=1.0, now=CLOSE)
    repo.expire_due(now=CLOSE + timedelta(hours=2))

    stored = repo.get("a1")
    assert stored is not None and stored.status is PriceAlertStatus.EXPIRED


def test_expiring_skips_an_alert_answered_in_the_meantime(
    repo: PriceAlertRepository,
) -> None:
    """Fired or cancelled between the scan and the write. Nothing to do, and certainly
    nothing to overwrite."""
    repo.arm(an_alert(), ttl_hours=1.0, now=CLOSE)
    repo.fire("a1", price=2410.0, snapshot={}, now=CLOSE)

    assert repo.expire_due(now=CLOSE + timedelta(hours=2)) == []
    stored = repo.get("a1")
    assert stored is not None and stored.status is PriceAlertStatus.FIRED


def test_every_answer_goes_through_the_transition_gate(repo: PriceAlertRepository) -> None:
    """CLAUDE.md: no status field is written without ``assert_transition``. A fired alert
    cannot be re-armed, and the gate is what says so rather than an ``if`` somewhere."""
    from aureon.models.enums import assert_price_alert_transition

    with pytest.raises(TransitionError):
        assert_price_alert_transition(PriceAlertStatus.FIRED, PriceAlertStatus.ARMED)
