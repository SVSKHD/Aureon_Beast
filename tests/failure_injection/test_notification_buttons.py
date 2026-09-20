"""The buttons under an announcement, against the emulator and a broker (9C, §71).

``tests/unit/test_notifier.py`` pins what is announced and how often. What it cannot show
is the property the [Execute] button is most likely to quietly lose: that arriving at a
trade from a *notification* is no shorter than arriving at one from ``/execute``.

A button carrying a size would make the most consequential number in the system a thing
somebody tapped rather than typed. A button that skipped CONFIRM would make a detection
create a trade, which CLAUDE.md forbids outright. Neither is visible in a unit test of the
notifier, because neither is the notifier's decision -- so these drive the real button, the
real modal and the real ``ConfirmTradeView`` with a ``FakeBroker`` standing by, and count
what reached it at every step.

The interaction is a stand-in for Discord's transport only. Every decision under test is
Aureon's.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from aureon.config import AureonConfig
from aureon.discord.context import build_context
from aureon.discord.views import ConfirmTradeView, LotModal, NotificationView
from aureon.execution.fake_broker import DEFAULT_SYMBOL_INFO, FakeBroker
from aureon.models.base import MarketTime, utc_now
from aureon.models.detection import Detection, IndicatorSnapshot, SessionContext
from aureon.models.enums import (
    Direction,
    MarketState,
    SessionName,
    Timeframe,
    TradeRequestStatus,
)
from aureon.models.identity import comment_token
from aureon.models.market import QuoteSnapshot
from aureon.models.settings import ExecutionSettings
from aureon.models.system import SymbolState, SystemState
from aureon.storage.detection_repository import DetectionRepository
from aureon.storage.settings_repository import ExecutionSettingsRepository
from aureon.storage.symbol_repository import SymbolRepository
from aureon.storage.system_state_repository import SystemStateRepository
from tests.failure_injection.conftest import USER, FakeInteraction, embed_text

pytestmark = pytest.mark.emulator

GOLD = "XAUUSD"
TZ = "Europe/Athens"
STRANGER = "user-999"


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker(bid=2400.00, ask=2400.30, symbol_info=DEFAULT_SYMBOL_INFO)


@pytest.fixture
def context(firestore_client):
    return build_context(
        AureonConfig(
            symbols=(GOLD,),
            authorized_user_ids=(USER,),
            alert_channel_id=4242,
        ),
        firestore_client,
    )


@pytest.fixture
def market(firestore_client) -> QuoteSnapshot:
    """Everything the confirmation reads: the published spec, a fresh quote, settings."""
    SymbolRepository(firestore_client).publish(DEFAULT_SYMBOL_INFO)
    quote = QuoteSnapshot(
        symbol=GOLD, bid=2400.00, ask=2400.30, point=0.01, captured_at=utc_now()
    )
    SystemStateRepository(firestore_client).write(
        SystemState(
            symbols=(
                SymbolState(
                    symbol=GOLD,
                    timeframe=Timeframe.M5,
                    market_state=MarketState.OPEN,
                    last_quote=quote,
                ),
            )
        ),
        force=True,
    )
    ExecutionSettingsRepository(firestore_client).write(
        ExecutionSettings(
            trading_enabled=True,
            max_lot=1.0,
            max_spread_points=500.0,
            max_deviation_points=20,
            quote_ttl_seconds=15.0,
            confirmation_ttl_seconds=300.0,
        )
    )
    return quote


@pytest.fixture
def announced(firestore_client) -> Detection:
    """A stored detection, as the notifier would have found it."""
    moment = utc_now()
    detection = Detection(
        detection_id="det-notify-1",
        account_scope="primary",
        symbol=GOLD,
        timeframe=Timeframe.M5,
        agent_name="ema_cross",
        agent_version="2.1.0",
        event_key="bullish",
        direction=Direction.BUY,
        detected_at=MarketTime.from_utc(moment, TZ),
        candle_open_time=MarketTime.from_utc(moment - timedelta(minutes=5), TZ),
        price=2400.50,
        indicators=IndicatorSnapshot(ema={"fast": 2401.0, "slow": 2395.0}, rsi=61.4),
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
    )
    DetectionRepository(firestore_client).upsert(detection)
    return detection


# ── Driving the real components ───────────────────────────────────────────────


def view_for(context, detection: Detection | None, *, side: str | None = "buy"):
    return NotificationView(
        context,
        symbol=GOLD,
        detection_id=None if detection is None else detection.detection_id,
        side=side,
    )


def press(button, interaction: FakeInteraction) -> None:
    """Press a real button.

    ``view.execute`` is the ``Button`` discord.py builds from the decorated method, and its
    ``callback`` is bound to both the view and the item -- so this runs the same coroutine a
    real click runs, with nothing in between to be lenient on the tests' behalf.
    """
    asyncio.run(button.callback(interaction))


def submit(modal: LotModal, interaction: FakeInteraction, lot: str) -> None:
    """Type a lot into the modal and submit it.

    The value goes in through discord.py's own ``_refresh_state``, which is what the
    gateway calls with the submitted payload, rather than by assigning the field -- a test
    that set the attribute directly would still pass if the modal stopped reading the input
    at all.
    """
    modal.lot._refresh_state(interaction, {"value": lot})  # type: ignore[arg-type]
    asyncio.run(modal.on_submit(interaction))


def requests_in(firestore_client) -> list[dict]:
    from aureon.storage import paths

    return [
        doc.to_dict() for doc in firestore_client.collection(paths.TRADE_REQUESTS).stream()
    ]


# ── [Execute] asks for the lot and writes nothing ─────────────────────────────


def test_the_execute_button_asks_for_a_lot_and_places_nothing(
    context, firestore_client, market, announced, broker
) -> None:
    """One press, one modal, zero documents, zero broker calls.

    The press is where a "one-tap trade" would live if it existed. It does not: everything
    the press produces is a question.
    """
    interaction = FakeInteraction()
    press(view_for(context, announced).execute, interaction)

    assert len(interaction.modals) == 1
    assert isinstance(interaction.modals[0], LotModal)
    assert interaction.followup.sends == []
    assert requests_in(firestore_client) == []
    assert broker.ledger == []


def test_the_lot_field_offers_no_default_and_no_last_time(
    context, market, announced
) -> None:
    """A prefilled size is a decision made by the interface. The lot is the one number the
    human types every time."""
    interaction = FakeInteraction()
    press(view_for(context, announced).execute, interaction)
    field = interaction.modals[0].lot

    assert field.default is None
    assert field.value == ""
    assert field.required is True


def test_the_modal_reaches_the_same_confirmation_as_the_slash_command(
    context, firestore_client, market, announced, broker, make_worker
) -> None:
    """A REQUESTED document and a CONFIRM button -- and the executor, handed that very
    request, refuses it because it is not CONFIRMED (CLAUDE.md)."""
    interaction = FakeInteraction()
    press(view_for(context, announced).execute, interaction)
    submit(interaction.modals[0], interaction, "0.10")

    stored = requests_in(firestore_client)
    assert len(stored) == 1
    assert stored[0]["status"] == TradeRequestStatus.REQUESTED.value
    assert stored[0]["volume"] == 0.10
    assert len(interaction.views) == 1
    assert isinstance(interaction.views[0], ConfirmTradeView)

    assert broker.ledger == []
    request_id = stored[0]["request_id"]
    assert make_worker(broker, executor_id="exec-1").process(request_id) is None
    assert broker.ledger == []


def test_the_announcement_links_its_detection_into_the_request(
    context, firestore_client, market, announced
) -> None:
    """§50: an execution opened from an embed carries that detection's id, which makes the
    link an explicit statement by a human rather than a proximity guess in a later review."""
    interaction = FakeInteraction()
    press(view_for(context, announced).execute, interaction)
    submit(interaction.modals[0], interaction, "0.10")

    stored = requests_in(firestore_client)[0]
    assert stored["detection_id"] == announced.detection_id
    assert embed_text(interaction.embeds[0])


def test_confirming_from_an_announcement_places_exactly_one_order(
    context, firestore_client, market, announced, broker, make_worker
) -> None:
    """The button is a shortcut through the typing, not through the authorisation."""
    interaction = FakeInteraction()
    press(view_for(context, announced).execute, interaction)
    submit(interaction.modals[0], interaction, "0.10")
    view = interaction.views[0]

    press(view.confirm, FakeInteraction())
    request_id = requests_in(firestore_client)[0]["request_id"]
    assert (
        requests_in(firestore_client)[0]["status"] == TradeRequestStatus.CONFIRMED.value
    )

    worker = make_worker(broker, executor_id="exec-1")
    worker.process(request_id)
    assert len(broker.executions_for(comment_token(request_id))) == 1

    # And a second press of the same button adds nothing.
    press(view.confirm, FakeInteraction())
    worker.process(request_id)
    assert len(broker.executions_for(comment_token(request_id))) == 1


def test_a_reader_who_may_not_trade_gets_no_modal(
    context, firestore_client, market, announced, broker
) -> None:
    """The channel is readable by more people than may trade (§71), and the button is in
    the channel."""
    interaction = FakeInteraction(STRANGER)
    press(view_for(context, announced).execute, interaction)

    assert interaction.modals == []
    assert requests_in(firestore_client) == []
    assert broker.ledger == []
    assert "Not authorized" in embed_text(interaction.embeds[0])


def test_a_context_only_announcement_has_no_execute_button(context, market) -> None:
    """There is no side to prefill, and inventing one is the guess this design refuses."""
    labels = {item.label for item in view_for(context, None, side=None).children}
    assert labels == {"Monitor"}


def test_monitor_points_at_the_command_rather_than_answering_itself(
    context, firestore_client, market, announced, broker
) -> None:
    """Two renderings of "how is this detection doing" would drift, and the one inside a
    notification would be the one nobody updated."""
    interaction = FakeInteraction()
    press(view_for(context, announced).monitor, interaction)

    text = embed_text(interaction.embeds[0])
    assert f"/monitor detection:{announced.detection_id}" in text
    assert requests_in(firestore_client) == []
    assert broker.ledger == []
