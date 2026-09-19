"""Discord's Firestore flows, end to end against the emulator (§37, §46, §47, §57).

The rules live in ``service.py`` and are unit-tested there. These check what happens when
those decisions actually reach Firestore and are acted on by the executor: a confirmation
that becomes a real execution, a control request that the executor performs, and the
trading switch taking effect on the very next request.
"""

from __future__ import annotations

import pytest

from aureon.discord.service import (
    DraftRequest,
    build_control_request,
    check_confirm_press,
)
from aureon.execution.control_worker import ControlWorker
from aureon.execution.fake_broker import FakeBroker
from aureon.models.enums import (
    ControlRequestKind,
    ControlRequestStatus,
    FailureCode,
    OrderType,
    TradeRequestStatus,
)
from aureon.models.identity import comment_token
from aureon.models.market import QuoteSnapshot
from aureon.models.trade import TradeRequest
from aureon.storage.control_request_repository import (
    ControlClaimRejected,
    ControlRequestRepository,
)
from aureon.storage.settings_repository import ExecutionSettingsRepository
from aureon.storage.symbol_repository import SymbolRepository
from aureon.storage.trade_request_repository import (
    ConfirmationRejected,
    TradeRequestRepository,
)
from tests.failure_injection.conftest import MAGIC, SYMBOL, USER

pytestmark = pytest.mark.emulator


@pytest.fixture
def controls(firestore_client) -> ControlRequestRepository:
    return ControlRequestRepository(firestore_client)


@pytest.fixture
def settings_repo(firestore_client) -> ExecutionSettingsRepository:
    return ExecutionSettingsRepository(firestore_client)


def drafted(requested_by: str = USER) -> TradeRequest:
    return DraftRequest(
        symbol=SYMBOL,
        order_type=OrderType.MARKET_BUY,
        volume=0.10,
        requested_by=requested_by,
    ).to_request()


def fresh_quote() -> QuoteSnapshot:
    return QuoteSnapshot(symbol=SYMBOL, bid=2400.00, ask=2400.30, point=0.01)


# ── The confirm flow (§27, §37) ───────────────────────────────────────────────


def test_a_discord_draft_becomes_an_executed_trade(
    repository: TradeRequestRepository, broker: FakeBroker, make_worker
) -> None:
    """The whole path: Discord writes REQUESTED, the human confirms, the executor acts."""
    request = repository.create(drafted())
    assert request.status is TradeRequestStatus.REQUESTED

    gate = check_confirm_press(request, USER, fresh_quote(), _settings())
    assert gate.ok is True
    repository.confirm(request.request_id, USER, fresh_quote())

    make_worker(broker, executor_id="exec-1").process(request.request_id)
    assert repository.get(request.request_id).status is TradeRequestStatus.FILLED
    assert len(broker.executions_for(comment_token(request.request_id))) == 1


def test_the_wrong_user_cannot_confirm_even_via_the_repository(
    repository: TradeRequestRepository, broker: FakeBroker, make_worker
) -> None:
    """§27, enforced in Firestore as well as in the view.

    The button check is a courtesy to the human; this is the guarantee. A bug in the view
    must not be able to authorise someone else's money.
    """
    request = repository.create(drafted())
    with pytest.raises(ConfirmationRejected, match="did not request"):
        repository.confirm(request.request_id, "someone-else", fresh_quote())

    assert repository.get(request.request_id).status is TradeRequestStatus.REQUESTED
    assert make_worker(broker, executor_id="exec-1").process(request.request_id) is None
    assert broker.executions_for(comment_token(request.request_id)) == []


def test_a_double_confirm_produces_one_execution(
    repository: TradeRequestRepository, broker: FakeBroker, make_worker
) -> None:
    """A duplicated button click must not become two trades."""
    request = repository.create(drafted())
    for _ in range(3):
        repository.confirm(request.request_id, USER, fresh_quote())

    stored = repository.get(request.request_id)
    assert stored.confirmation_version == 1

    make_worker(broker, executor_id="exec-1").process(request.request_id)
    assert len(broker.executions_for(comment_token(request.request_id))) == 1


def test_a_cancelled_draft_never_executes(
    repository: TradeRequestRepository, broker: FakeBroker, make_worker
) -> None:
    """The CANCEL button's path, via the repository."""
    request = repository.create(drafted())
    repository.resolve(
        request.request_id,
        "discord",
        TradeRequestStatus.CANCELLED,
        failure_message="cancelled in Discord before confirmation",
        reconciliation=True,
    )
    assert make_worker(broker, executor_id="exec-1").process(request.request_id) is None
    assert broker.executions_for(comment_token(request.request_id)) == []


# ── Control requests (§46, §47) ───────────────────────────────────────────────


def test_a_close_request_is_performed_by_the_executor(
    repository: TradeRequestRepository,
    controls: ControlRequestRepository,
    broker: FakeBroker,
    make_worker,
) -> None:
    """Discord writes it; the executor -- the only process with a broker -- performs it."""
    request = repository.create(drafted())
    repository.confirm(request.request_id, USER, fresh_quote())
    make_worker(broker, executor_id="exec-1").process(request.request_id)
    position_id = repository.get(request.request_id).position_id

    control = controls.create(
        build_control_request(ControlRequestKind.CLOSE, str(position_id), USER)
    )
    assert control.status is ControlRequestStatus.REQUESTED

    worker = ControlWorker(controls, broker, executor_id="exec-1")
    assert worker.poll_once() == 1

    assert controls.get(control.control_id).status is ControlRequestStatus.COMPLETED
    assert broker.open_positions() == []


def test_a_cancel_request_is_performed(
    repository: TradeRequestRepository,
    controls: ControlRequestRepository,
    broker: FakeBroker,
    make_worker,
) -> None:
    request = repository.create(
        DraftRequest(
            symbol=SYMBOL,
            order_type=OrderType.BUY_STOP,
            volume=0.10,
            requested_by=USER,
            price=2405.00,
        ).to_request()
    )
    repository.confirm(request.request_id, USER, fresh_quote())
    make_worker(broker, executor_id="exec-1").process(request.request_id)
    ticket = repository.get(request.request_id).order_ticket

    control = controls.create(
        build_control_request(ControlRequestKind.CANCEL, str(ticket), USER)
    )
    ControlWorker(controls, broker, executor_id="exec-1").poll_once()

    assert controls.get(control.control_id).status is ControlRequestStatus.COMPLETED
    assert broker.pending_orders() == []


def test_cancelling_an_order_that_already_filled_fails_rather_than_lying(
    repository: TradeRequestRepository,
    controls: ControlRequestRepository,
    broker: FakeBroker,
    make_worker,
) -> None:
    """§46. The market won the race.

    Reporting success here is how a human comes to believe an order is gone when it is
    actually a live position.
    """
    request = repository.create(
        DraftRequest(
            symbol=SYMBOL,
            order_type=OrderType.BUY_STOP,
            volume=0.10,
            requested_by=USER,
            price=2405.00,
        ).to_request()
    )
    repository.confirm(request.request_id, USER, fresh_quote())
    make_worker(broker, executor_id="exec-1").process(request.request_id)
    ticket = repository.get(request.request_id).order_ticket

    broker.fill_pending(ticket)  # it fills before the cancel lands

    control = controls.create(
        build_control_request(ControlRequestKind.CANCEL, str(ticket), USER)
    )
    ControlWorker(controls, broker, executor_id="exec-1").poll_once()

    resolved = controls.get(control.control_id)
    assert resolved.status is ControlRequestStatus.FAILED
    assert resolved.failure_code is FailureCode.BROKER_REJECTED
    # And the position really is live -- the human must not be told otherwise.
    assert len(broker.open_positions()) == 1


def test_two_executors_perform_a_control_request_once(
    controls: ControlRequestRepository, broker: FakeBroker, repository, make_worker
) -> None:
    """The same claim/lease discipline as a trade: closing twice could reverse a position."""
    request = repository.create(drafted())
    repository.confirm(request.request_id, USER, fresh_quote())
    make_worker(broker, executor_id="exec-1").process(request.request_id)
    position_id = repository.get(request.request_id).position_id

    control = controls.create(
        build_control_request(ControlRequestKind.CLOSE, str(position_id), USER)
    )
    first = ControlWorker(controls, broker, executor_id="exec-a")
    second = ControlWorker(controls, broker, executor_id="exec-b")

    results = [first.process(control.control_id), second.process(control.control_id)]
    assert len([r for r in results if r is not None]) == 1
    assert len([e for e in broker.ledger if e.kind == "close"]) == 1


def test_a_claimed_control_request_cannot_be_claimed_again(
    controls: ControlRequestRepository
) -> None:
    control = controls.create(
        build_control_request(ControlRequestKind.CLOSE, "555", USER)
    )
    controls.claim(control.control_id, "exec-a")
    with pytest.raises(ControlClaimRejected):
        controls.claim(control.control_id, "exec-b")


def test_control_requests_are_audited(
    firestore_client, controls: ControlRequestRepository
) -> None:
    """§60."""
    from aureon.storage import paths

    control = controls.create(
        build_control_request(ControlRequestKind.CLOSE, "555", USER, volume=0.05)
    )
    controls.claim(control.control_id, "exec-a")
    controls.resolve(control.control_id, "exec-a", ControlRequestStatus.COMPLETED)

    actions = sorted(
        doc.to_dict()["action"]
        for doc in firestore_client.collection(paths.AUDIT_LOGS).stream()
        if doc.to_dict().get("document_id") == control.control_id
    )
    assert actions == [
        "control_request.claim",
        "control_request.close",
        "control_request.resolve",
    ]


# ── §57: the trading switch ───────────────────────────────────────────────────


def test_disabling_trading_blocks_the_very_next_request(
    repository: TradeRequestRepository,
    settings_repo: ExecutionSettingsRepository,
    broker: FakeBroker,
    make_worker,
) -> None:
    """§57, end to end: Discord writes the flag, the guard reads it, nothing is sent."""
    settings_repo.set_trading_enabled(False, actor=USER, reason="incident")

    request = repository.create(drafted())
    repository.confirm(request.request_id, USER, fresh_quote())

    from aureon.execution.execution_worker import ExecutionWorker

    worker = ExecutionWorker(
        repository,
        broker,
        magic=MAGIC,
        # The real provider, so the Firestore flag is what decides.
        settings_provider=settings_repo.read_or_default,
        executor_id="exec-1",
    )
    worker.process(request.request_id)

    failed = repository.get(request.request_id)
    assert failed.status is TradeRequestStatus.FAILED
    assert failed.failure_code is FailureCode.TRADING_DISABLED
    assert broker.executions_for(comment_token(request.request_id)) == []
    assert "send_market_order" not in broker.calls


def test_enabling_trading_lets_the_next_request_through(
    repository: TradeRequestRepository,
    settings_repo: ExecutionSettingsRepository,
    broker: FakeBroker,
) -> None:
    settings_repo.set_trading_enabled(False, actor=USER)
    settings_repo.set_trading_enabled(True, actor=USER)

    request = repository.create(drafted())
    repository.confirm(request.request_id, USER, fresh_quote())

    from aureon.execution.execution_worker import ExecutionWorker

    ExecutionWorker(
        repository,
        broker,
        magic=MAGIC,
        settings_provider=settings_repo.read_or_default,
        executor_id="exec-1",
    ).process(request.request_id)

    assert repository.get(request.request_id).status is TradeRequestStatus.FILLED


def test_an_absent_settings_document_blocks_trading(
    repository: TradeRequestRepository,
    settings_repo: ExecutionSettingsRepository,
    broker: FakeBroker,
) -> None:
    """decision 66: fail closed. An absent document must not become an open bot."""
    assert settings_repo.read() is None

    request = repository.create(drafted())
    repository.confirm(request.request_id, USER, fresh_quote())

    from aureon.execution.execution_worker import ExecutionWorker

    ExecutionWorker(
        repository,
        broker,
        magic=MAGIC,
        settings_provider=settings_repo.read_or_default,
        executor_id="exec-1",
    ).process(request.request_id)

    failed = repository.get(request.request_id)
    assert failed.failure_code is FailureCode.TRADING_DISABLED


# ── Published symbol specs (decision 79) ──────────────────────────────────────


def test_symbol_specs_round_trip_for_discord(firestore_client) -> None:
    """Discord reads metadata from Firestore because it may not call the broker."""
    from aureon.execution.fake_broker import DEFAULT_SYMBOL_INFO

    repository = SymbolRepository(firestore_client)
    repository.publish(DEFAULT_SYMBOL_INFO)

    loaded = repository.get(DEFAULT_SYMBOL_INFO.symbol)
    assert loaded is not None
    assert loaded.volume_step == pytest.approx(DEFAULT_SYMBOL_INFO.volume_step)
    assert loaded.filling_modes == DEFAULT_SYMBOL_INFO.filling_modes
    # The copy says when it was written, so a reader can judge staleness.
    assert repository.published_at(DEFAULT_SYMBOL_INFO.symbol) is not None


def test_a_missing_symbol_spec_reads_as_none(firestore_client) -> None:
    assert SymbolRepository(firestore_client).get("NOPE") is None


def _settings():
    from aureon.models.settings import ExecutionSettings

    return ExecutionSettings(
        trading_enabled=True, quote_ttl_seconds=300.0, confirmation_ttl_seconds=300.0
    )
