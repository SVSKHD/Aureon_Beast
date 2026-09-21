"""``/note`` against the emulator, and the weekly review that prints them (9D-4, §45, §63).

One property carries this file: **a note never touches its trade**. A CLOSED trade refuses
every field write but the reconciliation stamps, because a closed trade is a record of what
happened and editing one silently rewrites history — so a note that reached the trade
document would be the exact failure §45 exists to prevent, arriving through the one command
designed to be used after the fact.

Everything else here is the consequence: the note goes to its own collection, the weekly
review copies it in beside that trade's outcome, `#tags` group the week by the trader's own
vocabulary, and not one counted number moves because somebody wrote a sentence.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from aureon.config import AureonConfig
from aureon.discord.commands.note import NoteCommands
from aureon.discord.context import build_context
from aureon.models.base import MarketTime, utc_now
from aureon.models.enums import Direction, TradeStatus
from aureon.models.trade import Trade
from tests.failure_injection.conftest import USER, FakeInteraction, embed_text

pytestmark = pytest.mark.emulator

GOLD = "XAUUSD"
TZ = "Europe/Athens"


@pytest.fixture
def context(firestore_client):
    return build_context(
        AureonConfig(
            symbols=(GOLD,),
            evaluation_rules={GOLD: "XAU_OUTCOME_V2"},
            authorized_user_ids=(USER,),
        ),
        firestore_client,
    )


@pytest.fixture
def closed_trade(firestore_client):
    """A CLOSED trade, written straight to the document as the monitor would leave it."""

    def store(trade_id: str = "t-1", *, minutes_ago: float = 60.0) -> Trade:
        from aureon.storage import paths

        opened = utc_now() - timedelta(minutes=minutes_ago)
        trade = Trade(
            trade_id=trade_id,
            mt5_position_id=abs(hash(trade_id)) % 10**8,
            symbol=GOLD,
            direction=Direction.BUY,
            volume=0.1,
            open_price=2400.0,
            open_time=MarketTime.from_utc(opened, TZ),
            close_time=MarketTime.from_utc(opened + timedelta(minutes=30), TZ),
            close_price=2402.0,
            closed_volume=0.1,
            status=TradeStatus.CLOSED,
        )
        firestore_client.document(paths.trade_path(trade_id)).set(
            trade.model_dump(mode="json")
        )
        return trade

    return store


def run_note(context, interaction, *, trade: str, text: str):
    asyncio.run(NoteCommands(context).note(interaction, trade, text))


def stored_notes(firestore_client) -> list[dict]:
    from aureon.storage import paths

    return [doc.to_dict() for doc in firestore_client.collection(paths.TRADE_NOTES).stream()]


def trade_document(firestore_client, trade_id: str) -> dict:
    from aureon.storage import paths

    return firestore_client.document(paths.trade_path(trade_id)).get().to_dict()


# ── The property ──────────────────────────────────────────────────────────────


def test_a_note_on_a_closed_trade_leaves_the_trade_byte_for_byte(
    context, firestore_client, closed_trade
) -> None:
    """§45. The trade document is compared in full, not field by field: a test naming the
    fields it expects unchanged cannot notice a new one being added."""
    closed_trade("t-1")
    before = trade_document(firestore_client, "t-1")

    interaction = FakeInteraction()
    run_note(context, interaction, trade="t-1", text="chased it #london")

    assert trade_document(firestore_client, "t-1") == before
    assert "Noted" in embed_text(interaction.embeds[0])

    notes = stored_notes(firestore_client)
    assert len(notes) == 1
    assert notes[0]["trade_id"] == "t-1"
    assert notes[0]["text"] == "chased it #london"
    assert notes[0]["author"] == USER


def test_several_notes_on_one_trade_are_kept_in_order(
    context, firestore_client, closed_trade
) -> None:
    """A thread, not a field: the second note does not replace the first."""
    closed_trade("t-1")
    run_note(context, FakeInteraction(), trade="t-1", text="first")
    run_note(context, FakeInteraction(), trade="t-1", text="second")

    found = context.notes.for_trade("t-1")
    assert [n.text for n in found] == ["first", "second"]


def test_last_names_the_most_recently_opened_trade(
    context, firestore_client, closed_trade
) -> None:
    closed_trade("t-old", minutes_ago=600)
    closed_trade("t-new", minutes_ago=30)

    run_note(context, FakeInteraction(), trade="last", text="the one I just did")
    assert stored_notes(firestore_client)[0]["trade_id"] == "t-new"


def test_an_unknown_trade_is_refused_and_nothing_is_written(
    context, firestore_client
) -> None:
    interaction = FakeInteraction()
    run_note(context, interaction, trade="nope", text="anything")
    assert "No such trade" in embed_text(interaction.embeds[0])
    assert stored_notes(firestore_client) == []


def test_an_empty_note_is_refused(context, firestore_client, closed_trade) -> None:
    """A blank row under a trade is worse than no row: a reader next year would wonder
    what was meant and have no way to find out."""
    closed_trade("t-1")
    interaction = FakeInteraction()
    run_note(context, interaction, trade="t-1", text="   ")
    assert "Nothing to record" in embed_text(interaction.embeds[0])
    assert stored_notes(firestore_client) == []


def test_an_unauthorized_user_writes_nothing(context, firestore_client, closed_trade) -> None:
    closed_trade("t-1")
    interaction = FakeInteraction("user-999")
    run_note(context, interaction, trade="t-1", text="not mine to write")
    assert "Not authorized" in embed_text(interaction.embeds[0])
    assert stored_notes(firestore_client) == []


# ── The weekly review prints them ─────────────────────────────────────────────


def test_the_review_prints_notes_under_their_trade_and_groups_by_tag(
    context, firestore_client, closed_trade
) -> None:
    """§63. The words a human wrote, beside the outcome Aureon measured."""
    from aureon.evaluation.rules import XAU_OUTCOME_V2
    from aureon.reviews.service import ReviewService

    opened = utc_now() - timedelta(minutes=60)
    closed_trade("t-1", minutes_ago=60)
    closed_trade("t-2", minutes_ago=50)

    run_note(context, FakeInteraction(), trade="t-1", text="chased it #chased #london")
    run_note(context, FakeInteraction(), trade="t-2", text="fine setup #london")

    service = ReviewService(
        firestore_client,
        XAU_OUTCOME_V2,
        market_tz=TZ,
        infer_window_minutes=10,
        symbol=GOLD,
    )
    iso = opened.isocalendar()
    review = service.generate_weekly(iso.year, iso.week, store=False)

    assert review.trade_notes["t-1"] == ("chased it #chased #london",)
    assert review.trade_notes["t-2"] == ("fine setup #london",)
    assert review.trades_by_tag["london"] == ("t-1", "t-2")
    assert review.trades_by_tag["chased"] == ("t-1",)


def test_a_trade_with_no_note_gets_no_empty_row(
    context, firestore_client, closed_trade
) -> None:
    from aureon.evaluation.rules import XAU_OUTCOME_V2
    from aureon.reviews.service import ReviewService

    opened = utc_now() - timedelta(minutes=60)
    closed_trade("t-1", minutes_ago=60)

    service = ReviewService(
        firestore_client,
        XAU_OUTCOME_V2,
        market_tz=TZ,
        infer_window_minutes=10,
        symbol=GOLD,
    )
    iso = opened.isocalendar()
    review = service.generate_weekly(iso.year, iso.week, store=False)
    assert review.trade_notes == {}
    assert review.trades_by_tag == {}
