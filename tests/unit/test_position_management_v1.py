"""GAP 2/3/4/12: live management of Aureon-owned positions only, one position, restart-safe."""

from __future__ import annotations

import pytest

from aureon.execution.control_worker import ControlWorker
from aureon.execution.execution_guard import BrokerSnapshot, GuardResult, check
from aureon.execution.fake_broker import FakeBroker
from aureon.models.base import utc_now
from aureon.models.control import ControlRequest
from aureon.models.enums import (
    AccountMode,
    ControlRequestKind,
    ControlRequestStatus,
    Direction,
    FailureCode,
    ManagementPhase,
    MarketState,
    OrderType,
    TradeSource,
)
from aureon.models.market import QuoteSnapshot
from aureon.models.settings import ExecutionSettings
from aureon.models.trade import BrokerOrderRequest, TradeRequest
from aureon.positions.position_monitor import PositionMonitor
from aureon.storage.postgres.repositories.control_requests import ControlRequestRepository
from aureon.storage.postgres.repositories.trade_requests import TradeRequestRepository
from aureon.storage.postgres.repositories.trades import TerminalWriteRejected, TradeRepository

MAGIC = 770177
SYMBOL = "XAUUSD"


def _open(broker: FakeBroker, *, magic: int, comment: str, direction=Direction.BUY, sl=None):
    result = broker.send_market_order(
        BrokerOrderRequest(
            symbol=SYMBOL,
            order_type=OrderType.MARKET_BUY
            if direction is Direction.BUY
            else OrderType.MARKET_SELL,
            volume=0.1,
            sl=sl,
            magic=magic,
            comment=comment,
        )
    )
    assert result.ok
    return result.position_id


def _monitor(local_store, broker, *, controls=None, autonomous=False, policy=None):
    return PositionMonitor(
        TradeRepository(local_store, account_scope="primary"),
        TradeRequestRepository(local_store),
        broker,
        magic=MAGIC,
        account_scope="primary",
        market_tz="Europe/Athens",
        poll_seconds=0.0,
        exit_policy=policy,
        controls=controls,
        autonomous_management=autonomous,
    )


def _request_for(local_store, position_id: int, comment: str) -> None:
    """An Aureon position is one with our magic AND a matching request (§52)."""
    from aureon.models.enums import TradeRequestStatus

    repo = TradeRequestRepository(local_store)
    request = TradeRequest(
        request_id="req-aureon",
        symbol=SYMBOL,
        order_type=OrderType.MARKET_BUY,
        volume=0.1,
        requested_by="user",
        quote=QuoteSnapshot(
            symbol=SYMBOL, bid=2400.0, ask=2400.3, point=0.01, captured_at=utc_now()
        ),
    )
    repo.create(request)
    repo.confirm("req-aureon", "user", request.quote, confirmation_ttl_seconds=60)
    repo.claim("req-aureon", "exec-1", lease_seconds=60)
    repo.resolve(
        "req-aureon",
        "exec-1",
        TradeRequestStatus.FILLED,
        updates={
            "position_id": position_id,
            "comment_token": comment,
            "fill_price": 2400.3,
            "filled_volume": 0.1,
        },
    )


def test_manual_trade_is_observed_but_never_managed(local_store) -> None:
    broker = FakeBroker(bid=2400.0, ask=2400.3)
    manual = _open(broker, magic=1, comment="hand")
    monitor = _monitor(local_store, broker)
    result = monitor.startup()
    assert result.imported_external
    trades = TradeRepository(local_store)
    trade = trades.get_by_position(manual)
    assert trade.source is TradeSource.EXTERNAL_MT5 and trade.management_state is None
    broker.set_quote(bid=2410.0, ask=2410.3)
    monitor.poll_once()
    assert trades.get_by_position(manual).management_state is None
    with pytest.raises(TerminalWriteRejected):
        trades.update_management(trade.trade_id, management_state=object())
    assert "modify_position" not in broker.calls and "close_position" not in broker.calls


def test_aureon_trade_is_managed_and_the_state_survives_a_restart(local_store) -> None:
    broker = FakeBroker(bid=2400.0, ask=2400.3)
    position = _open(broker, magic=MAGIC, comment="AUR-abc", sl=2390.0)
    _request_for(local_store, position, "AUR-abc")
    monitor = _monitor(local_store, broker)
    monitor.startup()
    trades = TradeRepository(local_store)
    trade = trades.get_by_position(position)
    assert trade.source is TradeSource.AUREON
    assert trade.management_state is not None and trade.management_state.initial_stop == 2390.0

    broker.set_quote(bid=2406.0, ask=2406.3)  # +5.7 on the bid: protection activates
    monitor.poll_once()
    state = trades.get_by_position(position).management_state
    assert state.reached_5 and state.protection_activated
    first_stop = state.current_stop
    assert first_stop is not None and first_stop > 2390.0

    broker.set_quote(bid=2412.0, ask=2412.3)
    monitor.poll_once()
    ratcheted = trades.get_by_position(position).management_state.current_stop
    assert ratcheted > first_stop

    # Restart: a new monitor over the same store continues from the ratcheted stop.
    broker.set_quote(bid=2408.0, ask=2408.3)
    restarted = _monitor(local_store, broker)
    restarted.startup()
    after = trades.get_by_position(position).management_state
    assert after.current_stop == ratcheted
    assert after.phase is ManagementPhase.TRAILING
    assert after.trail_updates >= 2
    events = trades.management_events(trade.trade_id)
    assert events and events[0].phase and all(e.source == "exit_manager" for e in events)
    assert "close_position" not in broker.calls  # the monitor itself never touches the broker


def test_exit_experience_is_frozen_when_the_broker_closes_the_position(local_store) -> None:
    broker = FakeBroker(bid=2400.0, ask=2400.3)
    position = _open(broker, magic=MAGIC, comment="AUR-xyz", sl=2390.0)
    _request_for(local_store, position, "AUR-xyz")
    monitor = _monitor(local_store, broker)
    monitor.startup()
    broker.set_quote(bid=2418.0, ask=2418.3)
    monitor.poll_once()
    broker.set_quote(bid=2411.0, ask=2411.3)
    broker.close_position(position)  # a human closes it in the terminal
    monitor.poll_once()
    trade = TradeRepository(local_store).get_by_position(position)
    state = trade.management_state
    assert trade.status.value == "closed"
    assert state.phase is ManagementPhase.CLOSED
    assert state.exit_price == 2411.0 and state.exit_time is not None
    assert state.exit_reason
    assert state.realized_move == pytest.approx(2411.0 - 2400.3)
    assert state.profit_given_back_from_peak == pytest.approx(state.mfe - state.realized_move)
    assert state.reached_10 and not state.reached_20
    events = TradeRepository(local_store).management_events(trade.trade_id)
    assert events[-1].action == "closed" and events[-1].exit_price == 2411.0


def test_autonomous_management_raises_one_control_request_per_decision(local_store) -> None:
    broker = FakeBroker(bid=2400.0, ask=2400.3)
    position = _open(broker, magic=MAGIC, comment="AUR-ctl", sl=2390.0)
    _request_for(local_store, position, "AUR-ctl")
    controls = ControlRequestRepository(local_store)
    monitor = _monitor(local_store, broker, controls=controls, autonomous=True)
    monitor.startup()
    broker.set_quote(bid=2406.0, ask=2406.3)
    monitor.poll_once()
    requested = controls.with_status(ControlRequestStatus.REQUESTED)
    assert len(requested) == 1
    request = requested[0]
    assert request.kind is ControlRequestKind.MODIFY_STOP and request.autonomous
    assert request.stop_loss is not None and request.target == str(position)
    # The same quote again (or a restart replaying it) does not add a second request.
    monitor.poll_once()
    _monitor(local_store, broker, controls=controls, autonomous=True).startup()
    assert len(controls.with_status(ControlRequestStatus.REQUESTED)) == 1

    # The executor performs it and only then does the broker's stop move.
    worker = ControlWorker(
        controls,
        broker,
        executor_id="exec-1",
        trade_lookup=TradeRepository(local_store).get_by_position,
    )
    done = worker.process(request.control_id)
    assert done.status is ControlRequestStatus.COMPLETED
    assert broker.open_positions()[0].sl == pytest.approx(request.stop_loss)


def test_recording_only_mode_never_writes_control_requests(local_store) -> None:
    broker = FakeBroker(bid=2400.0, ask=2400.3)
    position = _open(broker, magic=MAGIC, comment="AUR-rec", sl=2390.0)
    _request_for(local_store, position, "AUR-rec")
    controls = ControlRequestRepository(local_store)
    monitor = _monitor(local_store, broker, controls=controls, autonomous=False)
    monitor.startup()
    broker.set_quote(bid=2406.0, ask=2406.3)
    monitor.poll_once()
    assert controls.with_status(ControlRequestStatus.REQUESTED) == []
    assert (
        TradeRepository(local_store).get_by_position(position).management_state.protection_activated
    )


def test_the_executor_refuses_an_autonomous_action_on_a_manual_position(local_store) -> None:
    broker = FakeBroker(bid=2400.0, ask=2400.3)
    manual = _open(broker, magic=1, comment="hand")
    monitor = _monitor(local_store, broker)
    monitor.startup()
    controls = ControlRequestRepository(local_store)
    controls.create(
        ControlRequest(
            control_id="mgmt-forged",
            kind=ControlRequestKind.CLOSE,
            target=str(manual),
            symbol=SYMBOL,
            requested_by="aureon:exit_manager",
        )
    )
    worker = ControlWorker(
        controls,
        broker,
        executor_id="exec-1",
        trade_lookup=TradeRepository(local_store).get_by_position,
    )
    result = worker.process("mgmt-forged")
    assert result.status is ControlRequestStatus.FAILED
    assert result.failure_code is FailureCode.NOT_AUREON_OWNED
    assert "close_position" not in broker.calls and broker.open_positions()
    # An explicit human close of their own trade is still theirs to make.
    controls.create(
        ControlRequest(
            control_id="ctl-human",
            kind=ControlRequestKind.CLOSE,
            target=str(manual),
            symbol=SYMBOL,
            requested_by="123456",
        )
    )
    assert worker.process("ctl-human").status is ControlRequestStatus.COMPLETED


def _snapshot(**overrides) -> BrokerSnapshot:
    from aureon.data.historical_provider import DEFAULT_SYMBOL_INFO
    from aureon.models.broker import AccountInfo

    base = dict(
        symbol_info=DEFAULT_SYMBOL_INFO,
        quote=QuoteSnapshot(
            symbol=SYMBOL, bid=2400.0, ask=2400.3, point=0.01, captured_at=utc_now()
        ),
        account=AccountInfo(
            login=1, balance=100_000, equity=100_000, margin_free=100_000, mode=AccountMode.DEMO
        ),
        open_positions=0,
        trades_today=0,
    )
    return BrokerSnapshot(**(base | overrides))


def _request() -> TradeRequest:
    from aureon.models.enums import TradeRequestStatus

    now = utc_now()
    return TradeRequest(
        request_id="req-guard",
        symbol=SYMBOL,
        order_type=OrderType.MARKET_BUY,
        volume=0.1,
        requested_by="u",
        status=TradeRequestStatus.CONFIRMED,
        confirmed_at=now,
        confirmed_by="u",
        quote=QuoteSnapshot(symbol=SYMBOL, bid=2400.0, ask=2400.3, point=0.01, captured_at=now),
    )


def test_one_aureon_position_per_symbol_and_manual_trades_do_not_count() -> None:
    settings = ExecutionSettings(trading_enabled=True, max_open_positions=5)
    ok = check(
        _request(),
        settings,
        market_state=MarketState.OPEN,
        broker=_snapshot(open_positions=3, aureon_positions_on_symbol=0),
    )
    assert ok.ok, ok.message  # three manual positions do not block Aureon's one
    blocked = check(
        _request(),
        settings,
        market_state=MarketState.OPEN,
        broker=_snapshot(open_positions=1, aureon_positions_on_symbol=1),
    )
    assert not blocked.ok and blocked.failure_code is FailureCode.MAX_AUREON_POSITIONS
    unknown = check(
        _request(),
        settings,
        market_state=MarketState.OPEN,
        broker=_snapshot(positions_unknown=True),
    )
    assert not unknown.ok and unknown.failure_code is FailureCode.POSITIONS_UNKNOWN
    assert isinstance(ok, GuardResult)
