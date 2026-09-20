"""``/execute`` end to end against the emulator and FakeBroker (9A, §27, §37-§42).

``tests/unit/test_discord_service.py`` pins the rules. What it cannot show is the property
the shortcut exists to preserve: that shortening the *typing* did not shorten the
*authorisation*. That is only visible when the command really writes to Firestore, a real
``ConfirmTradeView`` is really pressed, and a broker is standing by to record anything sent
to it.

So these tests drive the actual command and the actual button against the emulator with a
``FakeBroker`` whose ledger is inspected after every step:

* the command alone places **nothing** at the broker, and the executor refuses the
  REQUESTED document it wrote;
* two presses of CONFIRM produce **one** execution;
* a refusal (a lot above this symbol's ceiling) leaves **no** ``trade_requests`` document
  behind at all -- the refusal is before the embed, so there is nothing for the executor to
  find later;
* a stale quote **re-prompts** with current numbers instead of refusing or proceeding.

The interaction is a stand-in for Discord's transport only. Every decision under test is
Aureon's.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

from aureon.config import AureonConfig
from aureon.discord.commands.execute import ExecuteCommands
from aureon.discord.context import build_context
from aureon.discord.views import ConfirmTradeView
from aureon.execution.fake_broker import DEFAULT_SYMBOL_INFO, FakeBroker
from aureon.models.base import utc_now
from aureon.models.enums import FillingMode, MarketState, Timeframe, TradeRequestStatus
from aureon.models.identity import comment_token
from aureon.models.market import QuoteSnapshot
from aureon.models.settings import ExecutionSettings, SymbolLimits
from aureon.models.system import SymbolState, SystemState
from aureon.storage.settings_repository import ExecutionSettingsRepository
from aureon.storage.symbol_repository import SymbolRepository
from aureon.storage.system_state_repository import SystemStateRepository
from tests.failure_injection.conftest import USER

pytestmark = pytest.mark.emulator

GOLD = "XAUUSD"
SILVER = "XAGUSD"
SILVER_INFO = DEFAULT_SYMBOL_INFO.model_copy(
    update={"symbol": SILVER, "point": 0.001, "digits": 3}
)


# ── A Discord interaction, reduced to what the command actually uses ───────────


@dataclass
class FakeResponse:
    deferred: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    edits: list[dict[str, Any]] = field(default_factory=list)

    async def defer(self, **kwargs: Any) -> None:
        self.deferred = True

    def is_done(self) -> bool:
        return self.deferred

    async def send_message(self, **kwargs: Any) -> None:
        self.messages.append(kwargs)

    async def edit_message(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


@dataclass
class FakeFollowup:
    sends: list[dict[str, Any]] = field(default_factory=list)

    async def send(self, **kwargs: Any) -> None:
        self.sends.append(kwargs)


@dataclass
class FakeUser:
    id: str


class FakeInteraction:
    def __init__(self, user_id: str = USER) -> None:
        self.user = FakeUser(user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    # What the assertions read: the embeds this interaction was shown.
    @property
    def embeds(self) -> list[Any]:
        return [
            call["embed"]
            for call in [*self.followup.sends, *self.response.messages, *self.response.edits]
            if "embed" in call
        ]

    @property
    def views(self) -> list[Any]:
        return [call["view"] for call in self.followup.sends if call.get("view")]


def embed_text(embed: Any) -> str:
    parts = [str(embed.title or ""), str(embed.description or "")]
    parts += [f"{f.name} {f.value}" for f in embed.fields]
    return "\n".join(parts)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def broker() -> FakeBroker:
    """A broker quoting SILVER, overriding the suite's gold-priced default.

    Not cosmetic: the execution guard compares the confirmed quote against the broker's
    live price and refuses a move beyond ``max_deviation_points``. A gold-priced broker
    would refuse every silver order for a reason that has nothing to do with what these
    tests are about -- and would do it *after* CONFIRM, which is precisely the failure the
    guard exists to produce, so it would look like a passing safety test.
    """
    return FakeBroker(bid=30.000, ask=30.020, symbol_info=SILVER_INFO)


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
    """The symbol specs the observer publishes, for both symbols (9A)."""
    symbols = SymbolRepository(firestore_client)
    symbols.publish(DEFAULT_SYMBOL_INFO)
    symbols.publish(SILVER_INFO)


@pytest.fixture
def quote_published(firestore_client):
    """Publish one symbol's quote into its own ``system_state`` document."""

    def publish(symbol: str = SILVER, *, age_seconds: float = 0.0) -> QuoteSnapshot:
        bid, ask, point = (30.000, 30.020, 0.001) if symbol == SILVER else (2400.00, 2400.30, 0.01)
        quote = QuoteSnapshot(
            symbol=symbol,
            bid=bid,
            ask=ask,
            point=point,
            captured_at=utc_now() - timedelta(seconds=age_seconds),
        )
        SystemStateRepository(firestore_client).write(
            SystemState(
                symbols=(
                    SymbolState(
                        symbol=symbol,
                        timeframe=Timeframe.M5,
                        market_state=MarketState.OPEN,
                        last_quote=quote,
                    ),
                )
            ),
            force=True,
        )
        return quote

    return publish


@pytest.fixture
def stored_settings(firestore_client):
    def store(**overrides) -> ExecutionSettings:
        base = dict(
            trading_enabled=True,
            max_lot=1.0,
            max_spread_points=500.0,
            max_deviation_points=20,
            quote_ttl_seconds=15.0,
            confirmation_ttl_seconds=300.0,
        )
        return ExecutionSettingsRepository(firestore_client).write(
            ExecutionSettings(**(base | overrides))
        )

    return store


def press_confirm(view: ConfirmTradeView, interaction: FakeInteraction) -> None:
    """Press the real CONFIRM button.

    ``view.confirm`` is the ``Button`` discord.py builds from the decorated method, and its
    ``callback`` is bound to both the view and the item -- so this runs the same coroutine a
    real click runs, with nothing in between to be lenient on the tests' behalf.
    """
    asyncio.run(view.confirm.callback(interaction))


def run_execute(context, interaction, symbol=SILVER, side="buy", lot="0.10", detection=None):
    asyncio.run(
        ExecuteCommands(context).execute(interaction, symbol, side, lot, detection)
    )


def requests_in(firestore_client) -> list[dict]:
    from aureon.storage import paths

    return [doc.to_dict() for doc in firestore_client.collection(paths.TRADE_REQUESTS).stream()]


# ── The shortcut writes a request and nothing else ─────────────────────────────


def test_execute_without_confirm_places_nothing_at_the_broker(
    context, firestore_client, published, quote_published, stored_settings, broker, make_worker
) -> None:
    """The whole safety property of the shortcut, stated as an execution count.

    One embed, one REQUESTED document, zero broker calls -- and the executor, handed that
    very request, refuses it because it is not CONFIRMED (CLAUDE.md).
    """
    quote_published(SILVER)
    stored_settings()
    interaction = FakeInteraction()

    run_execute(context, interaction)

    assert len(interaction.followup.sends) == 1
    assert len(interaction.views) == 1

    stored = requests_in(firestore_client)
    assert len(stored) == 1
    assert stored[0]["symbol"] == SILVER
    assert stored[0]["status"] == TradeRequestStatus.REQUESTED.value

    assert broker.ledger == []
    request_id = stored[0]["request_id"]
    assert make_worker(broker, executor_id="exec-1").process(request_id) is None
    assert broker.ledger == []
    assert broker.executions_for(comment_token(request_id)) == []


def test_the_embed_names_the_symbol_the_mode_and_the_limits(
    context, firestore_client, published, quote_published, stored_settings
) -> None:
    """§39, §40 and 9A: a shortcut may not shorten what the human is shown."""
    quote_published(SILVER)
    stored_settings(per_symbol={SILVER: SymbolLimits(max_deviation_points=5)})
    interaction = FakeInteraction()

    run_execute(context, interaction)

    text = embed_text(interaction.embeds[0])
    assert SILVER in text
    assert FillingMode.FOK.value.upper() in text
    assert "5 points" in text
    # And the screen says the limits are this symbol's, not the global policy.
    assert f"{SILVER}-specific" in text
    assert "30.02" in text  # the ask it will cross


def test_a_lot_over_the_symbol_ceiling_leaves_no_request_behind(
    context, firestore_client, published, quote_published, stored_settings, broker, make_worker
) -> None:
    """9A: refused before the embed means refused before Firestore.

    A rejected shortcut that still wrote a REQUESTED document would leave something a
    human could later confirm, which is the opposite of a rejection.
    """
    quote_published(SILVER)
    stored_settings(max_lot=1.0, per_symbol={SILVER: SymbolLimits(max_lot=0.20)})
    interaction = FakeInteraction()

    run_execute(context, interaction, lot="0.50")

    assert requests_in(firestore_client) == []
    assert interaction.views == []  # no CONFIRM button was ever offered
    assert "0.2" in embed_text(interaction.embeds[0])
    assert broker.ledger == []

    # The same lot on the symbol without an override is accepted, so the refusal was the
    # per-symbol ceiling and not something incidental.
    quote_published(GOLD)
    gold = FakeInteraction()
    run_execute(context, gold, symbol=GOLD, lot="0.50")
    assert len(requests_in(firestore_client)) == 1


def test_an_unobserved_symbol_is_refused_without_touching_firestore(
    context, firestore_client, published, stored_settings
) -> None:
    stored_settings()
    interaction = FakeInteraction()
    run_execute(context, interaction, symbol="EURUSD")
    assert requests_in(firestore_client) == []
    assert "not observed" in embed_text(interaction.embeds[0])


def test_a_missing_quote_refuses_rather_than_showing_a_price(
    context, firestore_client, published, stored_settings
) -> None:
    """No published quote means a stopped observer, not a tradable market (decision 80)."""
    stored_settings()
    interaction = FakeInteraction()
    run_execute(context, interaction)
    assert requests_in(firestore_client) == []
    assert "No published quote" in embed_text(interaction.embeds[0])


# ── CONFIRM: the part that was not shortened (§27) ─────────────────────────────


def test_a_double_tap_on_confirm_produces_one_execution(
    context, firestore_client, published, quote_published, stored_settings, broker, make_worker
) -> None:
    """Two presses, one trade. The guarantee is transactional, not visual."""
    quote_published(SILVER)
    stored_settings()
    interaction = FakeInteraction()
    run_execute(context, interaction)
    view: ConfirmTradeView = interaction.views[0]

    press_confirm(view, FakeInteraction())
    press_confirm(view, FakeInteraction())

    request_id = view.request.request_id
    stored = context.requests.get(request_id)
    assert stored.status is TradeRequestStatus.CONFIRMED
    assert stored.confirmation_version == 1

    make_worker(broker, executor_id="exec-1").process(request_id)
    make_worker(broker, executor_id="exec-2").process(request_id)
    assert len(broker.executions_for(comment_token(request_id))) == 1
    assert context.requests.get(request_id).status is TradeRequestStatus.FILLED


def test_only_the_requester_may_confirm_a_shortcut(
    context, firestore_client, published, quote_published, stored_settings, broker, make_worker
) -> None:
    """§27. The shortcut is ephemeral, but the authorisation check is not a courtesy."""
    quote_published(SILVER)
    stored_settings()
    interaction = FakeInteraction()
    run_execute(context, interaction)
    view: ConfirmTradeView = interaction.views[0]

    someone_else = FakeInteraction("user-9")
    press_confirm(view, someone_else)

    assert view.confirmed is False
    request_id = view.request.request_id
    assert context.requests.get(request_id).status is TradeRequestStatus.REQUESTED
    assert make_worker(broker, executor_id="exec-1").process(request_id) is None
    assert broker.ledger == []


def test_a_stale_quote_re_prompts_with_current_numbers(
    context, firestore_client, published, quote_published, stored_settings, broker, make_worker
) -> None:
    """§27. The human still wants the trade; they need prices that still exist.

    So the button neither proceeds on the stale price nor throws the request away: it
    redraws the screen from the quote Firestore holds now, and nothing is confirmed until
    a second press on numbers that are current.
    """
    quote_published(SILVER, age_seconds=0.0)
    stored_settings(quote_ttl_seconds=15.0)
    interaction = FakeInteraction()
    run_execute(context, interaction)
    view: ConfirmTradeView = interaction.views[0]

    # The observer's published quote goes stale -- an old capture time is exactly what a
    # stopped observer leaves behind.
    quote_published(SILVER, age_seconds=120.0)

    press = FakeInteraction()
    press_confirm(view, press)

    assert view.confirmed is False
    assert len(press.response.edits) == 1  # the screen was redrawn, not refused
    assert "went stale" in embed_text(press.response.edits[0]["embed"])
    request_id = view.request.request_id
    assert context.requests.get(request_id).status is TradeRequestStatus.REQUESTED
    assert broker.ledger == []

    # A fresh quote, a second press: now it confirms and executes exactly once.
    quote_published(SILVER, age_seconds=0.0)
    press_confirm(view, FakeInteraction())
    assert view.confirmed is True
    make_worker(broker, executor_id="exec-1").process(request_id)
    assert len(broker.executions_for(comment_token(request_id))) == 1
