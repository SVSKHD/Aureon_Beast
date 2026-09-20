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


# ── /remind, driven through the real command ──────────────────────────────────


@pytest.fixture
def context(firestore_client):
    from aureon.config import AureonConfig
    from aureon.discord.context import build_context

    config = AureonConfig(
        symbols=("XAUUSD", "XAGUSD"),
        evaluation_rules={"XAUUSD": "XAU_OUTCOME_V2", "XAGUSD": "XAG_OUTCOME_V1"},
        authorized_user_ids=(USER,),
    )
    return build_context(config, firestore_client)


@pytest.fixture
def quote_published(firestore_client):
    """Publish a quote into the symbol's own state document, as the observer does."""
    from aureon.models.enums import MarketState, Timeframe
    from aureon.models.market import QuoteSnapshot
    from aureon.models.system import SymbolState, SystemState
    from aureon.storage.system_state_repository import SystemStateRepository

    def publish(symbol: str = SYMBOL, *, bid: float = 3690.00, ask: float = 3690.30) -> None:
        SystemStateRepository(firestore_client).write(
            SystemState(
                symbols=(
                    SymbolState(
                        symbol=symbol,
                        timeframe=Timeframe.M5,
                        market_state=MarketState.OPEN,
                        last_quote=QuoteSnapshot(
                            symbol=symbol, bid=bid, ask=ask, point=0.01,
                            captured_at=utc_now(),
                        ),
                    ),
                )
            ),
            force=True,
        )

    return publish


def run_remind(context, interaction, **kwargs):
    import asyncio

    from aureon.discord.commands.remind import RemindCommands

    return asyncio.run(RemindCommands(context).price(interaction, **kwargs))


def test_remind_price_arms_one_alert_and_says_what_it_will_do(
    context, quote_published, firestore_client
) -> None:
    """9C's done-when, the arming half."""
    from tests.failure_injection.test_execute_shortcut import FakeInteraction, embed_text

    quote_published()
    interaction = FakeInteraction()
    run_remind(
        context, interaction, symbol=SYMBOL, level=3700.0, side="above", note="range high"
    )

    armed = PriceAlertRepository(firestore_client).armed(user_id=USER)
    assert len(armed) == 1
    assert armed[0].level == pytest.approx(3700.0)
    assert armed[0].note == "range high"
    assert armed[0].expires_at is not None

    text = embed_text(interaction.embeds[0])
    assert "Armed" in text and "3700" in text
    # The message states the three things it will NOT do.
    assert "once" in text
    assert "CONFIRM" in text


def test_remind_price_refuses_a_level_already_behind_the_market(
    context, quote_published, firestore_client
) -> None:
    from tests.failure_injection.test_execute_shortcut import FakeInteraction, embed_text

    quote_published()
    interaction = FakeInteraction()
    run_remind(context, interaction, symbol=SYMBOL, level=3600.0, side="above")

    assert PriceAlertRepository(firestore_client).armed(user_id=USER) == []
    assert "Did you mean `below`?" in embed_text(interaction.embeds[0])


def test_remind_price_refuses_a_symbol_with_no_published_quote(
    context, firestore_client
) -> None:
    """A stopped observer is not a market to set a level in."""
    from tests.failure_injection.test_execute_shortcut import FakeInteraction, embed_text

    interaction = FakeInteraction()
    run_remind(context, interaction, symbol=SYMBOL, level=3700.0, side="above")
    assert PriceAlertRepository(firestore_client).armed() == []
    assert "No published quote" in embed_text(interaction.embeds[0])


def test_remind_list_and_cancel_are_the_users_own(
    context, quote_published, firestore_client
) -> None:
    import asyncio

    from aureon.discord.commands.remind import RemindCommands
    from tests.failure_injection.test_execute_shortcut import FakeInteraction, embed_text

    quote_published()
    run_remind(context, FakeInteraction(), symbol=SYMBOL, level=3700.0, side="above")
    mine = PriceAlertRepository(firestore_client).armed(user_id=USER)
    assert len(mine) == 1

    listing = FakeInteraction()
    asyncio.run(RemindCommands(context).list_alerts(listing))
    assert mine[0].alert_id in embed_text(listing.embeds[0])

    # Somebody else cannot cancel it, and the alert stays armed.
    intruder = FakeInteraction("user-9")
    asyncio.run(RemindCommands(context).cancel(intruder, mine[0].alert_id))
    assert (
        PriceAlertRepository(firestore_client).get(mine[0].alert_id).status
        is PriceAlertStatus.ARMED
    )

    owner = FakeInteraction()
    asyncio.run(RemindCommands(context).cancel(owner, mine[0].alert_id))
    assert (
        PriceAlertRepository(firestore_client).get(mine[0].alert_id).status
        is PriceAlertStatus.CANCELLED
    )
    assert "Cancelled" in embed_text(owner.embeds[0])


# ── The observer answers alerts from the quotes it reads ──────────────────────

#: A level the fixture's first candles are below and its later ones are above, so a replay
#: crosses it exactly once. Derived from the candles rather than written down, in the test.


@pytest.fixture
def observer_factory(tmp_path, firestore_client, candles):
    """A real Observer over the fixture week, wired to the emulator (9C)."""
    from aureon.config import AureonConfig
    from aureon.outbox.local_outbox import LocalOutbox
    from aureon.outbox.outbox_worker import OutboxWorker
    from aureon.services.observer_state import ObserverState
    from aureon.storage.detection_repository import DetectionRepository
    from main_observer import Observer
    from tests.conftest import FakeLiveProvider, cross_agent

    def build(*, suffix: str = "a"):
        config = AureonConfig.from_env(
            env={
                "AUREON_ACCOUNT_SCOPE": "primary",
                "AUREON_MARKET_TZ": "Europe/Athens",
                "AUREON_SYMBOLS": SYMBOL,
                "AUREON_TIMEFRAMES": "M5",
                # Shared across restarts on purpose: the cursor and the queue survive, as
                # they do in production.
                "AUREON_OUTBOX_PATH": str(tmp_path / "outbox.db"),
                "AUREON_OBSERVER_STATE_PATH": str(tmp_path / "observer_state.json"),
            }
        )
        outbox = LocalOutbox(config.outbox_path)
        observer = Observer(
            config,
            FakeLiveProvider(candles),
            outbox=outbox,
            worker=OutboxWorker(outbox, DetectionRepository(firestore_client).upsert_payload),
            state=ObserverState(config.observer_state_path),
            agents=[cross_agent()],
            alert_repository=PriceAlertRepository(firestore_client),
        )
        return observer, outbox

    return build


def test_a_level_crossed_by_the_observers_quote_fires_once(
    observer_factory, alerts: PriceAlertRepository, candles
) -> None:
    """9C's done-when: a fake quote crosses the level and one reminder is owed.

    The level is taken from the fixture itself -- above the price at the cut, below a later
    one -- so the crossing is a fact about the data rather than a number somebody chose.
    """
    observer, outbox = observer_factory()
    provider = observer.provider

    cut = 200
    provider.set_clock(candles[cut].close_time)
    start_price = provider.get_quote(SYMBOL).ask
    later = max(c.close for c in candles[cut : cut + 300])
    level = round((start_price + later) / 2, 2)
    assert start_price < level < later, "the fixture does not cross this level"

    stored = alerts.arm(
        PriceAlert(
            alert_id=new_alert_id(),
            symbol=SYMBOL,
            level=level,
            side="above",
            requested_by=USER,
        )
    )

    # Advance candle by candle, exactly as the live loop does.
    for candle in candles[cut : cut + 300]:
        provider.advance_to_close_of(candle)
        observer.market_engine.poll_once()
        if alerts.get(stored.alert_id).status is PriceAlertStatus.FIRED:
            break

    fired = alerts.get(stored.alert_id)
    assert fired.status is PriceAlertStatus.FIRED
    assert fired.fired_price >= level
    assert fired.fired_at is not None
    # The snapshot the phase asks for, frozen by the process that has the indicators.
    assert fired.fired_snapshot["bid"] is not None
    assert "ema_relation" in fired.fired_snapshot
    assert "volatility" in fired.fired_snapshot
    outbox.close()


def test_it_fires_once_across_an_observer_restart(
    observer_factory, alerts: PriceAlertRepository, candles
) -> None:
    """The phase's own test. The claim is transactional, so a restart cannot re-answer it.

    Kept honest by checking the snapshot too: a second firing would overwrite it with a
    later market, which is the damage the test is really about.
    """
    first, outbox_a = observer_factory()
    provider = first.provider
    cut = 200
    provider.set_clock(candles[cut].close_time)
    level = round(provider.get_quote(SYMBOL).ask + 0.5, 2)

    stored = alerts.arm(
        PriceAlert(
            alert_id=new_alert_id(),
            symbol=SYMBOL,
            level=level,
            side="above",
            requested_by=USER,
        )
    )

    for candle in candles[cut : cut + 200]:
        provider.advance_to_close_of(candle)
        first.market_engine.poll_once()
        if alerts.get(stored.alert_id).status is PriceAlertStatus.FIRED:
            break
    fired_once = alerts.get(stored.alert_id)
    assert fired_once.status is PriceAlertStatus.FIRED
    outbox_a.close()

    # A new process over the same state, replaying the same quotes.
    second, outbox_b = observer_factory(suffix="b")
    second.provider.set_clock(provider.now_utc())
    for candle in candles[cut + 200 : cut + 260]:
        second.provider.advance_to_close_of(candle)
        second.market_engine.poll_once()

    final = alerts.get(stored.alert_id)
    assert final.fired_at == fired_once.fired_at
    assert final.fired_price == fired_once.fired_price
    assert final.fired_snapshot == fired_once.fired_snapshot
    outbox_b.close()


def test_an_expired_alert_is_never_answered_by_a_later_quote(
    observer_factory, alerts: PriceAlertRepository, candles
) -> None:
    """Expiry runs on candle close, and an expired alert is terminal (9C)."""
    observer, outbox = observer_factory()
    provider = observer.provider
    cut = 200
    provider.set_clock(candles[cut].close_time)
    level = round(provider.get_quote(SYMBOL).ask + 0.5, 2)

    stored = alerts.arm(
        PriceAlert(
            alert_id=new_alert_id(),
            symbol=SYMBOL,
            level=level,
            side="above",
            requested_by=USER,
            # Already past its window when the observer next closes a candle.
            expires_at=utc_now() - timedelta(minutes=1),
        )
    )

    provider.advance_to_close_of(candles[cut + 1])
    observer.market_engine.poll_once()
    assert alerts.get(stored.alert_id).status is PriceAlertStatus.EXPIRED

    for candle in candles[cut + 2 : cut + 120]:
        provider.advance_to_close_of(candle)
        observer.market_engine.poll_once()

    final = alerts.get(stored.alert_id)
    assert final.status is PriceAlertStatus.EXPIRED
    assert final.fired_at is None
    assert final.fired_snapshot == {}
    outbox.close()


def test_a_symbol_with_no_armed_alert_costs_no_extra_quote_read(
    observer_factory, alerts: PriceAlertRepository, candles
) -> None:
    """The check is cheap in the common case, and the test can SEE that.

    The provider counts quote reads, because "we did not do the expensive thing" is
    otherwise invisible and a test that cannot see it passes whatever the code does. This
    observer has no state repository, so the only reason to read a quote at all is an armed
    alert: none means **zero** reads, and one means one.
    """
    observer, outbox = observer_factory()
    provider = observer.provider
    provider.set_clock(candles[200].close_time)

    provider.quote_calls = 0
    provider.advance_to_close_of(candles[201])
    observer.market_engine.poll_once()
    without_alerts = provider.quote_calls
    assert without_alerts == 0, "no armed alert, so no quote was read for one"

    alerts.arm(
        PriceAlert(
            alert_id=new_alert_id(),
            symbol=SYMBOL,
            level=99_999.0,  # never reached, so nothing fires and only the read is counted
            side="above",
            requested_by=USER,
        )
    )
    provider.quote_calls = 0
    provider.advance_to_close_of(candles[202])
    observer.market_engine.poll_once()
    assert provider.quote_calls == without_alerts + 1
    outbox.close()
