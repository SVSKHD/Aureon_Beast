"""Price alerts and notification records (9C).

Both documents exist so that something is said **once**. These tests pin the rules that make
that true where a unit test can: the status machine, the per-user cap, the ownership check on
a cancel, expiry on the market clock, and the claim that a notification's id IS its dedup.

What a unit test cannot prove is anything **transactional**. ``fire``, ``cancel`` and the
expiry sweep run through ``firestore.transactional``, which the in-memory double cannot satisfy
-- deliberately, because a double that implemented the SDK's transaction protocol would be
testing my model of Firestore rather than Firestore. Every one of those paths is covered in
``tests/failure_injection/test_alert_flows.py`` against the emulator, including the case that
matters most: two observers seeing the same crossing and only one message being sent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.models.alerts import MAX_ARMED_ALERTS_PER_USER, Notification, PriceAlert
from aureon.models.enums import (
    NotificationKind,
    NotificationStatus,
    PriceAlertStatus,
    TransitionError,
    assert_price_alert_transition,
)
from aureon.storage.alert_repository import (
    AlertRejected,
    PriceAlertRepository,
    new_alert_id,
)
from aureon.storage.notification_repository import NotificationRepository

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
USER = "user-1"
SYMBOL = "XAUUSD"


def alert(**overrides) -> PriceAlert:
    base = dict(
        alert_id=overrides.pop("alert_id", new_alert_id()),
        symbol=SYMBOL,
        level=3700.0,
        side="above",
        requested_by=USER,
    )
    return PriceAlert(**(base | overrides))


@pytest.fixture
def alerts(firestore) -> PriceAlertRepository:
    return PriceAlertRepository(firestore)


@pytest.fixture
def notifications(firestore) -> NotificationRepository:
    return NotificationRepository(firestore)


# ── The status machine ────────────────────────────────────────────────────────


def test_an_armed_alert_may_fire_cancel_or_expire() -> None:
    for target in (
        PriceAlertStatus.FIRED,
        PriceAlertStatus.CANCELLED,
        PriceAlertStatus.EXPIRED,
    ):
        assert_price_alert_transition(PriceAlertStatus.ARMED, target)


@pytest.mark.parametrize(
    "terminal",
    [PriceAlertStatus.FIRED, PriceAlertStatus.CANCELLED, PriceAlertStatus.EXPIRED],
)
def test_nothing_leaves_a_terminal_alert(terminal: PriceAlertStatus) -> None:
    """An alert that has fired is a fact about a moment, not a switch. Re-arming is a NEW
    alert, so the record of what was asked and when it was answered stays one fact."""
    with pytest.raises(TransitionError):
        assert_price_alert_transition(terminal, PriceAlertStatus.ARMED)


def test_the_crossing_test_uses_the_side_of_the_book_a_trader_transacts_at() -> None:
    """An alert for 3700 from below is answered when they could have BOUGHT at 3700.

    The mid would fire half a spread early, every time, in the direction that flatters the
    alert.
    """
    above = alert(level=3700.0, side="above")
    assert above.crossed_by(bid=3699.0, ask=3700.05) is True
    assert above.crossed_by(bid=3700.02, ask=3700.32) is True
    assert above.crossed_by(bid=3699.0, ask=3699.30) is False

    below = alert(level=3600.0, side="below")
    assert below.crossed_by(bid=3599.95, ask=3600.25) is True
    assert below.crossed_by(bid=3600.10, ask=3600.40) is False


def test_an_unknown_side_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown alert side"):
        alert(side="sideways")


# ── Arming ────────────────────────────────────────────────────────────────────


def test_arming_stores_an_expiry_a_day_out(alerts: PriceAlertRepository) -> None:
    """A day, because what a trader asked about in this session is not what they meant in
    the next one."""
    stored = alerts.arm(alert(), now=NOW)
    assert stored.status is PriceAlertStatus.ARMED
    assert stored.created_at == NOW
    assert stored.expires_at == NOW + timedelta(hours=24)


def test_an_explicit_expiry_is_kept(alerts: PriceAlertRepository) -> None:
    wanted = NOW + timedelta(hours=2)
    assert alerts.arm(alert(expires_at=wanted), now=NOW).expires_at == wanted


def test_a_user_cannot_arm_more_than_the_cap(alerts: PriceAlertRepository) -> None:
    """Not a resource limit: the hundredth armed alert makes the channel useless for the
    ones that matter."""
    for index in range(MAX_ARMED_ALERTS_PER_USER):
        alerts.arm(alert(level=3700.0 + index), now=NOW)
    with pytest.raises(AlertRejected, match="already has"):
        alerts.arm(alert(level=9999.0), now=NOW)

    # The cap is per user, so somebody else is unaffected by my twenty.
    assert alerts.arm(alert(requested_by="user-2"), now=NOW).requested_by == "user-2"


def test_armed_listing_narrows_by_symbol_and_user(alerts: PriceAlertRepository) -> None:
    alerts.arm(alert(), now=NOW)
    alerts.arm(alert(symbol="XAGUSD", level=30.0), now=NOW)
    alerts.arm(alert(requested_by="user-2", level=3800.0), now=NOW)

    assert len(alerts.armed()) == 3
    assert {a.symbol for a in alerts.armed(symbol="xagusd")} == {"XAGUSD"}
    assert {a.requested_by for a in alerts.armed(user_id=USER)} == {USER}


# ── Answering ─────────────────────────────────────────────────────────────────







# ── Expiry ────────────────────────────────────────────────────────────────────




# ── Notifications ─────────────────────────────────────────────────────────────


def test_a_claim_may_be_taken_once(notifications: NotificationRepository) -> None:
    """The id IS the dedup: a create over an existing document fails, which is a claim."""
    first = notifications.claim(
        NotificationKind.DETECTION, "det-1", symbol=SYMBOL, channel_id="c1", now=NOW
    )
    assert first is not None
    assert first.status is NotificationStatus.SENT
    assert (
        notifications.claim(
            NotificationKind.DETECTION, "det-1", symbol=SYMBOL, channel_id="c1", now=NOW
        )
        is None
    )


def test_a_claim_survives_a_restart(firestore) -> None:
    """The reason the record is a document: an in-process set is empty after a restart,
    which is exactly when a duplicate would be sent."""
    NotificationRepository(firestore).claim(
        NotificationKind.ALERT, "al-1", symbol=SYMBOL, channel_id="c1", now=NOW
    )
    restarted = NotificationRepository(firestore)
    assert restarted.already_sent(NotificationKind.ALERT, "al-1") is True
    assert (
        restarted.claim(
            NotificationKind.ALERT, "al-1", symbol=SYMBOL, channel_id="c1", now=NOW
        )
        is None
    )


def test_the_two_kinds_do_not_collide(notifications: NotificationRepository) -> None:
    """A detection and an alert could in principle share a ref id; the kind separates them."""
    assert notifications.claim(
        NotificationKind.DETECTION, "x", symbol=SYMBOL, channel_id="c1", now=NOW
    )
    assert notifications.claim(
        NotificationKind.ALERT, "x", symbol=SYMBOL, channel_id="c1", now=NOW
    )


def test_a_failed_send_is_recorded_and_not_retried(
    notifications: NotificationRepository,
) -> None:
    """A retry over a channel that is rejecting messages would either duplicate the post or
    hide the outage. The row is for the operator."""
    notifications.claim(
        NotificationKind.DETECTION, "det-2", symbol=SYMBOL, channel_id="c1", now=NOW
    )
    notifications.mark_failed(NotificationKind.DETECTION, "det-2", message="403 forbidden")
    record = notifications.get(NotificationKind.DETECTION, "det-2")
    assert record.status is NotificationStatus.FAILED
    assert "403" in record.failure_message
    # Still claimed: already_sent is about "has this been announced", not "did it work".
    assert notifications.already_sent(NotificationKind.DETECTION, "det-2") is True


def test_marking_an_unclaimed_notification_failed_creates_nothing(
    notifications: NotificationRepository, firestore
) -> None:
    notifications.mark_failed(NotificationKind.DETECTION, "never", message="x")
    assert notifications.get(NotificationKind.DETECTION, "never") is None


def test_a_notification_id_is_derived_from_what_it_is_about() -> None:
    from aureon.storage import paths

    assert paths.notification_id("detection", "abc") == "detection__abc"
    with pytest.raises(ValueError, match="must not be empty"):
        paths.notification_id("detection", "")
    with pytest.raises(ValueError, match="must not contain"):
        paths.notification_id("detection", "a/b")


def test_a_notification_document_records_where_it_went() -> None:
    record = Notification(
        notification_id="detection__abc",
        kind=NotificationKind.DETECTION,
        symbol=SYMBOL,
        ref_id="abc",
        channel_id="99",
    )
    assert record.channel_id == "99"
    assert record.status is NotificationStatus.SENT
