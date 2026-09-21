"""The observer at the weekly close, and at the open (11B).

``tests/unit/test_sleep_cycle.py`` proves the state machine. This file proves the
observer actually *does* the things a parked phase implies, which is a separate claim:
a service can hold a correct phase and go on polling, and nothing about the phase
would notice.

So each test here names a consequence rather than a state:

* no stream is read while parked -- the provider's own call counter says so;
* the loop is still running and the terminal is still connected, because exiting would
  turn a weekly event into a restart and a restart is where a cursor gets lost;
* the outbox and the candle archive are drained AT the close, since a two-day pause is
  a long time to hold an undelivered detection or an unflushed parquet tail;
* the heartbeat slows and does not stop;
* the open finds the observer already awake, and the weekend's gap is backfilled;
* a bar straddling the close produces no detections, is archived anyway, and does not
  move the cursor backwards.

The clock is the fake provider's, so the whole weekend takes milliseconds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureon.config import AureonConfig
from aureon.models.base import MarketTime
from aureon.models.enums import MarketState, SleepPhase, Timeframe
from aureon.models.market import Candle
from aureon.outbox.local_outbox import LocalOutbox
from aureon.outbox.outbox_worker import OutboxWorker
from aureon.services.market_state_service import MarketStateService, WeeklySchedule
from aureon.services.observer_state import ObserverState
from aureon.services.sleep_cycle import SleepPhase as CyclePhase
from aureon.storage.detection_repository import DetectionRepository
from aureon.storage.system_state_repository import SystemStateRepository
from main_observer import Observer
from tests.conftest import MARKET_TZ, FakeLiveProvider, InMemoryFirestore, cross_agent

SYMBOL = "XAUUSD"

# 2026-09-18 is a Friday. The default schedule closes at 21:00 UTC and reopens Sunday
# 2026-09-20 at 22:00 UTC.
FRIDAY_CLOSE = datetime(2026, 9, 18, 21, 0, tzinfo=UTC)
SUNDAY_OPEN = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)

#: Short enough to cross in a few polls, long enough that one closed poll is not a close.
CONFIRM_SECONDS = 120.0


#: The last bar of the trading week, and the first of the next one -- both real rows of
#: aureon/data/fixtures/XAUUSD_M5.csv, which was generated across this very weekend.
LAST_FRIDAY_BAR = datetime(2026, 9, 18, 20, 55, tzinfo=UTC)
FIRST_SUNDAY_BAR = SUNDAY_OPEN


def before_the_close(candles: list[Candle]) -> list[Candle]:
    """The fixture week up to the Friday close, and nothing after it.

    The real fixture rather than a synthetic ramp: these are the bars the rest of the suite
    measures, they carry a genuine EMA cross, and the file already contains this exact
    weekend -- Friday 20:55 then nothing until Sunday 22:00 -- so the gap the observer has
    to sleep through is the one the data has.
    """
    return [c for c in candles if c.open_time.utc <= LAST_FRIDAY_BAR]


def after_the_open(candles: list[Candle]) -> list[Candle]:
    return [c for c in candles if c.open_time.utc >= FIRST_SUNDAY_BAR]


def config_for(tmp_path: Path) -> AureonConfig:
    return AureonConfig.from_env(
        env={
            "AUREON_ACCOUNT_SCOPE": "primary",
            "AUREON_MARKET_TZ": MARKET_TZ,
            "AUREON_SYMBOLS": SYMBOL,
            "AUREON_TIMEFRAMES": "M5",
            "AUREON_OUTBOX_PATH": str(tmp_path / "outbox.db"),
            "AUREON_OBSERVER_STATE_PATH": str(tmp_path / "state.json"),
            "AUREON_CLOSE_CONFIRM_SECONDS": str(CONFIRM_SECONDS),
            "AUREON_PREOPEN_SECONDS": "900",
            "AUREON_SLEEP_HEARTBEAT_SECONDS": "300",
            "AUREON_SLEEP_POLL_SECONDS": "60",
            "AUREON_STATE_HEARTBEAT_SECONDS": "5",
        }
    )


class Wired:
    """An observer, its fake feed, and the doubles a test needs to look at.

    ``seed_hours`` seeds the cursor that many hours before the last Friday bar, so the first
    poll processes a handful of bars rather than the 500 a cold start asks for. Most tests
    here are about a phase and not about detections, and a cold start in each of twenty of
    them costs minutes. A test that needs the week's detections passes ``seed_hours=None``.
    """

    def __init__(
        self, tmp_path: Path, candles: list[Candle], *, seed_hours: float | None = 2.0
    ) -> None:
        self.config = config_for(tmp_path)
        self.firestore = InMemoryFirestore()
        self.provider = FakeLiveProvider(candles)
        self.outbox = LocalOutbox(self.config.outbox_path)
        self.delivered: list[dict] = []
        self.worker = OutboxWorker(self.outbox, self._deliver)
        self.observer = Observer(
            self.config,
            self.provider,
            outbox=self.outbox,
            worker=self.worker,
            state=ObserverState(self.config.observer_state_path),
            market_state=MarketStateService(self.provider),
            state_repository=SystemStateRepository(self.firestore),
            agents=[cross_agent()],
        )
        if seed_hours is not None:
            self.observer.market_engine.seed_cursor(
                SYMBOL, Timeframe.M5, LAST_FRIDAY_BAR - timedelta(hours=seed_hours)
            )

    def _deliver(self, payload: dict) -> None:
        self.delivered.append(payload)

    def poll_at(self, moment: datetime) -> None:
        self.provider.set_clock(moment)
        self.observer.market_engine.poll_once()

    def state_doc(self) -> dict:
        from aureon.storage import paths

        return self.firestore.docs.get(
            paths.system_state_path(SYMBOL, Timeframe.M5), {}
        )


@pytest.fixture
def wired(tmp_path: Path, candles: list[Candle]) -> Wired:
    """A week's worth of bars up to the Friday close, and nothing after it."""
    return Wired(tmp_path, before_the_close(candles))


def sleep_it(wired: Wired, *, at: datetime = FRIDAY_CLOSE) -> None:
    """Drive the observer past the confirmation window into ASLEEP."""
    wired.poll_at(at + timedelta(seconds=1))
    wired.poll_at(at + timedelta(seconds=CONFIRM_SECONDS + 1))
    assert wired.observer.sleep.phase is CyclePhase.ASLEEP


# ── Falling asleep ───────────────────────────────────────────────────────────


def test_inside_the_week_the_observer_is_awake(wired: Wired) -> None:
    """A control, so a blanket "always asleep" bug is not mistaken for correctness."""
    wired.poll_at(FRIDAY_CLOSE - timedelta(hours=2))
    assert wired.observer.sleep.phase is CyclePhase.AWAKE
    assert not wired.observer.sleep.parked


def test_the_first_closed_poll_still_reads_the_streams(wired: Wired) -> None:
    """The close is unconfirmed, so nothing has changed yet."""
    wired.poll_at(FRIDAY_CLOSE + timedelta(seconds=1))
    assert wired.observer.sleep.phase is CyclePhase.CLOSING
    before = wired.provider.calls
    wired.poll_at(FRIDAY_CLOSE + timedelta(seconds=2))
    assert wired.provider.calls > before, "a closing observer still observes"


def test_no_stream_is_read_while_parked(wired: Wired) -> None:
    """The consequence the phase exists for.

    Asserted on the provider's own counter rather than on the phase: an observer that
    held ASLEEP and went on polling would satisfy every assertion about its phase.
    """
    sleep_it(wired)
    before = wired.provider.calls
    for minute in range(1, 30):
        wired.poll_at(FRIDAY_CLOSE + timedelta(minutes=minute + 5))
    assert wired.provider.calls == before


def test_alerts_are_not_checked_while_parked(wired: Wired) -> None:
    """No new quotes arrive over a weekend, and firing a level off a two-day-old one
    would answer a question nobody asked."""
    sleep_it(wired)
    before = wired.provider.quote_calls
    wired.poll_at(FRIDAY_CLOSE + timedelta(hours=3))
    assert wired.provider.quote_calls == before


def test_the_process_stays_up_and_the_terminal_stays_connected(wired: Wired) -> None:
    """Exiting would turn a predictable weekly event into a restart, and a restart is
    where a cursor, a lease or an undrained outbox goes missing."""
    sleep_it(wired)
    assert not wired.observer.market_engine.stopped
    assert not getattr(wired.provider, "closed", False)


def test_the_outbox_is_drained_at_the_close(
    tmp_path: Path, candles: list[Candle]
) -> None:
    """A queued detection is one nobody has seen, and the weekend is two days long."""
    wired = Wired(tmp_path, before_the_close(candles), seed_hours=None)
    # The worker's thread is never started here, so detections pile up in the queue
    # exactly as they would between two passes of a live one.
    wired.poll_at(FRIDAY_CLOSE - timedelta(hours=1))
    held = wired.outbox.pending_count()
    assert held >= 1, "the week produced a detection that is still queued"
    assert wired.delivered == []

    sleep_it(wired)
    assert wired.outbox.pending_count() == 0
    assert len(wired.delivered) == held


def test_the_cursor_is_saved_at_the_close(wired: Wired) -> None:
    wired.poll_at(FRIDAY_CLOSE - timedelta(hours=1))
    sleep_it(wired)
    reloaded = ObserverState(wired.config.observer_state_path)
    assert reloaded.get(SYMBOL, Timeframe.M5) is not None


def test_the_heartbeat_slows_at_the_close_and_never_stops(
    tmp_path: Path, candles: list[Candle]
) -> None:
    from aureon.services.heartbeat_service import HeartbeatService
    from aureon.storage.system_state_repository import HeartbeatRepository

    wired = Wired(tmp_path, before_the_close(candles))
    beat = HeartbeatService(
        HeartbeatRepository(wired.firestore), "observer", interval_seconds=5.0
    )
    wired.observer.heartbeat = beat

    sleep_it(wired)
    assert beat.interval_seconds == 300.0

    wired.provider._all.extend(after_the_open(candles))
    wired.poll_at(SUNDAY_OPEN + timedelta(minutes=10))
    assert beat.interval_seconds == 5.0


def test_a_status_reader_is_told_it_is_closed_and_when_it_opens(wired: Wired) -> None:
    """Discord may not call the broker and must not compute the weekly boundary itself,
    so the process holding the feed publishes both."""
    sleep_it(wired)
    doc = wired.state_doc()
    assert doc["sleep_phase"] == SleepPhase.ASLEEP.value
    assert doc["next_market_open"] == SUNDAY_OPEN.isoformat()


def test_an_awake_observer_publishes_no_next_open(wired: Wired) -> None:
    """A "next open" on a Tuesday afternoon reads as a closure that is not happening.

    An hour before the close rather than two: the state write is forced on candle close, and
    two hours back is behind the seeded cursor, so nothing would have closed and there would
    be no document to read. Asserting against a missing document would have passed for the
    wrong reason.
    """
    wired.poll_at(FRIDAY_CLOSE - timedelta(hours=1))
    doc = wired.state_doc()
    assert doc, "a candle closed, so there is a document"
    assert doc["sleep_phase"] == SleepPhase.AWAKE.value
    assert doc["next_market_open"] is None


# ── Waking ───────────────────────────────────────────────────────────────────


def test_the_observer_wakes_before_the_open_and_polls_again(
    wired: Wired, candles: list[Candle]
) -> None:
    sleep_it(wired)
    wired.provider._all.extend(after_the_open(candles))

    wired.poll_at(SUNDAY_OPEN - timedelta(seconds=800))
    assert wired.observer.sleep.phase is CyclePhase.WAKING
    assert not wired.observer.sleep.parked

    before = wired.provider.calls
    wired.poll_at(SUNDAY_OPEN - timedelta(seconds=700))
    assert wired.provider.calls > before, "a waking observer observes"


def test_the_weekends_worth_of_bars_is_picked_up_on_waking(
    wired: Wired, candles: list[Candle]
) -> None:
    """The cursor is where Friday left it, so the first awake poll asks for everything
    since -- which across a weekend is a two-day range holding one session's bars."""
    wired.poll_at(FRIDAY_CLOSE - timedelta(hours=1))
    last_friday = wired.observer.market_engine.cursor(SYMBOL, Timeframe.M5)
    assert last_friday is not None and last_friday < FRIDAY_CLOSE
    sleep_it(wired)

    monday = after_the_open(candles)
    wired.provider._all.extend(monday)
    # A minute past the last bar's close plus the engine's grace, so the final bar is
    # trusted to be final rather than still forming.
    wired.poll_at(SUNDAY_OPEN + timedelta(hours=2, minutes=1))

    resumed = wired.observer.market_engine.cursor(SYMBOL, Timeframe.M5)
    assert resumed is not None and resumed > last_friday
    # Past the open, not merely past Friday: the claim is that the bars on the far side of
    # a 49-hour gap were fetched, which a cursor that had crept forward would not show.
    assert resumed >= FIRST_SUNDAY_BAR
    assert resumed in {c.open_time.utc for c in monday}


def test_the_phase_returns_to_awake_at_the_open(
    wired: Wired, candles: list[Candle]
) -> None:
    sleep_it(wired)
    wired.provider._all.extend(after_the_open(candles))
    wired.poll_at(SUNDAY_OPEN - timedelta(seconds=600))
    wired.poll_at(SUNDAY_OPEN + timedelta(minutes=10))
    assert wired.observer.sleep.phase is CyclePhase.AWAKE
    assert not wired.observer.sleep.asleep


# ── The gap guard ────────────────────────────────────────────────────────────


def spanning_bar() -> Candle:
    """A bar running 20:58 to 21:03 on the Friday: five minutes containing the close."""
    opened = FRIDAY_CLOSE - timedelta(minutes=2)
    return Candle(
        symbol=SYMBOL,
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(opened, MARKET_TZ),
        open=2400.0,
        # A weekend gap, which is what makes this bar's range a lie about five minutes.
        high=2460.0,
        low=2399.0,
        close=2455.0,
        tick_volume=50,
    )


def test_a_bar_spanning_the_close_produces_no_detections(
    tmp_path: Path, candles: list[Candle]
) -> None:
    """Its range IS the weekend gap, so any cross found in it is a two-day move that
    involved no trading."""
    bars = before_the_close(candles)[:-1] + [spanning_bar()]
    wired = Wired(tmp_path, bars)
    wired.poll_at(FRIDAY_CLOSE + timedelta(minutes=5))

    engine = wired.observer.engines.for_symbol(SYMBOL)
    assert [s.open_time for s in engine.skipped] == [spanning_bar().open_time.utc]
    assert all(
        d.get("candle_open_time") != spanning_bar().open_time.utc.isoformat()
        for d in wired.delivered
    )


def test_the_spanning_bar_does_not_become_a_session_extreme(
    tmp_path: Path, candles: list[Candle]
) -> None:
    """A high taken from the gap bar would record a two-day move as five minutes, and
    every session statistic downstream would inherit it."""
    bars = before_the_close(candles)[:-1] + [spanning_bar()]
    wired = Wired(tmp_path, bars)
    wired.poll_at(FRIDAY_CLOSE + timedelta(minutes=5))

    snapshot = wired.observer._snapshot(SYMBOL, Timeframe.M5)
    assert snapshot.session_high is None or snapshot.session_high < 2460.0


def test_the_cursor_advances_past_a_spanning_bar(
    tmp_path: Path, candles: list[Candle]
) -> None:
    """Or it would be refetched and refused on every poll, for ever."""
    bars = before_the_close(candles)[:-1] + [spanning_bar()]
    wired = Wired(tmp_path, bars)
    wired.poll_at(FRIDAY_CLOSE + timedelta(minutes=5))

    reloaded = ObserverState(wired.config.observer_state_path)
    assert reloaded.get(SYMBOL, Timeframe.M5) == spanning_bar().open_time.utc


def test_the_last_real_bar_of_the_week_is_not_refused(
    tmp_path: Path, candles: list[Candle]
) -> None:
    """20:55 to 21:00 on a Friday is five real minutes of trading. Refusing it would
    throw away the week's final candle every week -- the failure mode that makes the
    boundaries exclusive."""
    bars = before_the_close(candles)
    assert bars[-1].close_time == FRIDAY_CLOSE
    wired = Wired(tmp_path, bars)
    wired.poll_at(FRIDAY_CLOSE + timedelta(seconds=10))

    engine = wired.observer.engines.for_symbol(SYMBOL)
    assert engine.skipped == []
    assert wired.observer.market_engine.cursor(SYMBOL, Timeframe.M5) == (
        bars[-1].open_time.utc
    )


# ── Faults keep it awake ─────────────────────────────────────────────────────


def test_a_market_state_service_that_raises_leaves_the_observer_awake(
    wired: Wired,
) -> None:
    """UNKNOWN is a fault, and a fault is exactly when the observer must stay loud."""

    class Broken:
        schedule = WeeklySchedule()

        def state_for(self, symbol: str, **kwargs: object):
            raise RuntimeError("the terminal went away")

    wired.observer.market_state = Broken()
    for minute in range(0, 40, 5):
        wired.poll_at(FRIDAY_CLOSE + timedelta(minutes=minute))
        assert wired.observer.sleep.phase is CyclePhase.AWAKE, minute
    assert wired.observer._states == {SYMBOL: MarketState.UNKNOWN}


def test_an_observer_with_no_market_state_service_never_sleeps(
    tmp_path: Path, candles: list[Candle]
) -> None:
    """It cannot tell a closed market from a dead one, and staying awake is the answer
    that cannot turn an outage into silence."""
    wired = Wired(tmp_path, before_the_close(candles))
    wired.observer.market_state = None
    for hour in range(0, 40, 4):
        wired.poll_at(FRIDAY_CLOSE + timedelta(hours=hour))
        assert wired.observer.sleep.phase is CyclePhase.AWAKE, hour


def test_one_symbol_disabled_does_not_sleep_a_two_symbol_observer(
    tmp_path: Path, candles: list[Candle]
) -> None:
    """The observer-level version of the rule.

    A close-only silver inside the trading week is the broker disabling one instrument.
    Parking on it would stop observing gold, which is open and moving -- so this asserts
    the loop kept READING, not merely that the phase looked right.
    """
    config = AureonConfig.from_env(
        env={
            "AUREON_ACCOUNT_SCOPE": "primary",
            "AUREON_MARKET_TZ": MARKET_TZ,
            "AUREON_SYMBOLS": "XAUUSD,XAGUSD",
            "AUREON_TIMEFRAMES": "M5",
            "AUREON_EVAL_RULES": "XAUUSD:XAU_OUTCOME_V2,XAGUSD:XAG_OUTCOME_V1",
            "AUREON_OUTBOX_PATH": str(tmp_path / "outbox.db"),
            "AUREON_OBSERVER_STATE_PATH": str(tmp_path / "state.json"),
            "AUREON_CLOSE_CONFIRM_SECONDS": str(CONFIRM_SECONDS),
        }
    )
    tuesday = FRIDAY_CLOSE - timedelta(days=3)
    provider = ClosedForSilver(candles)
    firestore = InMemoryFirestore()
    outbox = LocalOutbox(config.outbox_path)
    observer = Observer(
        config,
        provider,
        outbox=outbox,
        worker=OutboxWorker(outbox, DetectionRepository(firestore).upsert_payload),
        state=ObserverState(config.observer_state_path),
        market_state=MarketStateService(provider),
        # A roster each: one shared list would share the agents' LevelTrackers between two
        # instruments, which the observer refuses outright.
        agents={"XAUUSD": [cross_agent()], "XAGUSD": [cross_agent()]},
    )

    for minute in range(0, 40, 5):
        provider.set_clock(tuesday + timedelta(hours=4, minutes=minute))
        before = provider.calls
        observer.market_engine.poll_once()
        assert observer.sleep.phase is CyclePhase.AWAKE, minute
        assert provider.calls > before, f"{minute}m: gold stopped being observed"

    assert observer._states == {
        "XAUUSD": MarketState.OPEN,
        "XAGUSD": MarketState.CLOSED,
    }


class ClosedForSilver(FakeLiveProvider):
    """A terminal quoting gold normally and reporting silver close-only."""

    def symbol_info(self, symbol: str):
        info = super().symbol_info(symbol)
        if symbol == "XAGUSD":
            return info.model_copy(update={"trade_mode": "close_only"})
        return info
