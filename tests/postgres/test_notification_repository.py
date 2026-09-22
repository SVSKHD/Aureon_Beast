"""Notifications, and the claim that stops a card being posted twice (9C, 12 T-11).

The failure this prevents is visible rather than expensive: the same setup card appearing
twice in a trading channel, or a fired reminder announced on every sweep until somebody
notices. But it is the same shape as the executor's claim, and it is worth proving with the
same rigour -- two notifier sweeps a second apart is the normal case, not an edge one.
"""

from __future__ import annotations

import threading

import pytest

from aureon.models.enums import NotificationKind, NotificationStatus
from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.notifications import (
    NotificationRepository,
    notification_id,
)
from tests.postgres.factories import CLOSE

pytestmark = pytest.mark.postgres


@pytest.fixture
def repo(schema: Database) -> NotificationRepository:
    return NotificationRepository(schema)


def _claim(repo: NotificationRepository, ref_id: str = "s1"):
    return repo.claim(
        NotificationKind.SETUP, ref_id, symbol="XAUUSD", channel_id="123", now=CLOSE
    )


# ── The claim ─────────────────────────────────────────────────────────────────


def test_a_claim_succeeds_once(repo: NotificationRepository) -> None:
    claimed = _claim(repo)
    assert claimed is not None
    assert claimed.notification_id == "setup__s1"
    assert claimed.sent_at == CLOSE
    assert claimed.status is NotificationStatus.SENT


def test_a_second_claim_returns_none(repo: NotificationRepository) -> None:
    """``None`` rather than an exception: "somebody else is posting this" is the normal
    outcome of a sweep, not an error the notifier should have to catch."""
    assert _claim(repo) is not None
    assert _claim(repo) is None


def test_a_failed_claim_does_not_break_the_row(repo: NotificationRepository) -> None:
    """The SAVEPOINT's purpose.

    PostgreSQL aborts a whole transaction on a constraint violation unless the failure
    happens inside a nested block -- so without the savepoint, the collision would propagate
    as a broken transaction instead of returning ``None``.

    Asserted by the LOST claim returning ``None`` rather than raising, and by the row still
    being readable afterwards. Both matter: a version that let the IntegrityError escape
    would fail the first assertion, and one that rolled back the whole transaction would
    fail the second.
    """
    first = _claim(repo)
    assert _claim(repo) is None, "the collision escaped instead of being absorbed"
    assert repo.get(NotificationKind.SETUP, "s1") == first
    # And the repository is still usable afterwards, on the same database.
    assert _claim(repo, "s2") is not None


def test_different_refs_do_not_collide(repo: NotificationRepository) -> None:
    assert _claim(repo, "s1") is not None
    assert _claim(repo, "s2") is not None


def test_different_kinds_of_the_same_ref_do_not_collide(
    repo: NotificationRepository,
) -> None:
    """A detection and a setup can share an id space; the kind is part of the key."""
    assert _claim(repo, "x") is not None
    assert repo.claim(
        NotificationKind.DETECTION, "x", symbol="XAUUSD", channel_id="123", now=CLOSE
    ) is not None


def test_two_sweeps_claiming_at_once_produce_one_post(schema: Database) -> None:
    """The race the id exists to settle, proved rather than reasoned about.

    Two notifier sweeps a second apart is the normal case. If both could claim, the same
    setup card would be posted twice into a trading channel -- and a human reading two
    cards for one setup has to work out which is current.
    """
    won: list[object] = []
    lost: list[object] = []

    def run() -> None:
        own = NotificationRepository(schema)
        result = own.claim(
            NotificationKind.SETUP, "s1", symbol="XAUUSD", channel_id="123", now=CLOSE
        )
        (won if result is not None else lost).append(result)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(won) == 1, f"won={len(won)} lost={len(lost)}"
    assert len(lost) == 3


# ── The id ────────────────────────────────────────────────────────────────────


def test_the_id_shape_is_the_one_9c_used(repo: NotificationRepository) -> None:
    """Kept byte-identical so a Firestore export lands on the same primary key (C-12).

    If the shape changed, the export would create a second row for every notification and
    every card already posted would be posted again.
    """
    assert notification_id("setup", "s1") == "setup__s1"
    with pytest.raises(ValueError):
        notification_id("", "s1")
    with pytest.raises(ValueError):
        notification_id("setup", "")


# ── After the send ────────────────────────────────────────────────────────────


def test_the_message_id_can_be_recorded(repo: NotificationRepository) -> None:
    """12 T-11: so the card can be EDITED as the setup advances rather than reposted."""
    _claim(repo)
    updated = repo.record_message(NotificationKind.SETUP, "s1", "999888777")
    assert updated is not None and updated.message_id == "999888777"
    assert repo.get(NotificationKind.SETUP, "s1").message_id == "999888777"  # type: ignore[union-attr]


def test_recording_a_message_on_an_unclaimed_notification_is_none(
    repo: NotificationRepository,
) -> None:
    assert repo.record_message(NotificationKind.SETUP, "never", "1") is None


def test_a_failed_send_is_recorded_and_the_row_stays(repo: NotificationRepository) -> None:
    """Deleting it would make the next sweep try again -- and a send that fails because the
    message is malformed would then be retried for ever against a channel that keeps
    refusing it."""
    _claim(repo)
    failed = repo.mark_failed(NotificationKind.SETUP, "s1", "403 Forbidden")
    assert failed is not None
    assert failed.status is NotificationStatus.FAILED
    assert failed.failure_message == "403 Forbidden"
    assert repo.already_sent(NotificationKind.SETUP, "s1") is True


def test_a_failed_notification_cannot_be_re_claimed(repo: NotificationRepository) -> None:
    """The consequence of keeping the row, stated as a test so nobody 'fixes' it later."""
    _claim(repo)
    repo.mark_failed(NotificationKind.SETUP, "s1", "403 Forbidden")
    assert _claim(repo) is None


# ── Reading ───────────────────────────────────────────────────────────────────


def test_already_sent_is_an_optimisation_not_the_gate(repo: NotificationRepository) -> None:
    """Documented as such, and asserted so the distinction survives.

    The decision belongs to ``claim``, which is atomic. This read may be stale by the time
    the caller acts on it, and treating it as the gate would put the race straight back.
    """
    assert repo.already_sent(NotificationKind.SETUP, "s1") is False
    _claim(repo)
    assert repo.already_sent(NotificationKind.SETUP, "s1") is True


def test_recent_is_newest_first_and_bounded(repo: NotificationRepository) -> None:
    from datetime import timedelta

    for index in range(4):
        repo.claim(
            NotificationKind.SETUP,
            f"s{index}",
            symbol="XAUUSD",
            channel_id="123",
            now=CLOSE + timedelta(minutes=index),
        )
    recent = repo.recent(limit=2)
    assert [n.ref_id for n in recent] == ["s3", "s2"]
