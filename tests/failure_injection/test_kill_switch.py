"""The kill switch, terminal immutability, and control-request leases (§57, §58, §60).

All three are about the same failure mode: a write that should not have been allowed to
land. None of them can be proven against a double, because a double defines its own
concurrency and would get to decide the answer. So they live here, against the emulator's
real read-then-conditional-write.

* ``/trading disable`` is the last line between a bug and a funded account. Two
  concurrent toggles must resolve to one winner, and the switch must never flip without
  its audit row.
* A terminal request or trade is history. Its status was already protected; its other
  fields were not.
* A control request must only be resolved by the executor that still holds its lease.
"""

from __future__ import annotations

import threading

import pytest

from aureon.execution.fake_broker import FakeBroker
from aureon.models.base import utc_now
from aureon.models.enums import (
    ControlRequestKind,
    ControlRequestStatus,
    FailureCode,
    TradeRequestStatus,
    TradeStatus,
)
from aureon.models.settings import ExecutionSettings
from aureon.storage import paths
from aureon.storage.control_request_repository import (
    ControlLeaseLost,
    ControlRequestRepository,
)
from aureon.storage.settings_repository import (
    ExecutionSettingsRepository,
    SettingsVersionConflict,
)
from aureon.storage.trade_request_repository import (
    TerminalWriteRejected,
    TradeRequestRepository,
)
from tests.failure_injection.conftest import SYMBOL, USER

pytestmark = pytest.mark.emulator


@pytest.fixture
def settings_repo(firestore_client) -> ExecutionSettingsRepository:
    return ExecutionSettingsRepository(firestore_client)


def audit_rows(firestore_client, action_prefix: str) -> list[dict]:
    return [
        doc.to_dict()
        for doc in firestore_client.collection(paths.AUDIT_LOGS).stream()
        if (doc.to_dict() or {}).get("action", "").startswith(action_prefix)
    ]


# ── 6a. The kill switch ───────────────────────────────────────────────────────


def test_the_switch_and_its_audit_row_commit_together(
    settings_repo, firestore_client
) -> None:
    """§60: the switch must never flip without a record, nor record a flip that failed."""
    settings_repo.set_trading_enabled(True, actor=USER)
    settings_repo.set_trading_enabled(False, actor=USER, reason="drawdown")

    settings = settings_repo.read()
    assert settings is not None
    assert settings.trading_enabled is False
    assert settings.disabled_reason == "drawdown"
    assert settings.settings_version == 2

    actions = sorted(row["action"] for row in audit_rows(firestore_client, "trading."))
    assert actions == ["trading.disable", "trading.enable"]


def test_two_concurrent_toggles_produce_one_winner_and_one_row_each(
    settings_repo, firestore_client
) -> None:
    """The property the version check exists for.

    Without it this is last-write-wins on the one setting that decides whether real
    money can move -- and the direction that loses might be the disable.
    """
    settings_repo.set_trading_enabled(False, actor="seed")
    start = threading.Barrier(2)
    outcomes: list[object] = []
    lock = threading.Lock()

    def toggle(enabled: bool) -> None:
        start.wait(timeout=10)
        try:
            result = settings_repo.set_trading_enabled(
                enabled, actor=f"racer-{enabled}", if_version=1
            )
        except Exception as exc:  # noqa: BLE001 - the loser is the point
            result = exc
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=toggle, args=(flag,)) for flag in (True, False)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    settled = [o for o in outcomes if isinstance(o, ExecutionSettings)]
    assert len(settled) == 1, (
        f"exactly one toggle must win; got {len(settled)}: {outcomes}"
    )
    assert settled[0].settings_version == 2

    # One audit row per SUCCESSFUL write. The loser must leave nothing behind.
    rows = audit_rows(firestore_client, "trading.")
    versions = [row["audit_id"] for row in rows]
    assert len(rows) == len(set(versions)), "an audit row was duplicated"
    assert len([r for r in rows if r["actor"].startswith("racer-")]) == 1


def test_a_stale_version_assertion_is_refused(settings_repo) -> None:
    """For a UI that read the settings, rendered a button, and got a press later."""
    settings_repo.set_trading_enabled(True, actor=USER)  # -> version 1
    settings_repo.set_trading_enabled(False, actor=USER)  # -> version 2

    with pytest.raises(SettingsVersionConflict, match="version 2"):
        settings_repo.set_trading_enabled(True, actor=USER, if_version=1)

    assert settings_repo.read().trading_enabled is False, "the refused write landed"


def test_a_missing_settings_document_reads_as_disabled(settings_repo) -> None:
    """An absent document must not read as "trading was already on"."""
    assert settings_repo.read() is None
    updated = settings_repo.set_trading_enabled(False, actor=USER, reason="first boot")
    assert updated.trading_enabled is False
    assert updated.settings_version == 1


def test_the_audit_id_is_deterministic_per_version(settings_repo, firestore_client) -> None:
    """A retried transaction must not leave two rows for one human decision."""
    settings_repo.set_trading_enabled(True, actor=USER)
    rows = audit_rows(firestore_client, "trading.")
    assert [row["audit_id"] for row in rows] == ["settings-execution-v00000001"]


# ── 6b. Terminal immutability ─────────────────────────────────────────────────


TERMINAL_REQUEST_CASES = [
    TradeRequestStatus.FILLED,
    TradeRequestStatus.CANCELLED,
    TradeRequestStatus.EXPIRED,
    TradeRequestStatus.FAILED,
    TradeRequestStatus.FAILED_STALE,
    TradeRequestStatus.FAILED_RECONCILIATION,
]


def drive_to(repository: TradeRequestRepository, confirmed, status) -> str:
    """Get a request into a terminal status by a legal route."""
    request = confirmed()
    request_id = request.request_id
    if status is TradeRequestStatus.FAILED_STALE:
        repository.resolve(
            request_id, "e1", status, reconciliation=True, failure_code=FailureCode.UNKNOWN
        )
        return request_id
    if status in {TradeRequestStatus.CANCELLED}:
        repository.resolve(request_id, "e1", status, reconciliation=True)
        return request_id
    repository.claim(request_id, "e1")
    if status is TradeRequestStatus.EXPIRED:
        repository.resolve(request_id, "e1", TradeRequestStatus.PENDING)
        repository.resolve(request_id, "e1", status)
        return request_id
    repository.resolve(
        request_id,
        "e1",
        status,
        failure_code=FailureCode.UNKNOWN if "fail" in status.value else None,
    )
    return request_id


@pytest.mark.parametrize("status", TERMINAL_REQUEST_CASES, ids=lambda s: s.value)
def test_a_terminal_request_refuses_a_field_write(
    repository: TradeRequestRepository, confirmed, status
) -> None:
    """Every terminal state, because one unprotected state is the one that gets used."""
    request_id = drive_to(repository, confirmed, status)
    stored = repository.get(request_id)
    assert stored is not None and stored.status is status

    with pytest.raises(TerminalWriteRejected, match=status.value):
        repository.resolve(
            request_id,
            "e1",
            status,  # same status: the gap the transition table cannot see
            updates={"volume": 99.0},
            reconciliation=True,
        )

    assert repository.get(request_id).volume == stored.volume


def test_a_terminal_request_still_accepts_a_reconciliation_stamp(
    repository: TradeRequestRepository, confirmed
) -> None:
    """The one write that changes no claim about what happened."""
    request_id = drive_to(repository, confirmed, TradeRequestStatus.FILLED)
    moment = utc_now()

    updated = repository.resolve(
        request_id,
        "e1",
        TradeRequestStatus.FILLED,
        updates={"last_reconciled_at": moment},
        reconciliation=True,
    )
    assert updated.last_reconciled_at == moment
    assert updated.status is TradeRequestStatus.FILLED


def test_a_closed_trade_refuses_a_pnl_rewrite(firestore_client) -> None:
    """The specific hazard: a settled P&L silently changing after the fact.

    Every review, every classification and every figure a human might act on is built on
    ``realized_pnl``. A path that can rewrite it after the trade closed makes all of
    them unfalsifiable.
    """
    from aureon.models.base import MarketTime
    from aureon.models.enums import Direction, TradeSource
    from aureon.models.trade import Trade
    from aureon.storage.trade_repository import TradeRepository, trade_id_for

    trades = TradeRepository(firestore_client, account_scope="primary")
    now = utc_now()
    trade_id = trade_id_for(4242, account_scope="primary")
    trades.upsert_open(
        Trade(
            trade_id=trade_id,
            mt5_position_id=4242,
            source=TradeSource.AUREON,
            symbol=SYMBOL,
            direction=Direction.BUY,
            volume=0.10,
            open_price=2400.0,
            open_time=MarketTime.from_utc(now, "Europe/Athens"),
            status=TradeStatus.OPEN,
        )
    )
    trades.transition(
        trade_id,
        TradeStatus.CLOSED,
        updates={
            "close_time": MarketTime.from_utc(now, "Europe/Athens"),
            "closed_volume": 0.10,
            "realized_pnl": 120.0,
        },
    )

    with pytest.raises(TerminalWriteRejected, match="closed"):
        trades.transition(trade_id, TradeStatus.CLOSED, updates={"realized_pnl": 999.0})

    assert trades.get(trade_id).realized_pnl == pytest.approx(120.0)


def test_a_closed_trade_still_accepts_a_sync_stamp(firestore_client) -> None:
    from aureon.models.base import MarketTime
    from aureon.models.enums import Direction, TradeSource
    from aureon.models.trade import Trade
    from aureon.storage.trade_repository import TradeRepository, trade_id_for

    trades = TradeRepository(firestore_client, account_scope="primary")
    now = utc_now()
    trade_id = trade_id_for(4343, account_scope="primary")
    trades.upsert_open(
        Trade(
            trade_id=trade_id,
            mt5_position_id=4343,
            source=TradeSource.AUREON,
            symbol=SYMBOL,
            direction=Direction.BUY,
            volume=0.10,
            open_price=2400.0,
            open_time=MarketTime.from_utc(now, "Europe/Athens"),
            status=TradeStatus.OPEN,
        )
    )
    trades.transition(
        trade_id,
        TradeStatus.CLOSED,
        updates={
            "close_time": MarketTime.from_utc(now, "Europe/Athens"),
            "closed_volume": 0.10,
        },
    )

    updated = trades.transition(
        trade_id, TradeStatus.CLOSED, updates={"last_synced_at": now}
    )
    assert updated.last_synced_at == now


def test_a_non_terminal_same_status_write_still_works(
    repository: TradeRequestRepository, confirmed
) -> None:
    """The load-bearing case the terminal guard must not break.

    EXECUTING -> EXECUTING is how the executor stamps ``comment_token`` before sending,
    which is the only thing that makes a crashed send findable. Breaking it would trade
    one silent failure for a much worse one.
    """
    request = confirmed()
    repository.claim(request.request_id, "e1")
    updated = repository.resolve(
        request.request_id,
        "e1",
        TradeRequestStatus.EXECUTING,
        updates={"comment_token": "AUR:ABCDEF"},
    )
    assert updated.comment_token == "AUR:ABCDEF"
    assert updated.status is TradeRequestStatus.EXECUTING


# ── 6d. Control-request leases ────────────────────────────────────────────────


@pytest.fixture
def controls(firestore_client) -> ControlRequestRepository:
    return ControlRequestRepository(firestore_client)


def a_cancel(controls: ControlRequestRepository) -> str:
    from aureon.models.control import ControlRequest

    request = ControlRequest(
        control_id=f"ctl-{utc_now().timestamp()}",
        kind=ControlRequestKind.CANCEL,
        target="12345",
        symbol=SYMBOL,
        requested_by=USER,
    )
    controls.create(request)
    return request.control_id


def test_an_executor_without_the_lease_cannot_resolve(controls) -> None:
    """The asymmetry closed: trade requests always checked this, controls did not."""
    control_id = a_cancel(controls)
    controls.claim(control_id, "executor-a", lease_seconds=60.0)

    with pytest.raises(ControlLeaseLost, match="executor-a"):
        controls.resolve(control_id, "executor-b", ControlRequestStatus.COMPLETED)

    assert controls.get(control_id).status is ControlRequestStatus.EXECUTING


def test_the_holder_can_resolve(controls) -> None:
    control_id = a_cancel(controls)
    controls.claim(control_id, "executor-a", lease_seconds=60.0)
    resolved = controls.resolve(
        control_id, "executor-a", ControlRequestStatus.COMPLETED
    )
    assert resolved.status is ControlRequestStatus.COMPLETED


def test_reconciliation_may_resolve_without_a_lease(controls) -> None:
    """The sanctioned exception: reconciliation repairs what a dead executor left."""
    control_id = a_cancel(controls)
    controls.claim(control_id, "executor-a", lease_seconds=60.0)
    resolved = controls.resolve(
        control_id,
        "nobody",
        ControlRequestStatus.FAILED,
        failure_code=FailureCode.CONNECTION_LOST,
        reconciliation=True,
    )
    assert resolved.status is ControlRequestStatus.FAILED


def test_two_executors_one_cancel_produces_one_broker_cancel(
    controls, firestore_client
) -> None:
    """The scenario the suite was missing (§46).

    Two workers race for one cancel. Exactly one may claim it, and exactly one cancel
    may reach the broker -- a second would be a no-op at best and, for a close, could
    open a position in the opposite direction if the original had already gone.
    """
    from aureon.execution.control_worker import ControlWorker
    from aureon.models.control import ControlRequest
    from aureon.models.enums import OrderType
    from aureon.models.trade import BrokerOrderRequest

    broker = FakeBroker()
    placed = broker.send_pending_order(
        BrokerOrderRequest(
            symbol=SYMBOL,
            order_type=OrderType.BUY_STOP,
            volume=0.10,
            price=2405.0,
            magic=770177,
            comment="AUR:ABCDEF",
        )
    )

    control_id = "ctl-race"
    controls.create(
        ControlRequest(
            control_id=control_id,
            kind=ControlRequestKind.CANCEL,
            target=str(placed.order_ticket),
            symbol=SYMBOL,
            requested_by=USER,
        )
    )

    workers = [
        ControlWorker(controls, broker, executor_id=f"e{i}", lease_seconds=60.0)
        for i in (1, 2)
    ]
    start = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def run(worker: ControlWorker) -> None:
        start.wait(timeout=10)
        try:
            outcome = worker.process(control_id)
        except Exception as exc:  # noqa: BLE001
            outcome = exc
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(w,)) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    cancels = [entry for entry in broker.ledger if entry.kind == "cancel"]
    assert len(cancels) == 1, f"the broker saw {len(cancels)} cancels, not 1"
    assert sum(w.performed + w.refused for w in workers) == 1
    assert controls.get(control_id).status is ControlRequestStatus.COMPLETED
