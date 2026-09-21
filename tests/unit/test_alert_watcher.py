"""Firing price alerts from the observer's quotes (9C).

Two things are being pinned. The **decision**: which quotes answer an alert, and which do
not. And the **snapshot**: that what gets frozen is the same set of fields ``/status``
renders, so a reminder and the panel cannot describe one moment differently.

The transactional exactly-once property is not here -- it belongs to the repository and is
tested against the emulator. What this file covers is the watcher's own judgement, including
the case a stub cannot fake away: a quote that does not reach the level answers nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.models.alerts import PriceAlert
from aureon.models.enums import PriceAlertStatus
from aureon.models.market import QuoteSnapshot
from aureon.services.alert_watcher import AlertWatcher, build_snapshot
from aureon.storage.alert_repository import AlertClaimRejected

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
SYMBOL = "XAUUSD"
USER = "user-1"


class FakeAlerts:
    """The repository's surface, with the transaction replaced by a status check.

    Deliberately not a mock: ``fire`` refuses anything not still ARMED, exactly as the real
    one does, because that refusal is what the watcher's control flow depends on.
    """

    def __init__(self, *alerts: PriceAlert) -> None:
        self.store = {a.alert_id: a for a in alerts}
        self.fired: list[tuple[str, float, dict]] = []
        self.expired_calls = 0
        self.raise_on_read = False

    def armed(self, *, symbol: str | None = None, user_id: str | None = None):
        if self.raise_on_read:
            raise ConnectionError("firestore unreachable")
        found = [a for a in self.store.values() if a.status is PriceAlertStatus.ARMED]
        if symbol is not None:
            found = [a for a in found if a.symbol == symbol]
        return found

    def fire(self, alert_id: str, *, price: float, snapshot: dict, now=None):
        current = self.store[alert_id]
        if current.status is not PriceAlertStatus.ARMED:
            raise AlertClaimRejected(f"alert {alert_id} is {current.status.value}")
        self.store[alert_id] = current.model_copy(
            update={
                "status": PriceAlertStatus.FIRED,
                "fired_price": price,
                "fired_snapshot": snapshot,
            }
        )
        self.fired.append((alert_id, price, snapshot))
        return self.store[alert_id]

    def expire_due(self, *, now=None):
        self.expired_calls += 1
        return []


def alert(**overrides) -> PriceAlert:
    base = dict(
        alert_id=overrides.pop("alert_id", "al-1"),
        symbol=SYMBOL,
        level=2450.0,
        side="above",
        requested_by=USER,
    )
    return PriceAlert(**(base | overrides))


def quote(bid: float, ask: float | None = None) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=SYMBOL,
        bid=bid,
        ask=ask if ask is not None else bid + 0.30,
        point=0.01,
        captured_at=NOW,
    )


# ── Which quotes answer an alert ──────────────────────────────────────────────


def test_the_first_quote_beyond_the_level_fires_it() -> None:
    alerts = FakeAlerts(alert())
    watcher = AlertWatcher(alerts)

    assert watcher.check(SYMBOL, quote(2449.00), now=NOW) == []
    assert watcher.check(SYMBOL, quote(2449.60), now=NOW) == []  # ask 2449.90, still under
    fired = watcher.check(SYMBOL, quote(2449.75), now=NOW)  # ask 2450.05
    assert len(fired) == 1
    assert fired[0].alert_id == "al-1"
    assert fired[0].price == pytest.approx(2450.05)


def test_it_fires_at_the_price_a_trader_would_have_transacted_at() -> None:
    """`above` is answered by the ask, `below` by the bid -- the side of the book they
    would have used, not the mid."""
    above = FakeAlerts(alert(side="above", level=2450.0))
    AlertWatcher(above).check(SYMBOL, quote(2449.80, 2450.10), now=NOW)
    assert above.fired[0][1] == pytest.approx(2450.10)

    below = FakeAlerts(alert(side="below", level=2400.0))
    AlertWatcher(below).check(SYMBOL, quote(2399.90, 2400.20), now=NOW)
    assert below.fired[0][1] == pytest.approx(2399.90)


def test_an_alert_fires_once_however_many_quotes_follow() -> None:
    """The repository refuses the second attempt; the watcher must not treat that as an
    error, and must not report a second firing."""
    alerts = FakeAlerts(alert())
    watcher = AlertWatcher(alerts)
    assert len(watcher.check(SYMBOL, quote(2450.00), now=NOW)) == 1
    for _ in range(5):
        assert watcher.check(SYMBOL, quote(2451.00), now=NOW) == []
    assert len(alerts.fired) == 1
    assert watcher.fired == 1


def test_another_symbols_quote_answers_nothing() -> None:
    alerts = FakeAlerts(alert(symbol="XAGUSD", level=30.0))
    assert AlertWatcher(alerts).check(SYMBOL, quote(2500.0), now=NOW) == []
    assert alerts.fired == []


def test_a_missing_quote_answers_nothing_rather_than_treating_none_as_a_price() -> None:
    alerts = FakeAlerts(alert())
    assert AlertWatcher(alerts).check(SYMBOL, None, now=NOW) == []
    assert alerts.fired == []


def test_a_cancelled_alert_is_skipped_without_an_error() -> None:
    cancelled = alert(status=PriceAlertStatus.CANCELLED)
    alerts = FakeAlerts(cancelled)
    assert AlertWatcher(alerts).check(SYMBOL, quote(2500.0), now=NOW) == []


def test_a_read_failure_does_not_stop_observation() -> None:
    """Alerting is a side errand: a Firestore outage must not take the observer down."""
    alerts = FakeAlerts(alert())
    alerts.raise_on_read = True
    assert AlertWatcher(alerts).check(SYMBOL, quote(2500.0), now=NOW) == []


def test_several_alerts_on_one_symbol_all_answer_from_one_quote() -> None:
    alerts = FakeAlerts(
        alert(alert_id="al-a", level=2450.0),
        alert(alert_id="al-b", level=2440.0),
        alert(alert_id="al-c", level=2600.0),
    )
    fired = AlertWatcher(alerts).check(SYMBOL, quote(2455.0), now=NOW)
    assert {f.alert_id for f in fired} == {"al-a", "al-b"}


def test_expiry_delegates_to_the_repository_and_counts() -> None:
    alerts = FakeAlerts(alert())
    watcher = AlertWatcher(alerts)
    watcher.expire(now=NOW)
    assert alerts.expired_calls == 1


# ── The frozen snapshot ───────────────────────────────────────────────────────


def state_fields(**overrides) -> dict[str, object]:
    base = {
        "ema_fast": 2451.0,
        "ema_slow": 2440.0,
        "ema_distance": 11.0,
        "rsi": 61.4,
        "rsi_zone": "neutral",
        "session": "london",
        "session_trend": "up",
        "session_high": 2455.0,
        "session_low": 2438.0,
        "last_cross": {"direction": "buy", "at": (NOW - timedelta(minutes=42)).isoformat()},
        "last_cross_at": NOW - timedelta(minutes=42),
        "last_sweep": {"direction": "buy", "level_type": "previous_day_low"},
        "last_wick": {"classification": "lower_rejection"},
        "last_breakout": None,
        "detections_today": 7,
    }
    return base | overrides


def test_the_snapshot_carries_every_field_the_phase_names() -> None:
    """EMA + relation + last cross + minutes since, RSI + zone, session + trend, the volume
    profile summary, volatility, and the last wick, sweep and breakout (9C)."""
    from aureon.models.profile import ProfileSummary, VolatilityContext

    snapshot = build_snapshot(
        quote=quote(2450.00),
        state_fields=state_fields(),
        profiles={"asia": ProfileSummary(scope="asia", poc_price=2445.0)},
        volatility=VolatilityContext(atr_14=1.25, regime="high"),
        now=NOW,
    )
    for field in (
        "bid",
        "ask",
        "ema_fast",
        "ema_slow",
        "ema_distance",
        "ema_relation",
        "minutes_since_cross",
        "rsi",
        "rsi_zone",
        "session",
        "session_trend",
        "last_cross",
        "last_sweep",
        "last_wick",
        "last_breakout",
        "volume_profile",
        "volatility",
    ):
        assert field in snapshot, f"{field} is missing from the frozen snapshot"

    assert snapshot["ema_relation"] == "above"
    assert snapshot["minutes_since_cross"] == pytest.approx(42.0)
    assert snapshot["volatility"]["regime"] == "high"
    assert snapshot["volume_profile"]["asia"]["poc_price"] == pytest.approx(2445.0)


def test_the_ema_relation_is_named_rather_than_left_to_a_subtraction() -> None:
    """A reminder is read in a hurry: the sign of a distance is the bias."""
    below = build_snapshot(
        quote=quote(2450.0),
        state_fields=state_fields(ema_fast=2430.0, ema_slow=2440.0),
        now=NOW,
    )
    assert below["ema_relation"] == "below"


def test_an_unknown_ema_gives_no_relation_rather_than_a_guess() -> None:
    snapshot = build_snapshot(
        quote=quote(2450.0), state_fields=state_fields(ema_fast=None), now=NOW
    )
    assert snapshot["ema_relation"] is None


def test_minutes_since_cross_is_computed_at_firing_not_stored() -> None:
    """It is the one field whose value depends on WHEN the alert fired, so a stored "4
    minutes ago" would be worse than nothing."""
    later = build_snapshot(
        quote=quote(2450.0),
        state_fields=state_fields(),
        now=NOW + timedelta(minutes=18),
    )
    assert later["minutes_since_cross"] == pytest.approx(60.0)


def test_no_cross_yet_means_no_minutes_rather_than_zero() -> None:
    snapshot = build_snapshot(
        quote=quote(2450.0),
        state_fields=state_fields(last_cross=None, last_cross_at=None),
        now=NOW,
    )
    assert snapshot["minutes_since_cross"] is None


def test_a_snapshot_without_a_quote_still_records_what_it_knows() -> None:
    """The watcher never fires without a quote, but the builder is also used by the tests
    and must not crash on one."""
    snapshot = build_snapshot(quote=None, state_fields=state_fields(), now=NOW)
    assert snapshot["bid"] is None
    assert snapshot["rsi"] == pytest.approx(61.4)


def test_the_snapshot_is_json_serialisable() -> None:
    """It is stored in Firestore and read back by Discord, so nothing exotic may leak in."""
    import json

    from aureon.models.profile import ProfileSummary, VolatilityContext

    snapshot = build_snapshot(
        quote=quote(2450.0),
        state_fields=state_fields(),
        profiles={"day": ProfileSummary(scope="day", poc_price=2444.0)},
        volatility=VolatilityContext(atr_14=1.0, regime="normal"),
        now=NOW,
    )
    assert json.loads(json.dumps(snapshot))["session"] == "london"
