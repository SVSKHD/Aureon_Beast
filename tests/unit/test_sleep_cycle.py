"""The weekend, in microseconds (11B).

Every interesting moment of the sleep cycle happens once a week at a fixed hour, which
makes it the kind of behaviour that normally gets tested by hoping. ``SleepCycle`` is fed
a clock and a set of market states instead of reading either, so Friday 21:00 and the
flickering close that is NOT Friday 21:00 are both one function call away.

What is asserted here, in order of how expensive getting it wrong would be:

* a fault never sleeps -- STALE and UNKNOWN are the states an operator most needs the
  system awake and loud for, and both are "quiet" from a tick timestamp alone;
* one symbol closing is not the market closing;
* the close is confirmed before anybody acts on it, and the confirmation resets;
* the crossings are reported exactly once, because the once-per-close work hangs off them;
* the wake is early and the wake is on the broker's word, not the calendar's.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from aureon.models.enums import MarketState
from aureon.services.market_state_service import WeeklySchedule
from aureon.services.sleep_cycle import (
    WAKE_ORDER,
    SleepCycle,
    SleepPhase,
)

SYMBOLS = ("XAUUSD", "XAGUSD")

# 2026-09-18 is a Friday; the schedule closes at 21:00 UTC and opens Sunday 22:00.
FRIDAY_OPEN = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
FRIDAY_CLOSE = datetime(2026, 9, 18, 21, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
SUNDAY_OPEN = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)
TUESDAY = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def states(*, closed: bool = False, **overrides: MarketState) -> dict[str, MarketState]:
    base = dict.fromkeys(
        SYMBOLS, MarketState.CLOSED if closed else MarketState.OPEN
    )
    return base | overrides


def cycle(**overrides) -> SleepCycle:
    return SleepCycle(**overrides)


# ── The schedule can name the boundaries ─────────────────────────────────────


def test_the_schedule_names_the_next_open_from_anywhere_in_the_weekend() -> None:
    schedule = WeeklySchedule()
    for moment in (FRIDAY_CLOSE, SATURDAY, SUNDAY_OPEN - timedelta(minutes=1)):
        assert schedule.next_open(moment) == SUNDAY_OPEN, moment


def test_at_the_open_the_next_open_is_a_week_away() -> None:
    """Strictly after, so a service waking at the open does not compute a deadline that
    has already passed and immediately decide the open is imminent again."""
    schedule = WeeklySchedule()
    assert schedule.next_open(SUNDAY_OPEN) == SUNDAY_OPEN + timedelta(days=7)
    assert schedule.is_open(SUNDAY_OPEN)


def test_the_schedule_names_the_next_close() -> None:
    schedule = WeeklySchedule()
    assert schedule.next_close(FRIDAY_OPEN) == FRIDAY_CLOSE
    assert schedule.next_close(FRIDAY_CLOSE) == FRIDAY_CLOSE + timedelta(days=7)
    assert schedule.next_close(TUESDAY) == datetime(2026, 9, 25, 21, 0, tzinfo=UTC)


def test_the_boundaries_come_from_the_schedule_not_the_weekday() -> None:
    """A broker on a different weekly calendar is configured, not patched: the wake
    deadline follows whatever the schedule says."""
    schedule = WeeklySchedule(open_weekday=0, open_time=time(6, 0))
    assert schedule.next_open(SATURDAY) == datetime(2026, 9, 21, 6, 0, tzinfo=UTC)


# ── A fault is not a weekend ─────────────────────────────────────────────────


def never_sleeps(c: SleepCycle, reading: dict[str, MarketState]) -> None:
    """Assert the phase across an hour of ``reading``, not just at the end of it.

    Asserting only the final phase was a vacuous test: a machine that slept on the wrong
    condition and was then woken by the same poll's OPEN symbol oscillates
    AWAKE -> CLOSING -> ASLEEP -> AWAKE and lands on AWAKE by luck. Found by planting
    ``any`` for ``all`` and watching the assertion still hold.
    """
    for minute in range(0, 60, 5):
        c.observe(reading, now=TUESDAY + timedelta(minutes=minute))
        assert c.phase is SleepPhase.AWAKE, f"{minute}m: {c.phase}"
        assert not c.parked, f"{minute}m"
        assert not c.asleep, f"{minute}m"
        assert c.closed_since is None, f"{minute}m"


@pytest.mark.parametrize("fault", [MarketState.STALE, MarketState.UNKNOWN])
def test_a_fault_never_puts_a_service_to_sleep(fault: MarketState) -> None:
    """The most expensive bug this file exists to prevent.

    A dead feed inside trading hours and a closed market look identical from a tick
    timestamp, and only one of them should quieten the heartbeats. Sleeping on STALE would
    turn an outage into two days of silence that reads exactly like Saturday.
    """
    never_sleeps(cycle(), dict.fromkeys(SYMBOLS, fault))


def test_one_symbol_closing_is_not_the_market_closing() -> None:
    """CLOSED on silver alone is a symbol being disabled. Sleeping on it would stop
    observing gold, which is open and moving."""
    never_sleeps(cycle(), states(XAGUSD=MarketState.CLOSED))


def test_a_service_with_no_symbols_configured_never_sleeps() -> None:
    """"All of nothing is closed" is vacuously true and would park the service for ever."""
    never_sleeps(cycle(), {})


# ── The close is confirmed ───────────────────────────────────────────────────


def test_the_first_closed_poll_does_not_sleep_anybody() -> None:
    c = cycle()
    assert c.observe(states(closed=True), now=FRIDAY_CLOSE) is None
    assert c.phase is SleepPhase.CLOSING
    assert not c.parked, "the close is unconfirmed, and polling is free"


def test_closed_held_for_the_confirmation_window_sleeps() -> None:
    c = cycle(close_confirm_seconds=300.0)
    c.observe(states(closed=True), now=FRIDAY_CLOSE)
    assert c.observe(states(closed=True), now=FRIDAY_CLOSE + timedelta(seconds=299)) is None
    crossing = c.observe(states(closed=True), now=FRIDAY_CLOSE + timedelta(seconds=300))
    assert crossing is not None and crossing.slept
    assert c.phase is SleepPhase.ASLEEP
    assert c.parked


def test_one_open_poll_inside_the_window_resets_the_confirmation() -> None:
    """A broker restarting looks like two closed polls on a Tuesday. The window exists for
    exactly that, so it has to restart from zero rather than accumulate."""
    c = cycle(close_confirm_seconds=300.0)
    c.observe(states(closed=True), now=TUESDAY)
    c.observe(states(closed=True), now=TUESDAY + timedelta(seconds=200))
    c.observe(states(), now=TUESDAY + timedelta(seconds=250))
    assert c.phase is SleepPhase.AWAKE
    assert c.closed_since is None
    # 250s of fresh closed readings is not 450s.
    c.observe(states(closed=True), now=TUESDAY + timedelta(seconds=300))
    assert c.observe(states(closed=True), now=TUESDAY + timedelta(seconds=550)) is None
    assert c.phase is SleepPhase.CLOSING


def test_falling_asleep_is_reported_exactly_once() -> None:
    """The once-per-close work -- the monitor's full reconciliation, the last daily review,
    both weeklies -- hangs off this crossing. A second report would run them twice."""
    c = cycle(close_confirm_seconds=0.0)
    crossings = [
        c.observe(states(closed=True), now=FRIDAY_CLOSE + timedelta(minutes=m))
        for m in range(5)
    ]
    assert sum(1 for x in crossings if x is not None and x.slept) == 1


def test_the_weeks_close_is_marked_weekly_and_a_disabled_symbol_is_not() -> None:
    """Only the weekly close owns the weekly reviews. A holiday closure on a Tuesday sleeps
    the services and generates nothing."""
    weekend = cycle(close_confirm_seconds=0.0)
    crossing = weekend.observe(states(closed=True), now=SATURDAY)
    assert crossing is not None and crossing.weekly
    assert crossing.next_open == SUNDAY_OPEN

    holiday = cycle(close_confirm_seconds=0.0)
    crossing = holiday.observe(states(closed=True), now=TUESDAY)
    assert crossing is not None and not crossing.weekly


# ── Waking ───────────────────────────────────────────────────────────────────


def asleep_on(moment: datetime) -> SleepCycle:
    c = cycle(close_confirm_seconds=0.0)
    c.observe(states(closed=True), now=moment)
    assert c.phase is SleepPhase.ASLEEP
    return c


def test_a_sleeping_service_stays_asleep_across_the_weekend() -> None:
    c = asleep_on(FRIDAY_CLOSE)
    for hour in range(1, 40):
        moment = FRIDAY_CLOSE + timedelta(hours=hour)
        if (SUNDAY_OPEN - moment).total_seconds() <= 3600:
            break  # the pre-open window; tested separately
        assert c.observe(states(closed=True), now=moment) is None, moment
        assert c.parked, moment


def test_preopen_wakes_a_sleeping_service() -> None:
    c = asleep_on(FRIDAY_CLOSE)
    crossing = c.observe(
        dict.fromkeys(SYMBOLS, MarketState.PREOPEN), now=SUNDAY_OPEN - timedelta(minutes=30)
    )
    assert crossing is not None and crossing.woke
    assert c.phase is SleepPhase.WAKING
    assert not c.parked, "waking early is the whole point: the loops run again"
    assert c.asleep, "the market is still shut, though -- no trade may execute yet"


def test_the_scheduled_open_minus_the_lead_wakes_a_service_the_broker_never_previews() -> None:
    """Not every broker publishes PREOPEN. The schedule is the fallback, and the lead time
    is what makes the observer's backfill finish before the first real candle."""
    c = asleep_on(FRIDAY_CLOSE)
    lead = timedelta(seconds=900)
    assert c.observe(states(closed=True), now=SUNDAY_OPEN - lead - timedelta(seconds=1)) is None
    crossing = c.observe(states(closed=True), now=SUNDAY_OPEN - lead)
    assert crossing is not None and crossing.woke
    assert c.phase is SleepPhase.WAKING


def test_an_open_symbol_wakes_a_service_whatever_the_calendar_says() -> None:
    """The broker is the authority. A market that opens early after an outage is right and
    the schedule is wrong."""
    c = asleep_on(FRIDAY_CLOSE)
    crossing = c.observe(states(), now=SATURDAY)
    assert crossing is not None and crossing.woke
    assert c.phase is SleepPhase.AWAKE
    assert not c.asleep


def test_waking_becomes_awake_at_the_first_open_reading() -> None:
    c = asleep_on(FRIDAY_CLOSE)
    c.observe(dict.fromkeys(SYMBOLS, MarketState.PREOPEN), now=SUNDAY_OPEN - timedelta(minutes=10))
    assert c.phase is SleepPhase.WAKING
    assert c.observe(states(), now=SUNDAY_OPEN) is None, "already reported as a wake"
    assert c.phase is SleepPhase.AWAKE


def test_waking_is_reported_exactly_once() -> None:
    c = asleep_on(FRIDAY_CLOSE)
    crossings = [
        c.observe(states(), now=SUNDAY_OPEN + timedelta(minutes=m)) for m in range(5)
    ]
    assert sum(1 for x in crossings if x is not None and x.woke) == 1


# ── Cadences ─────────────────────────────────────────────────────────────────


def test_the_heartbeat_slows_while_parked_and_never_stops() -> None:
    c = cycle(sleep_heartbeat_seconds=300.0)
    assert c.heartbeat_seconds(5.0) == 5.0
    c.observe(states(closed=True), now=FRIDAY_CLOSE)
    c.observe(states(closed=True), now=FRIDAY_CLOSE + timedelta(seconds=400))
    assert c.parked
    assert c.heartbeat_seconds(5.0) == 300.0
    assert c.heartbeat_seconds(5.0) > 0, "a heartbeat that stops looks like a dead process"


def test_the_poll_cadence_slows_while_parked() -> None:
    c = asleep_on(FRIDAY_CLOSE)
    assert c.poll_seconds(1.0) == 60.0
    c.observe(states(), now=SUNDAY_OPEN)
    assert c.poll_seconds(1.0) == 1.0


def test_a_waking_service_polls_at_the_awake_cadence() -> None:
    """It has work to do -- that is why it woke early."""
    c = asleep_on(FRIDAY_CLOSE)
    c.observe(dict.fromkeys(SYMBOLS, MarketState.PREOPEN), now=SUNDAY_OPEN - timedelta(minutes=10))
    assert c.poll_seconds(1.0) == 1.0
    assert c.heartbeat_seconds(5.0) == 5.0


def test_the_description_names_the_next_open_when_shut() -> None:
    c = asleep_on(FRIDAY_CLOSE)
    line = c.describe(SATURDAY)
    assert "asleep" in line
    assert "20 Sep 22:00 UTC" in line
    c.observe(states(), now=SUNDAY_OPEN)
    assert c.describe(SUNDAY_OPEN) == "awake"


def test_the_wake_order_puts_the_observer_first_and_discord_last() -> None:
    """Everything downstream reads what the observer writes, and Discord must not say
    "live" until the things behind it are."""
    assert WAKE_ORDER == ("observer", "monitor", "executor", "discord")
