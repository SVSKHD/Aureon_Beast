"""Position lifecycle: MT5 is the truth (§44, §45, §49-§53, §58, §77).

Every scenario the phase names, against the real emulator and FakeBroker. The theme is
that Aureon never *decides* a trade closed -- so a stop-loss, a close from a phone, and a
position opened by hand in the terminal must all land correctly without Aureon being
involved in any of them.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aureon.execution.fake_broker import FakeBroker
from aureon.models.base import MarketTime, utc_now
from aureon.models.broker import BrokerDeal
from aureon.models.enums import (
    DealEntry,
    Direction,
    ExcursionSource,
    Timeframe,
    TradeRequestStatus,
    TradeSource,
    TradeStatus,
)
from aureon.models.market import Candle
from aureon.models.trade import Trade
from aureon.positions.position_monitor import PositionMonitor
from aureon.storage.trade_repository import TradeRepository, trade_id_for
from aureon.storage.trade_request_repository import TradeRequestRepository
from tests.failure_injection.conftest import MAGIC, SYMBOL, make_request

pytestmark = pytest.mark.emulator

SCOPE = "primary"
TZ = "Europe/Athens"


@pytest.fixture
def trades(firestore_client) -> TradeRepository:
    return TradeRepository(firestore_client, account_scope=SCOPE)


@pytest.fixture
def monitor(trades, repository, broker) -> PositionMonitor:
    return PositionMonitor(
        trades, repository, broker, magic=MAGIC, account_scope=SCOPE, market_tz=TZ, point=0.01
    )


def executed(repository: TradeRequestRepository, broker: FakeBroker, make_worker) -> tuple:
    """Confirm and execute one market order, returning (request, position_id)."""
    request = make_request()
    repository.create(request)
    repository.confirm(request.request_id, request.requested_by, request.quote)
    make_worker(broker, executor_id="exec-1").process(request.request_id)
    stored = repository.get(request.request_id)
    assert stored.status is TradeRequestStatus.FILLED
    return stored, stored.position_id


def close_deal(broker: FakeBroker, position_id: int, *, reason: str, price: float, profit: float):
    """Inject an exit deal with a specific broker reason, as a real close would."""
    position = next(p for p in broker.open_positions() if p.position_id == position_id)
    broker._positions.pop(position_id)  # noqa: SLF001 - simulating the broker's own change
    deal = BrokerDeal(
        deal_id=99_000 + position_id,
        position_id=position_id,
        symbol=position.symbol,
        direction=Direction.SELL if position.direction is Direction.BUY else Direction.BUY,
        entry=DealEntry.OUT,
        volume=position.volume,
        price=price,
        executed_at=utc_now(),
        profit=profit,
        magic=position.magic,
        reason=reason,
    )
    broker._deals.append(deal)  # noqa: SLF001
    return deal


# ── Opening ───────────────────────────────────────────────────────────────────


def test_a_filled_order_becomes_an_open_trade(
    repository, broker, trades, monitor, make_worker
) -> None:
    request, position_id = executed(repository, broker, make_worker)
    monitor.poll_once()

    trade = trades.get_by_position(position_id)
    assert trade is not None
    assert trade.status is TradeStatus.OPEN
    assert trade.source is TradeSource.AUREON
    assert trade.trade_request_id == request.request_id
    assert trade.volume == pytest.approx(request.volume)


def test_polling_twice_does_not_duplicate_a_trade(
    repository, broker, trades, monitor, make_worker
) -> None:
    """The monitor re-sees every position on every poll; it must upsert, not accumulate."""
    executed(repository, broker, make_worker)
    monitor.poll_once()
    monitor.poll_once()
    assert len(trades.open_trades()) == 1


# ── Closing: the broker decides, we observe ───────────────────────────────────


@pytest.mark.parametrize(
    "reason,expected_close_reason",
    [("sl", "sl"), ("tp", "tp"), ("mobile", "mobile"), ("client", "manual"), ("so", "broker")],
)
def test_a_close_is_recorded_with_the_right_reason(
    repository, broker, trades, monitor, make_worker, reason: str, expected_close_reason: str
) -> None:
    """§44. A stop-loss, a take-profit and a phone all land without Aureon acting."""
    _, position_id = executed(repository, broker, make_worker)
    monitor.poll_once()

    close_deal(broker, position_id, reason=reason, price=2395.00, profit=-50.0)
    monitor.poll_once()

    trade = trades.get_by_position(position_id)
    assert trade.status is TradeStatus.CLOSED
    assert trade.close_price == pytest.approx(2395.00)
    assert trade.close_reason == expected_close_reason
    assert trade.close_reason_raw == reason
    assert trade.close_time is not None
    assert trade.realized_pnl == pytest.approx(-50.0)


def test_realized_pnl_is_the_sum_of_the_closing_deals(
    repository, broker, trades, monitor, make_worker
) -> None:
    """The broker's arithmetic, not ours.

    Recomputing from prices would disagree with the account statement the moment a
    contract size, a currency conversion, or a two-price partial close is involved.
    """
    _, position_id = executed(repository, broker, make_worker)
    monitor.poll_once()

    position = next(p for p in broker.open_positions() if p.position_id == position_id)
    broker._positions.pop(position_id)  # noqa: SLF001
    for index, (volume, price, profit) in enumerate(
        [(0.04, 2403.0, 12.0), (0.06, 2405.0, 30.0)]
    ):
        broker._deals.append(  # noqa: SLF001
            BrokerDeal(
                deal_id=98_100 + index,
                position_id=position_id,
                symbol=position.symbol,
                direction=Direction.SELL,
                entry=DealEntry.OUT,
                volume=volume,
                price=price,
                executed_at=utc_now() + timedelta(seconds=index),
                profit=profit,
                magic=MAGIC,
                reason="tp",
            )
        )
    monitor.poll_once()

    trade = trades.get_by_position(position_id)
    assert trade.status is TradeStatus.CLOSED
    assert trade.realized_pnl == pytest.approx(42.0)  # 12 + 30
    assert trade.close_price == pytest.approx(2405.0), "the LAST exit price is reported"


def test_a_close_while_the_monitor_was_down_is_reconstructed_on_restart(
    repository, broker, trades, monitor, make_worker
) -> None:
    """§77. The gap is the interesting part.

    The monitor never sees the position open -- it closes entirely while the process is
    down -- and must still record the close from the deals alone.
    """
    _, position_id = executed(repository, broker, make_worker)
    monitor.poll_once()  # records it open
    assert trades.get_by_position(position_id).status is TradeStatus.OPEN

    # Monitor is down. A human closes it from their phone.
    close_deal(broker, position_id, reason="mobile", price=2412.00, profit=120.0)

    # Restart.
    restarted = PositionMonitor(
        trades, repository, broker, magic=MAGIC, account_scope=SCOPE, market_tz=TZ
    )
    restarted.startup()

    trade = trades.get_by_position(position_id)
    assert trade.status is TradeStatus.CLOSED
    assert trade.close_reason == "mobile"
    assert trade.close_price == pytest.approx(2412.00)


def test_a_vanished_position_with_no_exit_deal_yet_is_left_alone(
    repository, broker, trades, monitor, make_worker
) -> None:
    """Inventing a close price would be worse than waiting a poll or two."""
    _, position_id = executed(repository, broker, make_worker)
    monitor.poll_once()

    broker._positions.pop(position_id)  # noqa: SLF001 - gone, no deal published yet
    monitor.poll_once()

    assert trades.get_by_position(position_id).status is TradeStatus.OPEN


# ── Partial closes (§44) ──────────────────────────────────────────────────────


def test_a_partial_close_is_recorded_as_partially_closed(
    repository, broker, trades, monitor, make_worker
) -> None:
    """0.25 down to 0.10: still open, but smaller than we recorded."""
    request = make_request(volume=0.25)
    repository.create(request)
    repository.confirm(request.request_id, request.requested_by, request.quote)
    make_worker(broker, executor_id="exec-1").process(request.request_id)
    position_id = repository.get(request.request_id).position_id
    monitor.poll_once()

    broker.close_position(position_id, 0.15)  # 0.25 -> 0.10 remaining
    monitor.poll_once()

    trade = trades.get_by_position(position_id)
    assert trade.status is TradeStatus.PARTIALLY_CLOSED
    assert trade.closed_volume == pytest.approx(0.15)
    assert trade.remaining_volume == pytest.approx(0.10)


def test_a_partial_then_full_close_ends_closed(
    repository, broker, trades, monitor, make_worker
) -> None:
    request = make_request(volume=0.25)
    repository.create(request)
    repository.confirm(request.request_id, request.requested_by, request.quote)
    make_worker(broker, executor_id="exec-1").process(request.request_id)
    position_id = repository.get(request.request_id).position_id
    monitor.poll_once()

    broker.close_position(position_id, 0.15)
    monitor.poll_once()
    assert trades.get_by_position(position_id).status is TradeStatus.PARTIALLY_CLOSED

    broker.close_position(position_id)  # the rest
    monitor.poll_once()

    trade = trades.get_by_position(position_id)
    assert trade.status is TradeStatus.CLOSED
    assert trade.closed_volume == pytest.approx(0.25)


# ── External positions (§52) ──────────────────────────────────────────────────


def test_a_position_opened_by_hand_is_imported_not_managed(
    repository, broker, trades, monitor
) -> None:
    """§52. Aureon did not open it and has no authorisation to touch it."""
    from aureon.models.enums import OrderType
    from aureon.models.trade import BrokerOrderRequest

    broker.send_market_order(
        BrokerOrderRequest(
            symbol=SYMBOL,
            order_type=OrderType.MARKET_BUY,
            volume=0.50,
            magic=999_999,  # not Aureon's magic
            comment="opened by hand",
        )
    )
    monitor.poll_once()

    imported = [t for t in trades.open_trades() if t.source is TradeSource.EXTERNAL_MT5]
    assert len(imported) == 1
    assert imported[0].trade_request_id is None
    assert imported[0].volume == pytest.approx(0.50)


def test_an_aureon_magic_position_with_no_request_is_still_external(
    repository, broker, trades, monitor
) -> None:
    """Our magic but no matching request means we cannot account for it (§52)."""
    from aureon.models.enums import OrderType
    from aureon.models.trade import BrokerOrderRequest

    broker.send_market_order(
        BrokerOrderRequest(
            symbol=SYMBOL,
            order_type=OrderType.MARKET_BUY,
            volume=0.20,
            magic=MAGIC,
            comment="AUR:ORPHAN",
        )
    )
    monitor.poll_once()

    trade = trades.open_trades()[0]
    assert trade.source is TradeSource.EXTERNAL_MT5
    assert trade.trade_request_id is None


def test_an_external_close_is_recorded_too(repository, broker, trades, monitor) -> None:
    from aureon.models.enums import OrderType
    from aureon.models.trade import BrokerOrderRequest

    result = broker.send_market_order(
        BrokerOrderRequest(
            symbol=SYMBOL,
            order_type=OrderType.MARKET_BUY,
            volume=0.20,
            magic=999_999,
            comment="hand",
        )
    )
    monitor.poll_once()
    close_deal(broker, result.position_id, reason="client", price=2399.0, profit=-20.0)
    monitor.poll_once()

    trade = trades.get_by_position(result.position_id)
    assert trade.status is TradeStatus.CLOSED
    assert trade.close_reason == "manual"


# ── Pending orders (§43) ──────────────────────────────────────────────────────


def test_a_pending_order_that_filled_while_offline_is_resolved(
    repository, broker, trades, monitor, make_worker
) -> None:
    """The request was PENDING; it filled while the monitor was down."""
    from aureon.models.enums import OrderType

    request = make_request(order_type=OrderType.BUY_STOP, price=2405.00)
    repository.create(request)
    repository.confirm(request.request_id, request.requested_by, request.quote)
    make_worker(broker, executor_id="exec-1").process(request.request_id)
    resting = repository.get(request.request_id)
    assert resting.status is TradeRequestStatus.PENDING

    broker.fill_pending(resting.order_ticket)  # monitor is down
    monitor.startup()

    settled = repository.get(request.request_id)
    assert settled.status is TradeRequestStatus.FILLED
    assert settled.position_id is not None
    assert trades.get_by_position(settled.position_id) is not None


def test_a_pending_order_cancelled_while_offline_is_resolved(
    repository, broker, trades, monitor, make_worker
) -> None:
    """Gone with no fill and no expiry: CANCELLED, not EXPIRED (decision 61)."""
    from aureon.models.enums import OrderType

    request = make_request(order_type=OrderType.BUY_STOP, price=2405.00)
    repository.create(request)
    repository.confirm(request.request_id, request.requested_by, request.quote)
    make_worker(broker, executor_id="exec-1").process(request.request_id)
    resting = repository.get(request.request_id)

    broker.cancel_order(resting.order_ticket)
    monitor.poll_once()

    assert repository.get(request.request_id).status is TradeRequestStatus.CANCELLED


def test_a_still_resting_pending_order_is_left_alone(
    repository, broker, monitor, make_worker
) -> None:
    from aureon.models.enums import OrderType

    request = make_request(order_type=OrderType.BUY_STOP, price=2405.00)
    repository.create(request)
    repository.confirm(request.request_id, request.requested_by, request.quote)
    make_worker(broker, executor_id="exec-1").process(request.request_id)

    monitor.poll_once()
    assert repository.get(request.request_id).status is TradeRequestStatus.PENDING


# ── Excursions (§45) ──────────────────────────────────────────────────────────


def test_excursions_follow_a_scripted_price_path(
    repository, broker, trades, monitor, make_worker
) -> None:
    """§45. MFE and MAE from live quotes, signed by the position's direction."""
    _, position_id = executed(repository, broker, make_worker)
    monitor.poll_once()
    trade_id = trade_id_for(position_id, account_scope=SCOPE)

    # Entry was at the ask (2400.30). Exits are measured at the bid for a long.
    for bid, ask in [(2401.00, 2401.30), (2398.00, 2398.30), (2400.00, 2400.30)]:
        broker.set_quote(bid=bid, ask=ask)
        monitor.poll_once()

    trade = trades.get(trade_id)
    assert trade.excursion.mfe is not None
    assert trade.excursion.mae is not None
    # Best bid 2401.00 against a 2400.30 entry = +70 points; worst 2398.00 = -230.
    assert trade.excursion.mfe == pytest.approx(70.0, abs=1.0)
    assert trade.excursion.mae == pytest.approx(-230.0, abs=1.0)
    assert trade.excursion.source is ExcursionSource.LIVE_TICKS


def test_reconstructed_excursions_are_flagged_as_such(
    repository, broker, trades, monitor, make_worker
) -> None:
    """§45. Candle-rebuilt figures must never claim to be live ticks.

    A 1-minute candle cannot say whether the position was still open at the moment of its
    high, so a review that mixed the two would overstate its own precision.
    """
    _, position_id = executed(repository, broker, make_worker)
    monitor.poll_once()
    trade = trades.get_by_position(position_id)

    base = trade.open_time.utc
    candles = [
        Candle(
            symbol=SYMBOL,
            timeframe=Timeframe.M1,
            open_time=MarketTime.from_utc(base + timedelta(minutes=i), TZ),
            open=2400.0,
            high=2400.0 + i,
            low=2400.0 - i,
            close=2400.0,
        )
        for i in range(1, 4)
    ]
    monitor.reconstruct_excursions(trade, candles)

    rebuilt = trades.get_by_position(position_id)
    assert rebuilt.excursion.source is ExcursionSource.RECONSTRUCTED
    assert rebuilt.excursion.mfe is not None


def test_a_closed_trades_excursions_are_final(
    repository, broker, trades, monitor, make_worker
) -> None:
    """A late tick must not extend the excursions of a position that has closed."""
    _, position_id = executed(repository, broker, make_worker)
    monitor.poll_once()
    broker.set_quote(bid=2401.00, ask=2401.30)
    monitor.poll_once()
    close_deal(broker, position_id, reason="tp", price=2401.0, profit=7.0)
    monitor.poll_once()

    trade_id = trade_id_for(position_id, account_scope=SCOPE)
    before = trades.get(trade_id).excursion
    trades.update_excursion(
        trade_id, before.model_copy(update={"mfe": 99_999.0})
    )
    assert trades.get(trade_id).excursion.mfe == pytest.approx(before.mfe)


# ── §58: the monitor is independent of the trading switch ─────────────────────


def test_the_monitor_runs_with_trading_disabled(
    repository, broker, trades, monitor, make_worker, settings
) -> None:
    """§58. Disabling trading stops the executor, not the recording of live positions.

    A monitor that paused alongside the executor would blind the operator during exactly
    the incident that made them disable trading.
    """
    _, position_id = executed(repository, broker, make_worker)
    monitor.poll_once()

    settings.trading_enabled = False  # the executor is now blocked

    close_deal(broker, position_id, reason="sl", price=2390.0, profit=-100.0)
    result = monitor.poll_once()

    assert result.closed, "the monitor stopped recording when trading was disabled"
    assert trades.get_by_position(position_id).status is TradeStatus.CLOSED


def test_the_monitor_never_places_an_order(
    repository, broker, trades, monitor, make_worker
) -> None:
    """It records what happened; it does not act."""
    executed(repository, broker, make_worker)
    broker.calls.clear()
    before = len(broker.ledger)

    monitor.poll_once()
    monitor.poll_once()

    assert "send_market_order" not in broker.calls
    assert "send_pending_order" not in broker.calls
    assert "close_position" not in broker.calls
    assert "cancel_order" not in broker.calls
    assert len(broker.ledger) == before


# ── Audit (§60) ───────────────────────────────────────────────────────────────


def test_trade_transitions_are_audited(
    repository, firestore_client, broker, trades, monitor, make_worker
) -> None:
    from aureon.storage import paths

    _, position_id = executed(repository, broker, make_worker)
    monitor.poll_once()
    close_deal(broker, position_id, reason="tp", price=2410.0, profit=97.0)
    monitor.poll_once()

    trade_id = trade_id_for(position_id, account_scope=SCOPE)
    actions = sorted(
        doc.to_dict()["action"]
        for doc in firestore_client.collection(paths.AUDIT_LOGS).stream()
        if doc.to_dict().get("document_id") == trade_id
    )
    assert "trade.open" in actions
    assert "trade.closed" in actions


def test_an_illegal_trade_transition_is_refused(trades) -> None:
    """CLOSED is terminal; reopening would rewrite history."""
    from aureon.storage.trade_repository import TradeTransitionRejected

    trade = Trade(
        trade_id="t-illegal",
        mt5_position_id=1,
        symbol=SYMBOL,
        direction=Direction.BUY,
        volume=0.1,
        open_price=2400.0,
        open_time=MarketTime.from_utc(utc_now(), TZ),
    )
    trades.upsert_open(trade)
    trades.transition(
        trade.trade_id,
        TradeStatus.CLOSED,
        updates={"close_time": MarketTime.from_utc(utc_now(), TZ)},
    )
    with pytest.raises(TradeTransitionRejected):
        trades.transition(trade.trade_id, TradeStatus.OPEN)


# ── Regression: incomplete exit evidence must not freeze a wrong P&L ──────────


def test_incomplete_exit_deals_do_not_mark_a_trade_closed(
    repository, broker, trades, monitor, make_worker
) -> None:
    """CLOSED is terminal, so it must never be written on partial evidence.

    A real bug: the position vanishes from the broker's open list while only *some* of its
    exit deals are visible -- easily caused by a broker timestamp a second ahead of our
    clock. Closing there froze a partial ``realized_pnl`` that could never be corrected,
    because no transition leaves CLOSED.

    The fix records PARTIALLY_CLOSED and finishes on a later poll.
    """
    request = make_request(volume=0.10)
    repository.create(request)
    repository.confirm(request.request_id, request.requested_by, request.quote)
    make_worker(broker, executor_id="exec-1").process(request.request_id)
    position_id = repository.get(request.request_id).position_id
    monitor.poll_once()

    position = next(p for p in broker.open_positions() if p.position_id == position_id)
    broker._positions.pop(position_id)  # noqa: SLF001 - fully closed at the broker

    def exit_deal(deal_id: int, volume: float, price: float, profit: float) -> BrokerDeal:
        return BrokerDeal(
            deal_id=deal_id,
            position_id=position_id,
            symbol=position.symbol,
            direction=Direction.SELL,
            entry=DealEntry.OUT,
            volume=volume,
            price=price,
            executed_at=utc_now(),
            profit=profit,
            magic=MAGIC,
            reason="tp",
        )

    # Only the first of two exit deals is visible so far.
    broker._deals.append(exit_deal(97_001, 0.04, 2403.0, 12.0))  # noqa: SLF001
    monitor.poll_once()

    partial = trades.get_by_position(position_id)
    assert partial.status is TradeStatus.PARTIALLY_CLOSED, (
        "a trade was marked CLOSED on incomplete exit evidence, freezing a wrong P&L"
    )
    assert partial.realized_pnl == pytest.approx(12.0)

    # The remaining deal arrives.
    broker._deals.append(exit_deal(97_002, 0.06, 2405.0, 30.0))  # noqa: SLF001
    monitor.poll_once()

    final = trades.get_by_position(position_id)
    assert final.status is TradeStatus.CLOSED
    assert final.realized_pnl == pytest.approx(42.0), "the corrected total must be recorded"
    assert final.closed_volume == pytest.approx(0.10)
