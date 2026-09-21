"""Whose money is this? (11A, F-3)

The guard this file covers exists because of an asymmetry that is not close. Being wrong in
one direction costs a refused order and a confused minute. Being wrong in the other costs
somebody's savings, placed by a system whose tests have never seen a real fill.

So every case here is written from the refusing side, and the ones that matter most are the
ones where nothing looks wrong: a broker that does not report ``trade_mode`` at all, a
provider that raises, a flag spelled ``1`` instead of ``true``. Each of those is a terminal
that has NOT said it is a demo, and each must be refused.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.execution.execution_guard import BrokerSnapshot, check
from aureon.execution.fake_broker import DEFAULT_SYMBOL_INFO, FakeBroker
from aureon.models.broker import AccountInfo
from aureon.models.enums import (
    AccountMode,
    FailureCode,
    MarketState,
    OrderType,
    TradeRequestStatus,
)
from aureon.models.market import QuoteSnapshot
from aureon.models.settings import ExecutionSettings
from aureon.models.trade import TradeRequest

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
SYMBOL = "XAUUSD"


def settings(**overrides) -> ExecutionSettings:
    base = dict(
        trading_enabled=True,
        max_lot=1.0,
        max_spread_points=500.0,
        max_deviation_points=20,
        quote_ttl_seconds=15.0,
        confirmation_ttl_seconds=300.0,
    )
    return ExecutionSettings(**(base | overrides))


def request() -> TradeRequest:
    return TradeRequest(
        request_id="req-1",
        symbol=SYMBOL,
        order_type=OrderType.MARKET_BUY,
        volume=0.1,
        requested_by="user-1",
        status=TradeRequestStatus.CONFIRMED,
        confirmed_by="user-1",
        confirmed_at=NOW - timedelta(seconds=5),
        deviation_points=10,
    )


def snapshot(account: AccountInfo | None) -> BrokerSnapshot:
    return BrokerSnapshot(
        symbol_info=DEFAULT_SYMBOL_INFO,
        quote=QuoteSnapshot(
            symbol=SYMBOL, bid=2400.00, ask=2400.30, point=0.01, captured_at=NOW
        ),
        account=account,
        open_positions=0,
    )


def account(mode: AccountMode) -> AccountInfo:
    return AccountInfo(
        login=5_000_001,
        balance=100_000.0,
        equity=100_000.0,
        margin_free=100_000.0,
        server="Broker-Server",
        mode=mode,
    )


def verdict(*, mode: AccountMode | None, allow: bool = False):
    return check(
        request(),
        settings(),
        market_state=MarketState.OPEN,
        broker=snapshot(None if mode is None else account(mode)),
        allow_live_execution=allow,
        now=NOW,
    )


# ── What is refused ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", [AccountMode.REAL, AccountMode.UNKNOWN])
def test_real_money_without_the_flag_is_refused(mode: AccountMode) -> None:
    """UNKNOWN sits beside REAL deliberately: a terminal that will not say what it is
    logged into has not said it is a demo."""
    result = verdict(mode=mode)
    assert result.ok is False
    assert result.failure_code is FailureCode.LIVE_EXECUTION_NOT_ALLOWED
    assert result.rule == "live_execution_allowed"
    assert "AUREON_ALLOW_LIVE_EXECUTION" in result.message


def test_a_missing_account_is_refused() -> None:
    """``account_info()`` raised and the executor passed None. Still not a demo."""
    result = verdict(mode=None)
    assert result.failure_code is FailureCode.LIVE_EXECUTION_NOT_ALLOWED


def test_the_refusal_names_the_terminal_so_an_operator_can_check_it() -> None:
    """A message that said only "not allowed" sends somebody to the wrong place. The login
    and server are what tells them which window to look at."""
    message = verdict(mode=AccountMode.REAL).message
    assert "5000001" in message.replace(",", "") or "5_000_001" in message
    assert "Broker-Server" in message
    assert "real" in message


# ── What is permitted ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", [AccountMode.DEMO, AccountMode.CONTEST])
def test_practice_accounts_proceed(mode: AccountMode) -> None:
    assert verdict(mode=mode).ok is True


def test_real_money_with_the_flag_proceeds() -> None:
    """The flag is the whole point: somebody said this was intended, on the box, while
    looking at which terminal was open."""
    assert verdict(mode=AccountMode.REAL, allow=True).ok is True


# ── Ordering ──────────────────────────────────────────────────────────────────


def test_the_account_is_reported_before_the_trading_switch() -> None:
    """Both wrong at once, and the operator hears about the terminal.

    The switch is something they turned off on purpose; the account is something they may
    not have noticed. Reporting the switch first would send them to Discord to flip it,
    which is exactly the wrong next action.
    """
    result = check(
        request(),
        settings(trading_enabled=False),
        market_state=MarketState.OPEN,
        broker=snapshot(account(AccountMode.REAL)),
        allow_live_execution=False,
        now=NOW,
    )
    assert result.failure_code is FailureCode.LIVE_EXECUTION_NOT_ALLOWED


def test_the_flag_defaults_to_refusing_when_a_caller_forgets_it() -> None:
    """``check()`` without the argument at all. A new call site that forgets this must
    refuse a live account, never permit one."""
    result = check(
        request(),
        settings(),
        market_state=MarketState.OPEN,
        broker=snapshot(account(AccountMode.REAL)),
        now=NOW,
    )
    assert result.failure_code is FailureCode.LIVE_EXECUTION_NOT_ALLOWED


# ── The mapping ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("trade_mode", "expected"),
    [
        (0, AccountMode.DEMO),
        (1, AccountMode.CONTEST),
        (2, AccountMode.REAL),
        (3, AccountMode.UNKNOWN),
        (None, AccountMode.UNKNOWN),
        ("nonsense", AccountMode.UNKNOWN),
    ],
)
def test_mt5s_integer_maps_to_a_name(trade_mode: object, expected: AccountMode) -> None:
    """An unrecognised value maps to UNKNOWN rather than falling through to DEMO. A broker
    that starts reporting 3 for something new must not read as a practice account."""
    assert AccountMode.from_trade_mode(trade_mode) is expected


def test_only_the_word_true_unlocks_it() -> None:
    """A flag whose job is to be hard to set by accident should be hard to set by accident.
    ``1``, ``yes`` and a stray ``false`` all leave it locked."""
    from aureon.config import AureonConfig

    base = {"AUREON_SYMBOLS": SYMBOL, "AUREON_EVAL_RULES": f"{SYMBOL}:XAU_OUTCOME_V2"}
    for value, expected in [
        ("true", True),
        ("TRUE", True),
        (" true ", True),
        ("1", False),
        ("yes", False),
        ("on", False),
        ("false", False),
        ("", False),
    ]:
        env = dict(base, AUREON_ALLOW_LIVE_EXECUTION=value)
        assert AureonConfig.from_env(env=env).allow_live_execution is expected, value
    assert AureonConfig.from_env(env=base).allow_live_execution is False


# ── The executor's own startup decision ───────────────────────────────────────


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def __call__(self, name: str, detail: str) -> None:
        self.events.append((name, detail))


def worker_for(broker: FakeBroker, *, allow: bool, ops: Recorder):
    from aureon.execution.execution_worker import ExecutionWorker

    class _NoRepository:
        def list_by_status(self, *_a, **_k):
            return []

    return ExecutionWorker(
        _NoRepository(),  # type: ignore[arg-type]
        broker,
        magic=424242,
        settings_provider=settings,
        allow_live_execution=allow,
        on_ops_event=ops,
    )


def test_a_live_terminal_puts_the_executor_in_reconcile_only() -> None:
    ops = Recorder()
    worker = worker_for(FakeBroker(mode=AccountMode.REAL), allow=False, ops=ops)

    assert worker.announce_account_mode() is AccountMode.REAL
    assert worker.reconcile_only is True
    assert [name for name, _ in ops.events] == ["live_account_detected"]
    assert "execution disabled" in ops.events[0][1]


def test_a_demo_terminal_does_not_announce_anything() -> None:
    ops = Recorder()
    worker = worker_for(FakeBroker(mode=AccountMode.DEMO), allow=False, ops=ops)

    assert worker.announce_account_mode() is AccountMode.DEMO
    assert worker.reconcile_only is False
    assert ops.events == []


def test_a_live_terminal_with_the_flag_still_says_so_out_loud() -> None:
    """Permitted is not the same as unremarkable. The operator gets one message saying
    real money is in play, which is the last chance to notice a flag left set."""
    ops = Recorder()
    worker = worker_for(FakeBroker(mode=AccountMode.REAL), allow=True, ops=ops)

    assert worker.announce_account_mode() is AccountMode.REAL
    assert worker.reconcile_only is False
    assert len(ops.events) == 1
    assert "ALLOWED" in ops.events[0][1]


def test_a_broker_that_raises_is_treated_as_real_money() -> None:
    class _Broken(FakeBroker):
        def account_info(self):
            raise RuntimeError("terminal not responding")

    ops = Recorder()
    worker = worker_for(_Broken(), allow=False, ops=ops)

    assert worker.announce_account_mode() is AccountMode.UNKNOWN
    assert worker.reconcile_only is True


def test_the_fake_broker_claims_demo_explicitly() -> None:
    """``AccountInfo.mode`` defaults to UNKNOWN, which the guard reads as real money — so a
    double that stayed silent would refuse every order in every test. It says DEMO on
    purpose, and a test that wants the guard to bite asks for REAL."""
    assert FakeBroker().account_info().mode is AccountMode.DEMO
    assert AccountInfo(login=1, balance=0.0, equity=0.0).mode is AccountMode.UNKNOWN


# ── /trading enable on a live account (11A F-3) ───────────────────────────────


def state_reporting(mode: AccountMode | None):
    from aureon.models.enums import MarketState, Timeframe
    from aureon.models.system import SymbolState, SystemState

    return SystemState(
        account_mode=mode,
        symbols=(
            SymbolState(
                symbol=SYMBOL, timeframe=Timeframe.M5, market_state=MarketState.OPEN
            ),
        ),
    )


def gate(mode: AccountMode | None, *, allow: bool):
    from aureon.discord.service import plan_trading_enable

    return plan_trading_enable(state_reporting(mode), allow_live_execution=allow)


def test_a_demo_account_enables_with_the_usual_single_confirmation() -> None:
    result = gate(AccountMode.DEMO, allow=False)
    assert result.allowed is True
    assert result.needs_live_confirmation is False
    assert result.fields == ()


def test_a_live_account_without_the_flag_refuses_rather_than_arming_a_dead_switch() -> None:
    """The failure this prevents: Discord saying "trading enabled" over an executor that is
    in reconcile-only mode and will refuse every request. That is the worst of both — a
    human believes they armed it, and nothing will fire."""
    result = gate(AccountMode.REAL, allow=False)
    assert result.allowed is False
    assert "AUREON_ALLOW_LIVE_EXECUTION" in result.message
    assert "reconcile-only" in result.message


def test_a_live_account_with_the_flag_demands_a_second_screen_saying_LIVE() -> None:
    result = gate(AccountMode.REAL, allow=True)
    assert result.allowed is True
    assert result.needs_live_confirmation is True
    assert "LIVE account" in result.message
    assert ("Account", "REAL") in result.fields


def test_an_unpublished_account_mode_is_treated_as_live() -> None:
    """The observer not having said yet is not the same as it having said "demo"."""
    result = gate(None, allow=False)
    assert result.allowed is False


def test_an_unreadable_state_is_treated_as_live() -> None:
    """``system_state`` missing entirely — Discord has no terminal to ask instead."""
    from aureon.discord.service import plan_trading_enable

    assert plan_trading_enable(None, allow_live_execution=False).allowed is False


def test_the_view_only_relabels_and_never_decides() -> None:
    """The gate is enforced before the view exists. A live account without the flag never
    gets a view at all, so this button cannot become the place the rule is forgotten."""
    from aureon.discord.views import EnableTradingView

    plain = EnableTradingView(None, "user-1")  # type: ignore[arg-type]
    live = EnableTradingView(None, "user-1", live=True)  # type: ignore[arg-type]
    assert plain.enable.label == "Enable trading"
    assert live.enable.label == "Enable on LIVE account"
