"""Trades, and the two rules that protect what was already recorded (§49-§53, §58).

MT5 is the truth about positions. This table is Aureon's record of what MT5 said, so the
interesting failures are not "the write did not happen" but "the write destroyed something".
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aureon.models.base import MarketTime
from aureon.models.enums import TradeSource, TradeStatus
from aureon.models.trade import Excursion
from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.trades import (
    COLLECTION,
    TerminalWriteRejected,
    TradeRepository,
    TradeTransitionRejected,
    trade_id_for,
)
from tests.postgres.factories import CLOSE, TZ, a_trade

pytestmark = pytest.mark.postgres

T = TradeStatus
TRADE_ID = trade_id_for(123456789012, account_scope="primary")


@pytest.fixture
def repo(schema: Database) -> TradeRepository:
    return TradeRepository(schema)


def close(repo: TradeRepository, trade_id: str = TRADE_ID, **updates: object):
    """Close a trade the way the model insists on.

    ``Trade`` refuses a CLOSED status without a ``close_time`` -- a closed position with no
    closing moment is a record nothing can be measured from. So the helper always supplies
    one, and the tests below are about what happens AFTER a legitimate close.
    """
    payload: dict = {"close_time": MarketTime.from_utc(CLOSE + timedelta(hours=1), TZ)}
    payload.update(updates)
    return repo.transition(trade_id, T.CLOSED, updates=payload)


# ── The round trip ────────────────────────────────────────────────────────────


def test_a_trade_survives_the_database_unchanged(repo: TradeRepository) -> None:
    written = repo.upsert_open(a_trade())
    assert repo.get(TRADE_ID) == written


def test_a_ten_digit_position_id_survives(repo: TradeRepository) -> None:
    """The reason the column is BIGINT. ``int4`` stops at 2147483647.

    An overflow here would be a failed write AFTER the order was sent -- the position exists
    and Aureon's record of it does not.
    """
    repo.upsert_open(a_trade(9_223_372_036_854_775_000))
    found = repo.get_by_position(9_223_372_036_854_775_000)
    assert found is not None and found.mt5_position_id == 9_223_372_036_854_775_000


def test_a_trade_is_findable_by_position_id(repo: TradeRepository) -> None:
    """How MT5 identifies one. A caller holding a position id from the broker should not
    have to know this repository's account scope to look it up."""
    repo.upsert_open(a_trade())
    found = repo.get_by_position(123456789012)
    assert found is not None and found.trade_id == TRADE_ID
    assert repo.get_by_position(999) is None


# ── An existing position is left alone ────────────────────────────────────────


def test_re_recording_an_open_position_does_not_overwrite_it(repo: TradeRepository) -> None:
    """The monitor calls this every poll, so this is the common path.

    The stored row carries excursion figures accumulated over hours of ticks, and a naive
    overwrite from a fresh broker read would erase them -- silently, because the overwrite
    looks like a successful sync.
    """
    repo.upsert_open(a_trade())
    repo.update_excursion(TRADE_ID, Excursion(mfe=12.0, mae=-3.0))

    repo.upsert_open(a_trade())  # a fresh broker read, no excursion

    stored = repo.get(TRADE_ID)
    assert stored is not None
    assert stored.excursion.mfe == 12.0, "the re-record erased the accumulated excursion"


def test_recording_a_position_is_audited(repo: TradeRepository) -> None:
    repo.upsert_open(a_trade())
    trail = repo.audit.for_document(COLLECTION, TRADE_ID)
    assert [record.action for record in trail] == ["trade.open"]


# ── Transitions ───────────────────────────────────────────────────────────────


def test_a_position_can_close(repo: TradeRepository) -> None:
    repo.upsert_open(a_trade())
    closed = close(repo, close_price=2410.0, realized_pnl=64.0)
    assert closed.status is T.CLOSED
    assert closed.realized_pnl == 64.0
    assert closed.close_time is not None


def test_a_closed_position_cannot_reopen(repo: TradeRepository) -> None:
    """The transition table's business, and it must keep saying so even for a terminal row."""
    repo.upsert_open(a_trade())
    close(repo)
    with pytest.raises(TradeTransitionRejected):
        repo.transition(TRADE_ID, T.OPEN)


def test_partially_closed_is_re_entrant(repo: TradeRepository) -> None:
    """0.25 → 0.10 → 0.05 is three partial closes, not one."""
    repo.upsert_open(a_trade(volume=0.25))
    repo.transition(TRADE_ID, T.PARTIALLY_CLOSED, updates={"closed_volume": 0.15})
    again = repo.transition(TRADE_ID, T.PARTIALLY_CLOSED, updates={"closed_volume": 0.20})
    assert again.closed_volume == 0.20


def test_transitioning_something_that_does_not_exist_is_refused(
    repo: TradeRepository,
) -> None:
    with pytest.raises(TradeTransitionRejected, match="no such"):
        repo.transition("nope", T.CLOSED)


def test_an_unchanged_status_with_nothing_to_write_is_a_no_op(repo: TradeRepository) -> None:
    repo.upsert_open(a_trade())
    before = len(repo.audit.for_document(COLLECTION, TRADE_ID))
    repo.transition(TRADE_ID, T.OPEN)
    assert len(repo.audit.for_document(COLLECTION, TRADE_ID)) == before


# ── §58: a settled trade is history ───────────────────────────────────────────


def test_a_settled_pnl_cannot_be_rewritten(repo: TradeRepository) -> None:
    """The gap the transition table cannot see.

    ``CLOSED → CLOSED`` is not a transition, so nothing in the table stops it rewriting a
    settled trade's ``realized_pnl`` -- which would make the P&L, and every review built on
    it, unfalsifiable.
    """
    repo.upsert_open(a_trade())
    close(repo, realized_pnl=64.0)

    with pytest.raises(TerminalWriteRejected, match="realized_pnl"):
        repo.transition(TRADE_ID, T.CLOSED, updates={"realized_pnl": 6400.0})

    stored = repo.get(TRADE_ID)
    assert stored is not None and stored.realized_pnl == 64.0


def test_the_observational_stamps_may_still_be_written(repo: TradeRepository) -> None:
    """They say when it was last LOOKED at, not what happened -- so writing them cannot
    change a settled outcome, and reconciliation needs to record that it checked."""
    repo.upsert_open(a_trade())
    close(repo, realized_pnl=64.0)
    stamped = repo.transition(
        TRADE_ID,
        T.CLOSED,
        updates={"last_reconciled_at": CLOSE + timedelta(hours=2)},
        reconciliation=True,
    )
    assert stamped.last_reconciled_at is not None
    assert stamped.realized_pnl == 64.0


def test_the_refusal_names_what_it_refused(repo: TradeRepository) -> None:
    """An operator reading this has to know which field was rejected, not just that one was."""
    repo.upsert_open(a_trade())
    close(repo)
    with pytest.raises(TerminalWriteRejected) as raised:
        repo.transition(TRADE_ID, T.CLOSED, updates={"close_price": 1.0, "swap": 2.0})
    message = str(raised.value)
    assert "close_price" in message and "swap" in message
    assert "last_reconciled_at" in message, "it does not say what IS allowed"


# ── Excursions ────────────────────────────────────────────────────────────────


def test_an_excursion_update_is_not_audited(repo: TradeRepository) -> None:
    """Excursions update continuously while a position is open; auditing every tick-driven
    revision would bury the transitions that matter in noise."""
    repo.upsert_open(a_trade())
    before = len(repo.audit.for_document(COLLECTION, TRADE_ID))
    repo.update_excursion(TRADE_ID, Excursion(mfe=5.0, mae=-1.0))
    assert len(repo.audit.for_document(COLLECTION, TRADE_ID)) == before


def test_a_closed_trades_excursions_are_final(repo: TradeRepository) -> None:
    """A late tick must not extend them."""
    repo.upsert_open(a_trade())
    repo.update_excursion(TRADE_ID, Excursion(mfe=12.0, mae=-3.0))
    close(repo)
    repo.update_excursion(TRADE_ID, Excursion(mfe=999.0, mae=-999.0))
    stored = repo.get(TRADE_ID)
    assert stored is not None and stored.excursion.mfe == 12.0


def test_an_excursion_update_on_a_missing_trade_is_none(repo: TradeRepository) -> None:
    assert repo.update_excursion("nope", Excursion(mfe=1.0, mae=0.0)) is None


# ── Queries ───────────────────────────────────────────────────────────────────


def test_open_trades_include_the_partially_closed(repo: TradeRepository) -> None:
    """A position with volume left is still a position, and omitting it would leave the
    monitor blind to the remainder."""
    repo.upsert_open(a_trade(1))
    repo.upsert_open(a_trade(2))
    repo.upsert_open(a_trade(3))
    repo.transition(trade_id_for(2, account_scope="primary"), T.PARTIALLY_CLOSED)
    close(repo, trade_id_for(3, account_scope="primary"))

    positions = {t.mt5_position_id for t in repo.open_trades()}
    assert positions == {1, 2}


def test_external_trades_are_distinguishable(repo: TradeRepository) -> None:
    """§52: an external trade is imported and never managed, so the source is a column the
    monitor filters on rather than a flag buried in a payload."""
    repo.upsert_open(a_trade(1, source=TradeSource.AUREON))
    repo.upsert_open(a_trade(2, source=TradeSource.EXTERNAL_MT5, magic=None))
    external = [t for t in repo.open_trades() if t.source is TradeSource.EXTERNAL_MT5]
    assert [t.mt5_position_id for t in external] == [2]


def test_a_period_counts_trades_by_when_they_opened(repo: TradeRepository) -> None:
    repo.upsert_open(a_trade())
    assert [t.trade_id for t in repo.in_period(CLOSE, CLOSE + timedelta(minutes=1))] == [TRADE_ID]
    assert repo.in_period(CLOSE - timedelta(minutes=1), CLOSE) == []


def test_list_by_status_can_be_scoped_to_a_symbol(repo: TradeRepository) -> None:
    repo.upsert_open(a_trade(1, symbol="XAUUSD"))
    repo.upsert_open(a_trade(2, symbol="XAGUSD", open_price=31.2))
    found = repo.list_by_status(T.OPEN, symbol="XAGUSD")
    assert [t.mt5_position_id for t in found] == [2]
