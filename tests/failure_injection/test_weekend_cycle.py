"""Forty-eight simulated hours across a weekend, all four services at once (11B).

The unit suites prove each piece: the state machine in ``test_sleep_cycle``, the observer's
consequences in ``test_observer_sleep``, the other three in ``test_service_sleep``, Discord's
reading of the published phase in ``test_discord_sleep``. What none of them can show is the
thing an operator actually cares about -- that the four processes, running together against
real Firestore, get through a weekend without anybody touching them.

So this drives one shared clock from Friday afternoon to Sunday's open, a minute or an hour
at a time, and asserts the four claims the phase is finished on:

1. **all four sleep**, at the same confirmed close, and each does its own close-time work;
2. **all four wake**, before the open, in the order ``WAKE_ORDER`` names;
3. **zero restarts** -- every loop is the same object it started as, every provider is still
   connected, and no process exited;
4. **Saturday ``/status``** renders the weekly review and says when the market opens next.

Against the emulator rather than a double, because the whole point is that the phase and the
next open go through a real document that a different process reads back. A `system_state`
write that only ever round-tripped through an in-memory dict would prove nothing about what
Discord sees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureon.config import AureonConfig
from aureon.evaluation.rules import get_rule
from aureon.execution.fake_broker import FakeBroker
from aureon.models.enums import MarketState, SleepPhase, Timeframe
from aureon.models.settings import ExecutionSettings
from aureon.outbox.local_outbox import LocalOutbox
from aureon.outbox.outbox_worker import OutboxWorker
from aureon.reviews.service import ReviewService
from aureon.services.market_state_service import MarketStateService
from aureon.services.observer_state import ObserverState
from aureon.services.sleep_cycle import WAKE_ORDER
from aureon.services.sleep_cycle import SleepPhase as CyclePhase
from aureon.storage.detection_repository import DetectionRepository
from aureon.storage.system_state_repository import HeartbeatRepository, SystemStateRepository
from aureon.storage.trade_repository import TradeRepository
from aureon.storage.trade_request_repository import TradeRequestRepository
from tests.conftest import MARKET_TZ, FakeLiveProvider, cross_agent

pytestmark = pytest.mark.emulator

GOLD = "XAUUSD"
SILVER = "XAGUSD"
SYMBOLS = (GOLD, SILVER)

# The fixture week's own boundaries: Friday 2026-09-18 21:00 UTC to Sunday 22:00.
FRIDAY_CLOSE = datetime(2026, 9, 18, 21, 0, tzinfo=UTC)
LAST_FRIDAY_BAR = datetime(2026, 9, 18, 20, 55, tzinfo=UTC)
SUNDAY_OPEN = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)

#: Short, so the confirmation is crossed in two polls of the simulated clock.
CONFIRM_SECONDS = 120.0

#: The four services this test drives, in the order it drives them -- which is
#: ``WAKE_ORDER`` with the review watcher appended and Discord left out.
#:
#: Discord is left out because it holds no sleep cycle: it reads the phase the observer
#: writes, so it is last in ``WAKE_ORDER`` by construction rather than by scheduling, and
#: what it sees is asserted through ``/status`` below. The review watcher is a fifth process
#: that 11B gave a cycle to, and it goes last because the reviews it generates read documents
#: the other three have finished writing.
DRIVEN_ORDER = (*WAKE_ORDER[:3], "review")


class WeekendProvider(FakeLiveProvider):
    """A terminal over gold and silver, with one clock and a connection that stays up."""

    def __init__(self, candles: list, silver: list) -> None:
        super().__init__(candles + silver)
        #: Counted so "the process never reconnected" is a real assertion rather than an
        #: absence of evidence.
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


def config_for(tmp_path: Path) -> AureonConfig:
    return AureonConfig.from_env(
        env={
            "AUREON_ACCOUNT_SCOPE": "primary",
            "AUREON_MARKET_TZ": MARKET_TZ,
            "AUREON_SYMBOLS": ",".join(SYMBOLS),
            "AUREON_TIMEFRAMES": "M5",
            "AUREON_EVAL_RULES": f"{GOLD}:XAU_OUTCOME_V2,{SILVER}:XAG_OUTCOME_V1",
            "AUREON_OUTBOX_PATH": str(tmp_path / "outbox.db"),
            "AUREON_OBSERVER_STATE_PATH": str(tmp_path / "state.json"),
            "AUREON_CLOSE_CONFIRM_SECONDS": str(CONFIRM_SECONDS),
            "AUREON_PREOPEN_SECONDS": "900",
            "AUREON_SLEEP_HEARTBEAT_SECONDS": "300",
            "AUREON_SLEEP_POLL_SECONDS": "60",
            "AUREON_STATE_HEARTBEAT_SECONDS": "5",
        }
    )


class Deployment:
    """All four services, one clock, one Firestore project.

    Each service is built exactly as its ``build_*`` function builds it, minus the MT5
    provider and the real broker -- the two things this environment cannot have. Everything
    else, including the ``system_state`` round trip the whole phase turns on, is real.
    """

    def __init__(self, tmp_path: Path, client, candles: list, silver: list) -> None:
        from main_executor import Executor
        from main_monitor import Monitor
        from main_observer import Observer
        from main_review import ReviewWatcher

        self.config = config_for(tmp_path)
        self.client = client
        self.provider = WeekendProvider(candles, silver)
        self.market_state = MarketStateService(self.provider)
        self.broker = FakeBroker()
        self.transitions: list[tuple[str, str]] = []

        settings = ExecutionSettings(
            trading_enabled=True, max_lot=10.0, status_stale_after_seconds=45.0
        )

        # ── the observer ─────────────────────────────────────────────────────
        self.outbox = LocalOutbox(self.config.outbox_path)
        self.observer = Observer(
            self.config,
            self.provider,
            outbox=self.outbox,
            worker=OutboxWorker(
                self.outbox, DetectionRepository(client).upsert_payload
            ),
            state=ObserverState(self.config.observer_state_path),
            market_state=self.market_state,
            state_repository=SystemStateRepository(client),
            agents={symbol: [cross_agent()] for symbol in SYMBOLS},
        )
        for symbol in SYMBOLS:
            self.observer.market_engine.seed_cursor(
                symbol, Timeframe.M5, LAST_FRIDAY_BAR - timedelta(hours=1)
            )

        # ── the executor ─────────────────────────────────────────────────────
        self.executor = Executor(
            self.config,
            self.broker,
            TradeRequestRepository(client),
            settings_provider=lambda: settings,
            market_state_provider=self._state_of,
            heartbeat=None,
            schedule=self.market_state.schedule,
            now=self.now,
        )

        # ── the monitor ──────────────────────────────────────────────────────
        self.monitor = Monitor(
            self.config,
            self.broker,
            TradeRepository(client, account_scope=self.config.account_scope),
            TradeRequestRepository(client),
            market_state_provider=self._state_of,
            schedule=self.market_state.schedule,
            now=self.now,
        )

        # ── the review watcher ───────────────────────────────────────────────
        self.watcher = ReviewWatcher(
            self.config,
            market_state_provider=self._state_of,
            schedule=self.market_state.schedule,
            now=self.now,
            build=lambda symbol: ReviewService(
                client,
                get_rule(self.config.rule_id_for(symbol)),
                market_tz=MARKET_TZ,
                infer_window_minutes=self.config.infer_window_minutes,
                account_scope=self.config.account_scope,
                symbol=symbol,
            ),
        )

        self.heartbeats = HeartbeatRepository(client)
        self.state_reader = SystemStateRepository(client)
        self._watch_transitions()

    # ── The clock ────────────────────────────────────────────────────────────

    def now(self) -> datetime:
        return self.provider.now_utc()

    def detections_from(self, candles: list, *, count: int = 3) -> list:
        """A few real detections off the fixture week, for the close to deliver."""
        from aureon.engine.analysis_engine import AnalysisEngine

        engine = AnalysisEngine(
            [cross_agent()],
            account_scope=self.config.account_scope,
            market_tz=MARKET_TZ,
        )
        return engine.feed(candles[:900])[:count]

    def _state_of(self, symbol: str) -> MarketState:
        return self.market_state.state_for(symbol).state

    def _watch_transitions(self) -> None:
        """Record every crossing, per service, in the order they happen."""
        for name, gate in self.gates.items():
            for kind in ("on_sleep", "on_wake"):
                existing = getattr(gate, kind)

                def record(crossing, _name=name, _existing=existing) -> None:
                    self.transitions.append((_name, crossing.kind))
                    if _existing is not None:
                        _existing(crossing)

                setattr(gate, kind, record)

    @property
    def gates(self) -> dict[str, object]:
        """In ``WAKE_ORDER``, which is also the order the tick drives them in."""
        return {
            "observer": self.observer.gate,
            "monitor": self.monitor.gate,
            "executor": self.executor.gate,
            "review": self.watcher.gate,
        }

    def tick(self, moment: datetime) -> None:
        """One simulated poll of the whole deployment.

        The observer goes through its real market engine, so the streams are genuinely
        skipped while parked. The other three are driven at their gate, because their poll
        bodies need a live broker and what is under test is the sleep decision.
        """
        self.provider.set_clock(moment)
        self.observer.market_engine.poll_once()
        self.monitor.gate.tick()
        self.executor.gate.tick()
        self.watcher.poll_once()

    def run_from(self, start: datetime, until: datetime, *, step: timedelta) -> None:
        moment = start
        while moment <= until:
            self.tick(moment)
            moment += step

    # ── What a reader sees ───────────────────────────────────────────────────

    def status_screen(self, *, now: datetime):
        """``/status`` as Discord builds it: Firestore reads only."""
        from aureon.discord.service import build_status

        reviews = {}
        from aureon.storage.review_reader import ReviewReader

        # The reader Discord actually holds -- it cannot write a review, which is the whole
        # reason the context takes a reader rather than the repository.
        reader = ReviewReader(self.client)
        for symbol in SYMBOLS:
            found = reader.latest_for(symbol)
            if found is not None:
                reviews[symbol] = found
        return build_status(
            system_state=self.state_reader.read(),
            heartbeats=self.heartbeats.read_all(),
            settings=ExecutionSettings(trading_enabled=True, max_lot=10.0),
            latest_reviews=reviews,
            now=now,
            sleep_heartbeat_seconds=self.config.sleep_heartbeat_seconds,
        )


@pytest.fixture
def deployment(tmp_path: Path, firestore_client, candles, silver_candles) -> Deployment:
    """The week up to Friday's close, and the bars of the session after the open."""
    week = [c for c in candles if c.open_time.utc <= LAST_FRIDAY_BAR]
    silver_week = [c for c in silver_candles if c.open_time.utc <= LAST_FRIDAY_BAR]
    return Deployment(tmp_path, firestore_client, week, silver_week)


def after_the_open(candles: list, silver: list) -> list:
    return [
        c
        for c in candles + silver
        if SUNDAY_OPEN <= c.open_time.utc < SUNDAY_OPEN + timedelta(hours=6)
    ]


# ── The weekend ──────────────────────────────────────────────────────────────


def test_forty_eight_hours_pass_with_nobody_touching_anything(
    deployment: Deployment, candles, silver_candles
) -> None:
    """The acceptance test for 11B.

    Friday afternoon to Sunday's open, in one run. Every assertion below is about the four
    processes together; each has its own unit test for the part it owns.
    """
    d = deployment

    # ── Friday, still trading ────────────────────────────────────────────────
    d.run_from(
        FRIDAY_CLOSE - timedelta(hours=1),
        FRIDAY_CLOSE - timedelta(minutes=5),
        step=timedelta(minutes=5),
    )
    assert all(
        gate.cycle.phase is CyclePhase.AWAKE for gate in d.gates.values()
    ), "a control: nobody sleeps inside the trading week"
    friday_calls = d.provider.calls
    assert friday_calls > 0, "and the streams were genuinely being read"

    # Something for the close to drain. Real detections from the fixture week, queued as the
    # observer queues them: a first draft asserted an empty queue after the close and passed
    # against a queue that had never held anything, which planting the drain away proved.
    held = d.detections_from(candles)
    assert d.outbox.enqueue_many(held) == len(held)
    assert d.outbox.pending_count() == len(held)

    # ── the close ────────────────────────────────────────────────────────────
    d.run_from(
        FRIDAY_CLOSE + timedelta(seconds=1),
        FRIDAY_CLOSE + timedelta(seconds=CONFIRM_SECONDS + 60),
        step=timedelta(seconds=60),
    )
    assert [
        name for name, kind in d.transitions if kind == "slept"
    ] == list(DRIVEN_ORDER), "all four slept, once each, in the order they were driven"
    assert all(gate.cycle.parked for gate in d.gates.values())

    # ── the close's own work ─────────────────────────────────────────────────
    assert d.outbox.pending_count() == 0, "the outbox was drained at the close"
    stored = DetectionRepository(d.client)
    assert all(stored.get(one.detection_id) is not None for one in held), (
        "and they are in Firestore, not merely off the queue"
    )
    assert d.monitor.closing_reconciliations == 1, "one full reconciliation, not none, not two"
    assert len(d.watcher.generated) == 2 * len(SYMBOLS), (
        "a daily and a weekly for each symbol: " + ", ".join(d.watcher.generated)
    )

    # ── the weekend ──────────────────────────────────────────────────────────
    parked_at = d.provider.calls
    d.run_from(
        FRIDAY_CLOSE + timedelta(hours=1),
        SUNDAY_OPEN - timedelta(hours=2),
        step=timedelta(minutes=30),
    )
    assert d.provider.calls == parked_at, "not one candle request in forty-odd hours"
    assert d.monitor.closing_reconciliations == 1, "and no second reconciliation"
    assert len(d.watcher.generated) == 2 * len(SYMBOLS), "and no second set of reviews"

    # ── Saturday, through /status ────────────────────────────────────────────
    saturday = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    screen = d.status_screen(now=saturday)
    assert screen.market_closed is True
    assert screen.sleep_phase is SleepPhase.ASLEEP
    assert screen.closed_line is not None
    assert "next open Sun 20 Sep 22:00 UTC" in screen.closed_line
    assert screen.review_summary is not None
    assert GOLD in screen.review_summary and SILVER in screen.review_summary
    assert screen.live_panels == [], "a closed screen shows the review, not a live layout"

    # ── waking, before the open ──────────────────────────────────────────────
    d.provider._all.extend(after_the_open(candles, silver_candles))
    d.run_from(
        SUNDAY_OPEN - timedelta(minutes=45),
        SUNDAY_OPEN - timedelta(minutes=5),
        step=timedelta(minutes=5),
    )
    woke = [name for name, kind in d.transitions if kind == "woke"]
    assert woke == list(DRIVEN_ORDER), f"woke out of order: {woke}"
    assert all(not gate.cycle.parked for gate in d.gates.values())
    assert d.provider.calls > parked_at, "and the observer is reading candles again"

    # ── the open ─────────────────────────────────────────────────────────────
    d.run_from(SUNDAY_OPEN, SUNDAY_OPEN + timedelta(hours=1), step=timedelta(minutes=5))
    assert all(gate.cycle.phase is CyclePhase.AWAKE for gate in d.gates.values())
    resumed = d.observer.market_engine.cursor(GOLD, Timeframe.M5)
    assert resumed is not None and resumed >= SUNDAY_OPEN, (
        "the bars on the far side of a 49-hour gap were picked up"
    )

    # ── zero restarts ────────────────────────────────────────────────────────
    assert d.provider.closes == 0, "the terminal was never disconnected"
    assert not d.observer.market_engine.stopped, "and the observer's loop never exited"
    assert len(d.transitions) == 8, (
        "four asleep, four awake, and nothing else: " + repr(d.transitions)
    )


def test_a_trade_confirmed_over_the_weekend_is_refused_rather_than_ignored(
    deployment: Deployment,
) -> None:
    """The executor's half of the design, end to end.

    A human who confirms a trade on a Saturday gets MARKET_CLOSED, not silence until Sunday
    night. Parking the request loop -- the obvious thing to copy from the observer -- would
    leave the request sitting confirmed until the open, which is indistinguishable from a
    broken executor and is how somebody comes to restart one.
    """
    from aureon.models.enums import FailureCode, TradeRequestStatus
    from tests.failure_injection.conftest import make_request

    d = deployment
    d.run_from(
        FRIDAY_CLOSE + timedelta(seconds=1),
        FRIDAY_CLOSE + timedelta(seconds=CONFIRM_SECONDS + 60),
        step=timedelta(seconds=60),
    )
    assert d.executor.sleep.parked

    repository = TradeRequestRepository(d.client)
    request = make_request()
    repository.create(request)
    repository.confirm(request.request_id, request.requested_by, request.quote)

    d.executor.worker.process(request.request_id)

    stored = repository.get(request.request_id)
    assert stored.status is TradeRequestStatus.FAILED
    assert stored.failure_code is FailureCode.MARKET_CLOSED
    # The guard reads a broker snapshot before it refuses -- it is a pure function fed one,
    # and reading moves no money. What must not happen is a SEND.
    assert not [c for c in d.broker.calls if c.startswith("send_")], d.broker.calls
