"""The execution pieces that need no emulator: the fake broker, capability checks,
idempotency, and the fail-closed settings repository.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from aureon.execution.broker_capabilities import (
    check_filling_mode,
    check_pending_entry,
    check_stops,
    check_volume,
)
from aureon.execution.broker_interface import BrokerError, BrokerInterface
from aureon.execution.fake_broker import DEFAULT_SYMBOL_INFO, Behaviour, FakeBroker
from aureon.execution.idempotency import AlreadySent, SendGuard, comment_token
from aureon.models.enums import FailureCode, FillingMode, OrderType
from aureon.models.settings import ExecutionSettings
from aureon.models.trade import BrokerOrderRequest
from aureon.storage.settings_repository import ExecutionSettingsRepository
from tests.conftest import InMemoryFirestore

MAGIC = 770177
TOKEN = "AUR:ABCDEF"


def order(**overrides) -> BrokerOrderRequest:
    base = dict(
        symbol="XAUUSD",
        order_type=OrderType.MARKET_BUY,
        volume=0.10,
        magic=MAGIC,
        comment=TOKEN,
    )
    return BrokerOrderRequest(**(base | overrides))


# ── The interface is read-only where it must be ───────────────────────────────


def test_the_fake_broker_implements_the_whole_interface() -> None:
    """A partial fake would let the executor depend on something a real broker lacks."""
    assert issubclass(FakeBroker, BrokerInterface)
    broker = FakeBroker()
    for name in (
        "account_info",
        "symbol_info",
        "quote",
        "send_market_order",
        "send_pending_order",
        "cancel_order",
        "close_position",
        "open_positions",
        "pending_orders",
        "orders_history",
        "deals_history",
    ):
        assert callable(getattr(broker, name)), name


# ── The crash asymmetry, which is the whole point ─────────────────────────────


def test_crash_after_send_leaves_the_order_at_the_broker() -> None:
    """THE dangerous case. The order exists; the caller never found out."""
    broker = FakeBroker().script(Behaviour(crash_after_send=True))
    with pytest.raises(BrokerError):
        broker.send_market_order(order())
    assert len(broker.executions_for(TOKEN)) == 1
    assert len(broker.open_positions()) == 1


def test_crash_before_result_leaves_nothing() -> None:
    """Indistinguishable from the above to the caller -- only looking tells them apart,
    which is why reconciliation exists and retrying does not."""
    broker = FakeBroker().script(Behaviour(crash_before_result=True))
    with pytest.raises(BrokerError):
        broker.send_market_order(order())
    assert broker.executions_for(TOKEN) == []
    assert broker.open_positions() == []


def test_a_rejection_is_an_answer_not_an_error() -> None:
    broker = FakeBroker().script(Behaviour(reject=FailureCode.INSUFFICIENT_MARGIN))
    result = broker.send_market_order(order())
    assert result.ok is False
    assert result.failure_code is FailureCode.INSUFFICIENT_MARGIN
    assert broker.executions_for(TOKEN) == []


def test_the_ledger_survives_a_simulated_restart() -> None:
    """A test that reset the ledger on restart would pass every crash scenario
    vacuously."""
    broker = FakeBroker()
    broker.send_market_order(order())
    assert len(broker.ledger) == 1
    # "Restart" = a new executor around the same broker. Nothing here resets.
    assert len(broker.executions_for(TOKEN)) == 1


def test_behaviours_are_consumed_one_per_send() -> None:
    broker = FakeBroker().script(
        Behaviour(reject=FailureCode.REQUOTE), Behaviour()
    )
    assert broker.send_market_order(order()).ok is False
    assert broker.send_market_order(order()).ok is True


def test_a_partial_fill_reports_the_filled_volume() -> None:
    broker = FakeBroker().script(Behaviour(partial_fill_volume=0.04))
    result = broker.send_market_order(order(volume=0.10))
    assert result.filled_volume == pytest.approx(0.04)


def test_closing_twice_is_refused_and_records_one_exit() -> None:
    broker = FakeBroker()
    result = broker.send_market_order(order())
    assert broker.close_position(result.position_id).ok is True
    assert broker.close_position(result.position_id).ok is False
    assert len([e for e in broker.ledger if e.kind == "close"]) == 1


def test_cancelling_a_filled_order_is_refused() -> None:
    broker = FakeBroker()
    result = broker.send_pending_order(
        order(order_type=OrderType.BUY_STOP, price=2405.0)
    )
    broker.fill_pending(result.order_ticket)
    assert broker.cancel_order(result.order_ticket).ok is False


def test_deals_record_both_sides_of_a_round_trip() -> None:
    from datetime import datetime

    broker = FakeBroker()
    result = broker.send_market_order(order())
    broker.close_position(result.position_id)
    deals = broker.deals_history(
        datetime(2020, 1, 1, tzinfo=UTC), datetime(2040, 1, 1, tzinfo=UTC)
    )
    assert [d.entry.value for d in deals] == ["in", "out"]


# ── Capability checks (§39, §42) ──────────────────────────────────────────────


def test_an_off_grid_volume_is_rejected_never_rounded() -> None:
    """Rounding down trades a size the human did not ask for; rounding up risks more of
    their money than they authorised."""
    assert check_volume(DEFAULT_SYMBOL_INFO, 0.30).ok is True
    for bad in (0.015, 0.005, 999.0, 0.0, -1.0):
        check = check_volume(DEFAULT_SYMBOL_INFO, bad)
        assert check.ok is False
        assert check.failure_code is FailureCode.VOLUME_INVALID


def test_an_unreported_filling_mode_list_does_not_block() -> None:
    """Failing closed here would block every trade on a symbol whose metadata is merely
    incomplete; the broker becomes the arbiter instead."""
    bare = DEFAULT_SYMBOL_INFO.model_copy(update={"filling_modes": ()})
    assert check_filling_mode(bare, FillingMode.FOK).ok is True
    assert check_filling_mode(DEFAULT_SYMBOL_INFO, None).ok is True


def test_stop_side_and_distance_are_distinct_failures() -> None:
    """They mean different things to whoever reads the embed."""
    wrong_side = check_stops(
        DEFAULT_SYMBOL_INFO,
        order_type=OrderType.MARKET_BUY,
        price=None,
        sl=2405.0,
        tp=None,
        reference_price=2400.0,
    )
    too_close = check_stops(
        DEFAULT_SYMBOL_INFO,
        order_type=OrderType.MARKET_BUY,
        price=None,
        sl=2399.9,
        tp=None,
        reference_price=2400.0,
    )
    assert wrong_side.failure_code is FailureCode.INVALID_STOPS
    assert too_close.failure_code is FailureCode.STOPS_TOO_CLOSE


@pytest.mark.parametrize(
    "order_type,price,ok",
    [
        (OrderType.BUY_STOP, 2405.0, True),
        (OrderType.BUY_STOP, 2395.0, False),
        (OrderType.SELL_STOP, 2395.0, True),
        (OrderType.SELL_STOP, 2405.0, False),
        (OrderType.BUY_LIMIT, 2395.0, True),
        (OrderType.BUY_LIMIT, 2405.0, False),
        (OrderType.SELL_LIMIT, 2405.0, True),
        (OrderType.SELL_LIMIT, 2395.0, False),
    ],
)
def test_pending_entries_must_sit_on_the_right_side(
    order_type: OrderType, price: float, ok: bool
) -> None:
    check = check_pending_entry(
        DEFAULT_SYMBOL_INFO, order_type=order_type, price=price, reference_price=2400.0
    )
    assert check.ok is ok


# ── In-process idempotency (§32) ──────────────────────────────────────────────


def test_one_process_cannot_send_the_same_request_twice() -> None:
    """Firestore's claim stops two PROCESSES; this stops one process retrying."""
    guard = SendGuard()
    attempt = guard.begin("r1")
    assert attempt
    with pytest.raises(AlreadySent, match="never re-sent"):
        guard.begin("r1")


def test_the_attempt_is_registered_before_the_send() -> None:
    """Recording it afterwards would let a crash mid-send be retried."""
    guard = SendGuard()
    guard.begin("r1")
    assert guard.has_sent("r1")
    assert guard.attempt_for("r1")


def test_different_requests_are_independent() -> None:
    guard = SendGuard()
    guard.begin("r1")
    guard.begin("r2")
    assert len(guard) == 2


def test_comment_tokens_are_stable_and_distinct() -> None:
    assert comment_token("r1") == comment_token("r1")
    assert comment_token("r1") != comment_token("r2")
    assert len(comment_token("r1")) == 10


# ── Settings fail closed (§57, decision 11) ───────────────────────────────────


def test_missing_settings_mean_trading_is_off() -> None:
    """An absent document must not become an open trading bot."""
    repository = ExecutionSettingsRepository(InMemoryFirestore())
    assert repository.read() is None
    assert repository.read_or_default().trading_enabled is False


def test_unreadable_settings_fail_closed() -> None:
    class Broken(InMemoryFirestore):
        def document(self, path: str):
            raise ConnectionError("firestore down")

    assert ExecutionSettingsRepository(Broken()).read_or_default().trading_enabled is False


def test_settings_round_trip() -> None:
    store = InMemoryFirestore()
    repository = ExecutionSettingsRepository(store)
    repository.write(ExecutionSettings(trading_enabled=True, max_lot=2.5))
    loaded = repository.read()
    assert loaded is not None
    assert loaded.trading_enabled is True
    assert loaded.max_lot == 2.5


def test_the_trading_switch_is_now_transactional() -> None:
    """§57, §60: the switch and its audit row commit together, with a version check.

    This test moved to the emulator suite when ``set_trading_enabled`` became
    transactional. What it used to assert -- that both writes happen -- can no longer be
    checked against the in-memory double: the double applies writes immediately and has
    no isolation, so it would pass whether or not the two writes were atomic. Asserting
    atomicity against a fake that defines its own semantics proves nothing.

    The real tests live in ``tests/failure_injection/test_kill_switch.py``: two
    concurrent toggles resolve to exactly one winner, and each successful write leaves
    exactly one audit row. What remains here is the shape of the API.
    """
    import inspect

    from aureon.storage.settings_repository import ExecutionSettingsRepository

    signature = inspect.signature(ExecutionSettingsRepository.set_trading_enabled)
    assert "if_version" in signature.parameters, (
        "the switch must accept a version to assert against"
    )
    source = inspect.getsource(ExecutionSettingsRepository.set_trading_enabled)
    assert "transaction.set" in source, "the switch must write inside a transaction"
    assert source.count("transaction.set") == 2, (
        "the settings document AND its audit row must both be written in the "
        "transaction; one of them is outside it"
    )
