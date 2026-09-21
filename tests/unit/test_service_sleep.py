"""The other three services at the close (11B).

The observer is the one that stops working; the other three each stop doing something
*different*, and the differences are the design:

* the **executor** never parks its request loop. A closed market is already refused, by
  the guard, on this process's own broker -- and a human who confirms a trade on a Saturday
  is owed that refusal rather than silence until Sunday night. Only the cadence changes.
* the **monitor** does park, because every number it records comes from a quote and a
  closed market has no new ones. What makes that safe is one full reconciliation AT the
  close, which is also the most useful moment for it.
* the **review watcher** does nothing at all except wait for the week to end, and then
  generates the last daily and every symbol's weekly -- on the market's word, not a cron
  line's.

Also here: ``SleepGate``, which all four share, and whose whole job is to never raise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.config import AureonConfig
from aureon.models.enums import MarketState
from aureon.services.market_state_service import WeeklySchedule
from aureon.services.sleep_cycle import SleepCycle, SleepGate, SleepPhase

SYMBOLS = ("XAUUSD", "XAGUSD")

FRIDAY_CLOSE = datetime(2026, 9, 18, 21, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
SUNDAY_OPEN = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)
TUESDAY = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


class Clock:
    """A clock a test moves by hand."""

    def __init__(self, moment: datetime) -> None:
        self.now = moment

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


def gate(
    *,
    states: dict[str, MarketState] | None = None,
    clock: Clock | None = None,
    **hooks,
) -> tuple[SleepGate, dict[str, MarketState], Clock]:
    reading = dict(states or dict.fromkeys(SYMBOLS, MarketState.CLOSED))
    tick = clock or Clock(FRIDAY_CLOSE)
    g = SleepGate(
        cycle=SleepCycle(close_confirm_seconds=0.0),
        states=lambda: reading,
        clock=tick,
        **hooks,
    )
    return g, reading, tick


# ── SleepGate never raises ───────────────────────────────────────────────────


def test_a_states_reader_that_raises_leaves_the_phase_alone() -> None:
    """A service that crashed because it could not work out whether the market was open
    would be strictly worse than one that stayed awake unnecessarily."""

    def explode() -> dict[str, MarketState]:
        raise RuntimeError("the terminal went away")

    g = SleepGate(cycle=SleepCycle(), states=explode, clock=lambda: TUESDAY)
    assert g.tick() is None
    assert not g.parked
    assert g.cycle.phase is SleepPhase.AWAKE


def test_a_clock_that_raises_leaves_the_phase_alone() -> None:
    def explode() -> datetime:
        raise RuntimeError("no clock")

    g = SleepGate(
        cycle=SleepCycle(),
        states=lambda: dict.fromkeys(SYMBOLS, MarketState.CLOSED),
        clock=explode,
    )
    assert g.tick() is None
    assert g.cycle.phase is SleepPhase.AWAKE


def test_a_sleep_hook_that_raises_still_parks_the_service() -> None:
    """The hook is the work a close makes urgent -- a flush, a reconciliation. Failing it
    is worth a log line and not worth staying awake for two days over."""

    def explode(crossing: object) -> None:
        raise RuntimeError("the reconciliation failed")

    g, _, _ = gate(on_sleep=explode)
    crossing = g.tick()
    assert crossing is not None and crossing.slept
    assert g.parked


def test_the_hooks_fire_once_each_and_carry_the_crossing() -> None:
    slept: list[object] = []
    woke: list[object] = []
    g, reading, clock = gate(on_sleep=slept.append, on_wake=woke.append)

    for _ in range(3):
        g.tick()
        clock.advance(minutes=5)
    assert len(slept) == 1
    assert slept[0].next_open == SUNDAY_OPEN

    reading.update(dict.fromkeys(SYMBOLS, MarketState.OPEN))
    for _ in range(3):
        g.tick()
        clock.advance(minutes=5)
    assert len(woke) == 1


def test_the_gate_passes_the_cadences_through() -> None:
    g, _, _ = gate()
    assert g.pace(1.0) == 1.0 and g.heartbeat(5.0) == 5.0
    g.tick()
    assert g.parked
    assert g.pace(1.0) == 60.0
    assert g.heartbeat(5.0) == 300.0


# ── The executor ─────────────────────────────────────────────────────────────


def executor_config(**overrides: str) -> AureonConfig:
    env = {
        "AUREON_ACCOUNT_SCOPE": "primary",
        "AUREON_SYMBOLS": ",".join(SYMBOLS),
        "AUREON_EVAL_RULES": "XAUUSD:XAU_OUTCOME_V2,XAGUSD:XAG_OUTCOME_V1",
        "AUREON_CLOSE_CONFIRM_SECONDS": "0",
        "AUREON_EXECUTOR_POLL_SECONDS": "2",
        "AUREON_SLEEP_POLL_SECONDS": "60",
        "AUREON_SLEEP_HEARTBEAT_SECONDS": "300",
        "AUREON_STATE_HEARTBEAT_SECONDS": "5",
    }
    env.update(overrides)
    return AureonConfig.from_env(env=env)


class FakeHeartbeat:
    """Only the one method the cadence change uses."""

    def __init__(self) -> None:
        self.interval_seconds = 5.0

    def set_interval(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError(seconds)
        self.interval_seconds = seconds


def build_test_executor(states: dict[str, MarketState], heartbeat: FakeHeartbeat):
    from aureon.execution.fake_broker import FakeBroker
    from aureon.models.settings import ExecutionSettings
    from aureon.storage.trade_request_repository import TradeRequestRepository
    from main_executor import Executor
    from tests.conftest import InMemoryFirestore

    return Executor(
        executor_config(),
        FakeBroker(),
        TradeRequestRepository(InMemoryFirestore()),
        settings_provider=lambda: ExecutionSettings(trading_enabled=True),
        market_state_provider=lambda symbol: states[symbol],
        heartbeat=heartbeat,
        schedule=WeeklySchedule(),
    )


def test_the_executors_request_loop_is_never_parked() -> None:
    """The one that would be wrong to copy from the observer.

    A CONFIRMED request arriving on a Saturday must be refused with MARKET_CLOSED, promptly
    and visibly. Parking the loop would leave it sitting in REQUESTED-confirmed limbo until
    Sunday night, which is indistinguishable from a broken executor.
    """
    states = dict.fromkeys(SYMBOLS, MarketState.CLOSED)
    executor = build_test_executor(states, FakeHeartbeat())
    executor.gate.clock = lambda: FRIDAY_CLOSE

    executor.gate.tick()
    assert executor.sleep.phase is SleepPhase.ASLEEP
    assert executor.worker.before_poll == executor.gate.tick
    assert executor.worker.pace == executor.gate.pace
    # The loop consults ``pace`` and nothing else: there is no parked hook to consult.
    assert not hasattr(executor.worker, "parked")


def test_the_executors_poll_slows_at_the_close_and_returns_at_the_open() -> None:
    states = dict.fromkeys(SYMBOLS, MarketState.CLOSED)
    executor = build_test_executor(states, FakeHeartbeat())
    executor.gate.clock = lambda: FRIDAY_CLOSE
    assert executor.worker._wait() == 2.0

    executor.gate.tick()
    assert executor.worker._wait() == 60.0

    states.update(dict.fromkeys(SYMBOLS, MarketState.OPEN))
    executor.gate.tick()
    assert executor.worker._wait() == 2.0


def test_the_executors_heartbeat_slows_and_never_stops() -> None:
    states = dict.fromkeys(SYMBOLS, MarketState.CLOSED)
    beat = FakeHeartbeat()
    executor = build_test_executor(states, beat)
    executor.gate.clock = lambda: FRIDAY_CLOSE

    executor.gate.tick()
    assert beat.interval_seconds == 300.0
    states.update(dict.fromkeys(SYMBOLS, MarketState.OPEN))
    executor.gate.tick()
    assert beat.interval_seconds == 5.0


def test_the_executor_never_sleeps_without_a_market_state_provider() -> None:
    """It cannot tell, and slowing itself down on a guess would delay a refusal a human is
    waiting for."""
    executor = build_test_executor(dict.fromkeys(SYMBOLS, MarketState.CLOSED), FakeHeartbeat())
    executor._market_state_provider = None
    executor.gate.clock = lambda: SATURDAY
    for _ in range(5):
        executor.gate.tick()
    assert executor.sleep.phase is SleepPhase.AWAKE


# ── The monitor ──────────────────────────────────────────────────────────────


class CountingMonitor:
    """Stands in for ``PositionMonitor``, counting what the hooks ask of it."""

    def __init__(self) -> None:
        self.startups = 0
        self.polls = 0
        self.fail = False

    def startup(self, **kwargs: object):
        from aureon.positions.position_monitor import PollResult

        self.startups += 1
        if self.fail:
            raise RuntimeError("the broker refused")
        return PollResult()

    def poll_once(self, **kwargs: object) -> None:
        self.polls += 1


def build_test_monitor(states: dict[str, MarketState], heartbeat: FakeHeartbeat):
    from aureon.execution.fake_broker import FakeBroker
    from aureon.storage.trade_repository import TradeRepository
    from aureon.storage.trade_request_repository import TradeRequestRepository
    from main_monitor import Monitor
    from tests.conftest import InMemoryFirestore

    client = InMemoryFirestore()
    monitor = Monitor(
        executor_config(),
        FakeBroker(),
        TradeRepository(client, account_scope="primary"),
        TradeRequestRepository(client),
        heartbeat=heartbeat,
        market_state_provider=lambda symbol: states[symbol],
        schedule=WeeklySchedule(),
    )
    monitor.monitor = CountingMonitor()  # type: ignore[assignment]
    monitor.gate.clock = lambda: FRIDAY_CLOSE
    return monitor


def test_the_monitor_reconciles_once_at_the_close() -> None:
    """The last chance to compare our books against the broker's while the broker is still
    answering. Anything found after this is found at the open, in the busiest ten minutes
    of the week."""
    states = dict.fromkeys(SYMBOLS, MarketState.CLOSED)
    monitor = build_test_monitor(states, FakeHeartbeat())

    for _ in range(4):
        monitor.gate.tick()
    assert monitor.monitor.startups == 1
    assert monitor.closing_reconciliations == 1


def test_the_reconciliation_is_the_same_one_the_process_runs_at_boot() -> None:
    """A second "closing" variant would be a second definition of what reconciled means,
    and the two would drift."""
    states = dict.fromkeys(SYMBOLS, MarketState.CLOSED)
    monitor = build_test_monitor(states, FakeHeartbeat())
    monitor.gate.tick()
    assert monitor.monitor.startups == 1, "startup(), not a bespoke closing pass"


def test_a_failed_closing_reconciliation_still_parks_the_monitor() -> None:
    states = dict.fromkeys(SYMBOLS, MarketState.CLOSED)
    monitor = build_test_monitor(states, FakeHeartbeat())
    monitor.monitor.fail = True  # type: ignore[attr-defined]
    monitor.gate.tick()
    assert monitor.gate.parked
    assert monitor.closing_reconciliations == 0, "it did not happen, so it is not counted"


def test_the_monitor_wires_its_parked_hook_onto_the_position_monitor() -> None:
    states = dict.fromkeys(SYMBOLS, MarketState.CLOSED)
    from aureon.execution.fake_broker import FakeBroker
    from aureon.storage.trade_repository import TradeRepository
    from aureon.storage.trade_request_repository import TradeRequestRepository
    from main_monitor import Monitor
    from tests.conftest import InMemoryFirestore

    client = InMemoryFirestore()
    monitor = Monitor(
        executor_config(),
        FakeBroker(),
        TradeRepository(client, account_scope="primary"),
        TradeRequestRepository(client),
        market_state_provider=lambda symbol: states[symbol],
        schedule=WeeklySchedule(),
    )
    assert monitor.monitor.parked is not None
    assert monitor.monitor.before_poll == monitor.gate.tick


@pytest.mark.parametrize("parked", [True, False])
def test_a_parked_position_monitor_loop_does_not_poll(parked: bool) -> None:
    """Driven through the real ``run`` loop, not by inspecting a flag.

    Every number the monitor records comes from a quote, and a closed market has no new
    ones: polling would rewrite the same excursions with the same prices for forty-eight
    hours. A monitor that held the right phase and polled anyway would satisfy every
    assertion about its phase, so the loop itself has to be the thing under test.

    No threads and no sleeps: the hook stops the loop on its third call and the cadence is
    zero, so this runs the real body three times and returns.
    """
    from aureon.execution.fake_broker import FakeBroker
    from aureon.positions.position_monitor import PositionMonitor
    from aureon.storage.trade_repository import TradeRepository
    from aureon.storage.trade_request_repository import TradeRequestRepository
    from tests.conftest import InMemoryFirestore

    client = InMemoryFirestore()
    rounds: list[int] = []
    polls: list[int] = []

    def count_and_stop() -> None:
        """The loop's own iteration counter, and the thing that ends it.

        Deliberately NOT the ``parked`` hook: a loop that stopped only when it consulted
        ``parked`` would spin for ever under the very plant this test exists to catch (drop
        the ``if not self.is_parked`` and nothing asks again). ``before_poll`` runs
        unconditionally, so the loop terminates whatever the body does.
        """
        rounds.append(1)
        if len(rounds) >= 3:
            pm.stop()

    pm = PositionMonitor(
        TradeRepository(client, account_scope="primary"),
        TradeRequestRepository(client),
        FakeBroker(),
        magic=1,
        account_scope="primary",
        market_tz="Europe/Athens",
        poll_seconds=0.0,
        before_poll=count_and_stop,
        parked=lambda: parked,
    )
    pm.poll_once = lambda **kwargs: polls.append(1)  # type: ignore[assignment]

    pm.run()
    assert len(rounds) == 3
    assert polls == ([] if parked else [1, 1, 1])


def test_the_monitors_heartbeat_slows_at_the_close() -> None:
    states = dict.fromkeys(SYMBOLS, MarketState.CLOSED)
    beat = FakeHeartbeat()
    monitor = build_test_monitor(states, beat)
    monitor.gate.tick()
    assert beat.interval_seconds == 300.0


# ── The review watcher ───────────────────────────────────────────────────────


class RecordingService:
    """Stands in for ``ReviewService``, recording what it was asked to generate."""

    market_tz = "Europe/Athens"

    def __init__(self, symbol: str, log: list[tuple[str, str, object]]) -> None:
        self.symbol = symbol
        self.log = log

    def generate_daily(self, date: str, *, generated_at: object) -> object:
        self.log.append(("daily", self.symbol, date))
        return object()

    def generate_weekly(self, year: int, week: int, *, generated_at: object) -> object:
        self.log.append(("weekly", self.symbol, (year, week)))
        return object()


def build_watcher(states: dict[str, MarketState], clock: Clock):
    from main_review import ReviewWatcher

    calls: list[tuple[str, str, object]] = []
    watcher = ReviewWatcher(
        executor_config(),
        market_state_provider=lambda symbol: states[symbol],
        schedule=WeeklySchedule(),
        now=clock,
        build=lambda symbol: RecordingService(symbol, calls),
    )
    return watcher, calls


def test_the_weeks_close_generates_a_daily_and_a_weekly_for_every_symbol() -> None:
    """"Both weeklies" is two symbols, each with its own frozen rule: one document covering
    two rules would add reached-counts measured in two instruments' money."""
    states = dict.fromkeys(SYMBOLS, MarketState.CLOSED)
    watcher, calls = build_watcher(states, Clock(FRIDAY_CLOSE + timedelta(minutes=1)))

    watcher.poll_once()
    kinds = [(kind, symbol) for kind, symbol, _ in calls]
    assert kinds == [
        ("daily", "XAUUSD"),
        ("weekly", "XAUUSD"),
        ("daily", "XAGUSD"),
        ("weekly", "XAGUSD"),
    ]


def test_a_midweek_closure_generates_nothing() -> None:
    """A holiday closure on a Tuesday sleeps the services. A "weekly review" of three days
    would be published with the same field names as a real one and nothing downstream could
    tell them apart."""
    states = dict.fromkeys(SYMBOLS, MarketState.CLOSED)
    watcher, calls = build_watcher(states, Clock(TUESDAY))
    watcher.poll_once()
    assert watcher.sleep.phase is SleepPhase.ASLEEP, "it still sleeps"
    assert calls == [], "it just does not publish a week that has not ended"


def test_the_reviews_are_generated_once_per_close() -> None:
    """Idempotent or not, a second pass is a second full read of the week's documents."""
    states = dict.fromkeys(SYMBOLS, MarketState.CLOSED)
    clock = Clock(FRIDAY_CLOSE + timedelta(minutes=1))
    watcher, calls = build_watcher(states, clock)
    for _ in range(5):
        watcher.poll_once()
        clock.advance(hours=2)
    assert len(calls) == 4


def test_one_symbol_failing_does_not_lose_the_other() -> None:
    """A missing silver review is not a reason to have no gold review."""
    from main_review import ReviewWatcher

    calls: list[tuple[str, str, object]] = []

    def build(symbol: str):
        if symbol == "XAUUSD":
            raise RuntimeError("gold's rule is missing")
        return RecordingService(symbol, calls)

    watcher = ReviewWatcher(
        executor_config(),
        market_state_provider=lambda symbol: MarketState.CLOSED,
        schedule=WeeklySchedule(),
        now=Clock(FRIDAY_CLOSE + timedelta(minutes=1)),
        build=build,
    )
    watcher.poll_once()
    assert [(k, s) for k, s, _ in calls] == [("daily", "XAGUSD"), ("weekly", "XAGUSD")]


@pytest.mark.parametrize("fault", [MarketState.STALE, MarketState.UNKNOWN])
def test_no_service_sleeps_on_a_fault(fault: MarketState) -> None:
    """Asserted once per service, because each holds its own cycle and a fifth one added
    later would be the one place this was forgotten."""
    states = dict.fromkeys(SYMBOLS, fault)
    clock = Clock(TUESDAY)

    executor = build_test_executor(states, FakeHeartbeat())
    executor.gate.clock = clock
    monitor = build_test_monitor(states, FakeHeartbeat())
    monitor.gate.clock = clock
    watcher, _ = build_watcher(states, clock)

    for _ in range(5):
        executor.gate.tick()
        monitor.gate.tick()
        watcher.poll_once()
        clock.advance(minutes=10)

    assert executor.sleep.phase is SleepPhase.AWAKE
    assert monitor.sleep.phase is SleepPhase.AWAKE
    assert watcher.sleep.phase is SleepPhase.AWAKE
