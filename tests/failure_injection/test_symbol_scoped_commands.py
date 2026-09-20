"""``symbol:`` across the commands, end to end against the emulator (9A, §46, §47, §59).

With one symbol observed, a `symbol:` option is decoration. With two it changes answers and
prevents mistakes, and both of those are only visible once real documents are involved:

* ``/status symbol:`` reads that symbol's own state document, and its open-trade and
  pending-request counts are that symbol's -- a count covering both instruments is the one
  number a trader reads to decide whether they are exposed.
* ``/close symbol:`` closes the one open position on that symbol, and refuses to choose when
  there are two.
* ``symbol:`` on ``/close-trade`` and ``/cancel-order`` is a cross-check on an eight-digit id
  nobody can read: a mismatch writes **no** control request, and an unverifiable claim is
  refused rather than assumed.
* the executor re-checks the asserted symbol against the live position before the broker is
  touched, because Firestore and the broker can disagree and the broker is the account that
  matters.

The interaction is a stand-in for Discord's transport. Every decision under test is Aureon's.
"""

from __future__ import annotations

import asyncio

import pytest

from aureon.config import AureonConfig
from aureon.discord.commands.control import ControlCommands
from aureon.discord.commands.status import StatusCommands
from aureon.discord.context import build_context
from aureon.discord.service import build_control_request
from aureon.execution.control_worker import ControlWorker
from aureon.execution.fake_broker import DEFAULT_SYMBOL_INFO, FakeBroker
from aureon.models.base import MarketTime, utc_now
from aureon.models.enums import (
    ControlRequestKind,
    ControlRequestStatus,
    Direction,
    MarketState,
    OrderType,
    Timeframe,
)
from aureon.models.market import QuoteSnapshot
from aureon.models.settings import ExecutionSettings
from aureon.models.system import SymbolState, SystemState
from aureon.models.trade import BrokerOrderRequest, Trade
from aureon.storage.control_request_repository import ControlRequestRepository
from aureon.storage.settings_repository import ExecutionSettingsRepository
from aureon.storage.symbol_repository import SymbolRepository
from aureon.storage.system_state_repository import (
    HeartbeatRepository,
    SystemStateRepository,
)
from aureon.storage.trade_repository import TradeRepository, trade_id_for
from tests.failure_injection.conftest import MAGIC, USER, make_request
from tests.failure_injection.test_execute_shortcut import (
    GOLD,
    SILVER,
    SILVER_INFO,
    FakeInteraction,
    embed_text,
)

pytestmark = pytest.mark.emulator

TZ = "Europe/Athens"


@pytest.fixture
def config() -> AureonConfig:
    return AureonConfig(
        symbols=(GOLD, SILVER),
        evaluation_rules={GOLD: "XAU_OUTCOME_V2", SILVER: "XAG_OUTCOME_V1"},
        authorized_user_ids=(USER,),
    )


@pytest.fixture
def context(config: AureonConfig, firestore_client):
    return build_context(config, firestore_client)


@pytest.fixture
def published(firestore_client) -> None:
    symbols = SymbolRepository(firestore_client)
    symbols.publish(DEFAULT_SYMBOL_INFO)
    symbols.publish(SILVER_INFO)


@pytest.fixture
def stored_settings(firestore_client) -> ExecutionSettings:
    return ExecutionSettingsRepository(firestore_client).write(
        ExecutionSettings(trading_enabled=True, max_lot=1.0)
    )


@pytest.fixture
def controls(firestore_client) -> ControlRequestRepository:
    return ControlRequestRepository(firestore_client)


@pytest.fixture
def state(firestore_client):
    """Publish one symbol's state document, as the observer does (9A)."""
    repository = SystemStateRepository(firestore_client)

    def publish(symbol: str, *, market_state: MarketState = MarketState.OPEN) -> None:
        bid, ask, point = (30.000, 30.020, 0.001) if symbol == SILVER else (2400.0, 2400.3, 0.01)
        repository.write(
            SystemState(
                symbols=(
                    SymbolState(
                        symbol=symbol,
                        timeframe=Timeframe.M5,
                        market_state=market_state,
                        last_quote=QuoteSnapshot(
                            symbol=symbol, bid=bid, ask=ask, point=point, captured_at=utc_now()
                        ),
                        detections_today=7 if symbol == SILVER else 3,
                    ),
                )
            ),
            force=True,
        )
        HeartbeatRepository(firestore_client).beat("observer", force=True)

    return publish


@pytest.fixture
def opened(firestore_client):
    """An open Trade document, as the monitor writes one from a broker position."""
    trades = TradeRepository(firestore_client)

    def store(symbol: str, position_id: int) -> Trade:
        return trades.upsert_open(
            Trade(
                # The canonical id (§49): ``get_by_position`` computes it rather than
                # querying, so a hand-made trade_id would be written and never found.
                trade_id=trade_id_for(position_id, account_scope="primary"),
                mt5_position_id=position_id,
                symbol=symbol,
                direction=Direction.BUY,
                volume=0.10,
                open_price=30.0 if symbol == SILVER else 2400.0,
                open_time=MarketTime.from_utc(utc_now(), TZ),
                magic=MAGIC,
            )
        )

    return store


def run(coro) -> None:
    asyncio.run(coro)


def controls_in(firestore_client) -> list[dict]:
    from aureon.storage import paths

    return [
        doc.to_dict() for doc in firestore_client.collection(paths.CONTROL_REQUESTS).stream()
    ]


# ── /status symbol: ───────────────────────────────────────────────────────────


def test_status_scoped_to_one_symbol_renders_that_symbols_live_panel(
    context, state, stored_settings
) -> None:
    """Half of 9A's done-when: `/status symbol:XAGUSD` renders live."""
    state(GOLD)
    state(SILVER)
    interaction = FakeInteraction()
    run(StatusCommands(context).status(interaction, SILVER))

    text = embed_text(interaction.embeds[0])
    assert SILVER in text
    assert GOLD not in text  # the other symbol is not on this screen at all
    assert MarketState.OPEN.value in text


def test_status_scoped_to_one_symbol_renders_the_closed_market_mode(
    context, state, stored_settings
) -> None:
    """The other half: on a CLOSED market the same command shows the review section.

    Scoping must not change WHICH of the two §59/§61-§63 modes is chosen -- that is decided
    by the market state of the symbol asked about, which is the point of reading its own
    document.
    """
    state(GOLD, market_state=MarketState.OPEN)
    state(SILVER, market_state=MarketState.CLOSED)

    silver = FakeInteraction()
    run(StatusCommands(context).status(silver, SILVER))
    assert "review" in embed_text(silver.embeds[0]).lower()

    gold = FakeInteraction()
    run(StatusCommands(context).status(gold, GOLD))
    assert "review" not in embed_text(gold.embeds[0]).lower()


def test_a_scoped_status_counts_only_that_symbols_trades_and_requests(
    context, firestore_client, state, stored_settings, opened
) -> None:
    from aureon.storage.trade_request_repository import TradeRequestRepository

    state(SILVER)
    opened(GOLD, 111)
    opened(SILVER, 222)
    requests = TradeRequestRepository(firestore_client)
    requests.create(make_request(request_id="req-gold"))
    silver_request = make_request(request_id="req-silver")
    requests.create(silver_request.model_copy(update={"symbol": SILVER}))

    # The screen itself, not the rendered embed: an embed is full of digits, and
    # "1 appears somewhere in the text" would pass for a screen counting both symbols.
    commands = StatusCommands(context)
    scoped = asyncio.run(commands._build(SILVER))
    assert (scoped.open_trades, scoped.pending_requests) == (1, 1)

    everything = asyncio.run(commands._build(None))
    assert (everything.open_trades, everything.pending_requests) == (2, 2)


def test_status_refuses_a_symbol_this_deployment_does_not_observe(
    context, state, stored_settings
) -> None:
    state(GOLD)
    interaction = FakeInteraction()
    run(StatusCommands(context).status(interaction, "EURUSD"))
    assert "not observed" in embed_text(interaction.embeds[0])


# ── /close symbol: ────────────────────────────────────────────────────────────


def test_the_close_shortcut_targets_the_one_open_position_on_that_symbol(
    context, firestore_client, published, opened
) -> None:
    opened(GOLD, 111)
    opened(SILVER, 222)
    interaction = FakeInteraction()
    run(ControlCommands(context).close(interaction, SILVER))

    stored = controls_in(firestore_client)
    assert len(stored) == 1
    assert stored[0]["kind"] == ControlRequestKind.CLOSE.value
    assert stored[0]["target"] == "222"
    assert stored[0]["symbol"] == SILVER
    assert stored[0]["status"] == ControlRequestStatus.REQUESTED.value


def test_the_close_shortcut_refuses_to_choose_between_two_positions(
    context, firestore_client, published, opened
) -> None:
    """Aureon does not get to decide which of a human's trades to end."""
    opened(SILVER, 222)
    opened(SILVER, 333)
    interaction = FakeInteraction()
    run(ControlCommands(context).close(interaction, SILVER))

    assert controls_in(firestore_client) == []
    text = embed_text(interaction.embeds[0])
    assert "222" in text and "333" in text


def test_the_close_shortcut_says_so_when_nothing_is_open(
    context, firestore_client, published, opened
) -> None:
    opened(GOLD, 111)
    interaction = FakeInteraction()
    run(ControlCommands(context).close(interaction, SILVER))
    assert controls_in(firestore_client) == []
    assert SILVER in embed_text(interaction.embeds[0])


# ── symbol: as a cross-check on /close-trade and /cancel-order ────────────────


def test_a_close_naming_the_wrong_symbol_writes_nothing(
    context, firestore_client, published, opened
) -> None:
    """9A. The typo that matters: one digit wrong points at the other instrument."""
    opened(GOLD, 111)
    interaction = FakeInteraction()
    run(ControlCommands(context).close_trade(interaction, "111", None, SILVER))

    assert controls_in(firestore_client) == []
    text = embed_text(interaction.embeds[0])
    assert GOLD in text and SILVER in text


def test_a_close_naming_the_right_symbol_proceeds_and_records_it(
    context, firestore_client, published, opened
) -> None:
    opened(SILVER, 222)
    interaction = FakeInteraction()
    run(ControlCommands(context).close_trade(interaction, "222", None, SILVER))

    stored = controls_in(firestore_client)
    assert len(stored) == 1
    assert stored[0]["symbol"] == SILVER


def test_a_symbol_aureon_cannot_verify_is_refused_rather_than_assumed(
    context, firestore_client, published
) -> None:
    """No trade record for this position id, so the claim cannot be checked at all."""
    interaction = FakeInteraction()
    run(ControlCommands(context).close_trade(interaction, "999", None, SILVER))
    assert controls_in(firestore_client) == []
    assert "no record" in embed_text(interaction.embeds[0])


def test_the_unchecked_action_is_still_available_without_a_symbol(
    context, firestore_client, published
) -> None:
    """Omitting `symbol:` is the deliberate way to act on a raw ticket, and still works."""
    interaction = FakeInteraction()
    run(ControlCommands(context).close_trade(interaction, "999", None, None))
    stored = controls_in(firestore_client)
    assert len(stored) == 1
    assert stored[0]["symbol"] is None


def test_a_cancel_checks_the_ticket_against_the_request_that_placed_it(
    context, firestore_client, published
) -> None:
    from aureon.models.enums import TradeRequestStatus
    from aureon.storage.trade_request_repository import TradeRequestRepository

    requests = TradeRequestRepository(firestore_client)
    placed = make_request(request_id="req-gold", order_type=OrderType.BUY_STOP, price=2405.0)
    requests.create(placed)
    requests.confirm(placed.request_id, USER, placed.quote, confirmation_ttl_seconds=300.0)
    requests.claim(placed.request_id, "exec-1")
    requests.resolve(
        placed.request_id,
        "exec-1",
        TradeRequestStatus.PENDING,
        updates={"order_ticket": 88_123},
    )

    wrong = FakeInteraction()
    run(ControlCommands(context).cancel_order(wrong, "88123", SILVER))
    assert controls_in(firestore_client) == []
    assert GOLD in embed_text(wrong.embeds[0])

    right = FakeInteraction()
    run(ControlCommands(context).cancel_order(right, "88123", GOLD))
    assert len(controls_in(firestore_client)) == 1


# ── The executor checks the same assertion against the broker ─────────────────


def test_the_executor_refuses_a_close_whose_live_symbol_disagrees(
    firestore_client, controls
) -> None:
    """Firestore said one thing; the broker is the account that matters (9A).

    Nothing is closed, the request FAILS with a message naming both symbols, and the position
    is still open afterwards -- which is the assertion that distinguishes "refused" from
    "closed and reported oddly".
    """
    broker = FakeBroker(symbol_info=DEFAULT_SYMBOL_INFO)
    result = broker.send_market_order(
        BrokerOrderRequest(
            symbol=GOLD,
            order_type=OrderType.MARKET_BUY,
            volume=0.10,
            deviation_points=20,
            magic=MAGIC,
            comment="AUR:TEST",
        )
    )
    position_id = result.position_id
    assert position_id is not None

    control = controls.create(
        build_control_request(
            ControlRequestKind.CLOSE, str(position_id), USER, symbol=SILVER
        )
    )
    worker = ControlWorker(controls, broker, executor_id="exec-1")
    resolved = worker.process(control.control_id)

    assert resolved is not None
    assert resolved.status is ControlRequestStatus.FAILED
    assert SILVER in (resolved.failure_message or "")
    assert GOLD in (resolved.failure_message or "")
    assert len(broker.open_positions()) == 1
    assert [entry for entry in broker.ledger if entry.kind == "close"] == []


def test_the_executor_performs_a_close_whose_live_symbol_matches(
    firestore_client, controls
) -> None:
    broker = FakeBroker(symbol_info=SILVER_INFO, bid=30.0, ask=30.02)
    result = broker.send_market_order(
        BrokerOrderRequest(
            symbol=SILVER,
            order_type=OrderType.MARKET_BUY,
            volume=0.10,
            deviation_points=20,
            magic=MAGIC,
            comment="AUR:TEST",
        )
    )
    control = controls.create(
        build_control_request(
            ControlRequestKind.CLOSE, str(result.position_id), USER, symbol=SILVER
        )
    )
    resolved = ControlWorker(controls, broker, executor_id="exec-1").process(
        control.control_id
    )
    assert resolved is not None
    assert resolved.status is ControlRequestStatus.COMPLETED
    assert broker.open_positions() == []
