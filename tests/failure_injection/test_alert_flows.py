"""Price alerts against the emulator: the transactional half (9C).

``tests/unit/test_alerts_storage.py`` pins the status machine, the per-user cap and the
notification id. What only the emulator can show is the property the transactions exist for:
an alert is answered **once**, even when two observers see the same crossing, and a cancel
that lands a moment earlier wins.

That is the same discipline the executor's claim uses for money, applied to a message — and it
matters for the same reason. A duplicated price alert tells a human twice that a level they are
watching has been crossed, which is a reason to act, twice.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aureon.models.alerts import PriceAlert
from aureon.models.base import utc_now
from aureon.models.enums import NotificationKind, PriceAlertStatus
from aureon.storage.alert_repository import (
    AlertClaimRejected,
    AlertRejected,
    PriceAlertRepository,
    new_alert_id,
)
from aureon.storage.notification_repository import NotificationRepository
from tests.failure_injection.conftest import SYMBOL, USER

pytestmark = pytest.mark.emulator


@pytest.fixture
def alerts(firestore_client) -> PriceAlertRepository:
    return PriceAlertRepository(firestore_client)


@pytest.fixture
def notifications(firestore_client) -> NotificationRepository:
    return NotificationRepository(firestore_client)


def alert(**overrides) -> PriceAlert:
    base = dict(
        alert_id=overrides.pop("alert_id", new_alert_id()),
        symbol=SYMBOL,
        level=3700.0,
        side="above",
        requested_by=USER,
    )
    return PriceAlert(**(base | overrides))


# ── Exactly once ──────────────────────────────────────────────────────────────


def test_an_alert_fires_once_even_when_two_observers_see_the_crossing(
    alerts: PriceAlertRepository,
) -> None:
    """The case the transaction exists for.

    Both callers read an ARMED alert and both try to answer it. Exactly one commits; the
    other's transaction aborts because its read set changed, and it raises rather than
    freezing a second snapshot and telling Discord again.
    """
    stored = alerts.arm(alert())
    first = alerts.fire(stored.alert_id, price=3700.10, snapshot={"by": "observer-a"})
    with pytest.raises(AlertClaimRejected, match="not armed"):
        alerts.fire(stored.alert_id, price=3700.20, snapshot={"by": "observer-b"})

    final = alerts.get(stored.alert_id)
    assert final.status is PriceAlertStatus.FIRED
    assert final.fired_price == pytest.approx(3700.10)
    assert final.fired_snapshot == {"by": "observer-a"}
    assert first.fired_at == final.fired_at


def test_the_snapshot_survives_the_round_trip_intact(alerts: PriceAlertRepository) -> None:
    """Discord renders this copy, so every field has to come back as it went in."""
    snapshot = {
        "ema_fast": 3699.5,
        "ema_slow": 3690.25,
        "ema_relation": "above",
        "rsi": 61.4,
        "rsi_zone": "neutral",
        "session": "london",
        "session_trend": "up",
        "minutes_since_cross": 42,
        "volume_profile": {"scope": "asia", "poc_price": 3680.0},
        "volatility": {"regime": "high", "atr_14": 1.25},
        "last_wick": {"classification": "lower_rejection"},
    }
    stored = alerts.arm(alert())
    alerts.fire(stored.alert_id, price=3700.10, snapshot=snapshot)
    assert alerts.get(stored.alert_id).fired_snapshot == snapshot


def test_a_cancel_that_lands_first_prevents_the_firing(
    alerts: PriceAlertRepository,
) -> None:
    stored = alerts.arm(alert())
    alerts.cancel(stored.alert_id, actor=USER)
    with pytest.raises(AlertClaimRejected):
        alerts.fire(stored.alert_id, price=3700.10, snapshot={})
    assert alerts.get(stored.alert_id).status is PriceAlertStatus.CANCELLED


def test_only_the_requester_may_cancel(alerts: PriceAlertRepository) -> None:
    """§71, enforced in the repository so it holds however the cancel arrives."""
    stored = alerts.arm(alert())
    with pytest.raises(AlertRejected, match="did not arm"):
        alerts.cancel(stored.alert_id, actor="someone-else")
    assert alerts.get(stored.alert_id).status is PriceAlertStatus.ARMED


def test_a_second_cancel_is_refused_rather_than_repeated(
    alerts: PriceAlertRepository,
) -> None:
    stored = alerts.arm(alert())
    alerts.cancel(stored.alert_id, actor=USER)
    with pytest.raises(AlertClaimRejected, match="already"):
        alerts.cancel(stored.alert_id, actor=USER)


# ── Expiry ────────────────────────────────────────────────────────────────────


def test_expiry_answers_only_the_due_ones(alerts: PriceAlertRepository) -> None:
    now = utc_now()
    due = alerts.arm(alert(expires_at=now - timedelta(minutes=1)), now=now)
    live = alerts.arm(alert(level=3800.0, expires_at=now + timedelta(hours=3)), now=now)

    assert alerts.expire_due(now=now) == [due.alert_id]
    assert alerts.get(due.alert_id).status is PriceAlertStatus.EXPIRED
    assert alerts.get(live.alert_id).status is PriceAlertStatus.ARMED


def test_an_expired_alert_never_fires(alerts: PriceAlertRepository) -> None:
    """The phase's own test: expired never fires."""
    now = utc_now()
    stored = alerts.arm(alert(expires_at=now - timedelta(seconds=1)), now=now)
    alerts.expire_due(now=now)
    with pytest.raises(AlertClaimRejected):
        alerts.fire(stored.alert_id, price=3700.10, snapshot={})


def test_expiry_leaves_a_fired_alert_alone(alerts: PriceAlertRepository) -> None:
    """A FIRED alert is past its expiry too, and must not be rewritten as EXPIRED."""
    now = utc_now()
    stored = alerts.arm(alert(expires_at=now - timedelta(seconds=1)), now=now)
    alerts.fire(stored.alert_id, price=3700.10, snapshot={"kept": True})
    assert alerts.expire_due(now=now) == []
    final = alerts.get(stored.alert_id)
    assert final.status is PriceAlertStatus.FIRED
    assert final.fired_snapshot == {"kept": True}


# ── Notifications ─────────────────────────────────────────────────────────────


def test_a_notification_claim_is_exclusive_in_firestore(
    notifications: NotificationRepository,
) -> None:
    """``create`` refuses an existing document, which is what makes the claim a claim.

    The unit tests use a double whose ``create`` raises; this is the same assertion against
    the client that defines the behaviour.
    """
    assert notifications.claim(
        NotificationKind.DETECTION, "det-1", symbol=SYMBOL, channel_id="c1"
    )
    assert (
        notifications.claim(
            NotificationKind.DETECTION, "det-1", symbol=SYMBOL, channel_id="c1"
        )
        is None
    )
    assert notifications.already_sent(NotificationKind.DETECTION, "det-1") is True


def test_a_claim_taken_by_one_process_is_visible_to_another(firestore_client) -> None:
    """Two bots, one channel: the second must find the first's record, not post again."""
    NotificationRepository(firestore_client).claim(
        NotificationKind.ALERT, "al-1", symbol=SYMBOL, channel_id="c1"
    )
    other = NotificationRepository(firestore_client)
    assert other.claim(NotificationKind.ALERT, "al-1", symbol=SYMBOL, channel_id="c1") is None
