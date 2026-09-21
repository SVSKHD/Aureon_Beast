"""Sleeping through the weekend and waking before the open (11B).

The market closes on Friday evening and does not open again until Sunday night. For
roughly forty-eight hours every service in the system has nothing to do, and the two
obvious ways to handle that are both wrong:

* **keep polling as if nothing happened** -- two days of "no new candle" at one poll a
  second, a heartbeat every five seconds that means nothing, and an ops register that
  cannot tell a closed market from a dead feed;
* **exit and rely on something to restart us** -- which turns a predictable weekly event
  into a restart, and a restart is the one moment a stateful service can lose its cursor,
  its lease or its outbox.

So: the services stay up, park their loops, and wake themselves before the open.

## The close is decided by one authority

``MarketStateService`` classifies tradability, and nothing here second-guesses it. In
particular no service anywhere asks the weekday directly: a broker that closes gold for a
holiday, or opens late after an outage, is right and a hardcoded calendar is wrong. The
schedule is consulted for exactly one thing -- *when* the next open is, so a sleeping
service knows how long it has -- and that lives on ``WeeklySchedule``.

## Why a close is confirmed before anybody acts on it

A single CLOSED reading is not the weekend. A broker restarting, a symbol briefly
disabled, a quote request that timed out into ``trade_mode=unknown`` -- each shows up as
one or two closed polls inside a perfectly normal Tuesday. Tearing down the loops on the
strength of that would cost a wake-up and, worse, would fire the once-per-close work
(the monitor's full reconciliation, the weekly reviews) in the middle of the session.

So CLOSED has to hold continuously for ``close_confirm_seconds`` before anything sleeps.
Any other state inside the window resets it; the cost of the delay is five minutes of
idle polling at the real close, which is nothing, and the thing it buys is that
"the market closed" means it.

## Why STALE and UNKNOWN never put anybody to sleep

They are faults. A feed that has stopped ticking inside trading hours, or a provider that
cannot say what a symbol is, is precisely when an operator needs the heartbeats fast and
the ops register loud. Sleeping through that would convert an outage into silence and the
silence would look exactly like Saturday. Only CLOSED sleeps.

## Why every symbol has to agree

CLOSED on XAGUSD alone is a symbol being disabled, not a market closing. Sleeping on it
would stop observing XAUUSD, which is open and moving. So the sleep needs every
configured symbol closed, and a system with no symbols configured never sleeps at all --
"all of nothing is closed" is vacuously true and would park a service forever.

## Waking early, on purpose

Services wake at the earlier of PREOPEN and ``preopen_seconds`` before the scheduled
open, because being up *at* the open is too late: the observer needs its backfill done
and its cursor seeded, the executor needs its lease clean, and Discord needs to have
stopped saying "closed". The lead time is wasted work on a quiet feed, which is the
cheapest thing in this file.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from aureon.models.base import to_utc
from aureon.models.enums import MarketState, SleepPhase
from aureon.services.market_state_service import WeeklySchedule

__all__ = [
    "DEFAULT_CLOSE_CONFIRM_SECONDS",
    "DEFAULT_PREOPEN_SECONDS",
    "DEFAULT_SLEEP_HEARTBEAT_SECONDS",
    "DEFAULT_SLEEP_POLL_SECONDS",
    "WAKE_ORDER",
    "SleepCycle",
    "SleepGate",
    "SleepPhase",
    "SleepTransition",
]

log = logging.getLogger(__name__)

#: CLOSED must hold this long continuously before anything sleeps.
DEFAULT_CLOSE_CONFIRM_SECONDS = 300.0

#: How far before the scheduled open a sleeping service wakes itself.
DEFAULT_PREOPEN_SECONDS = 900.0

#: Heartbeat cadence while asleep. Slow, but never absent: a heartbeat that stopped
#: would be indistinguishable from a process that died over the weekend.
DEFAULT_SLEEP_HEARTBEAT_SECONDS = 300.0

#: How often a sleeping service asks whether the market is open yet.
DEFAULT_SLEEP_POLL_SECONDS = 60.0


@dataclass(frozen=True)
class SleepTransition:
    """A crossing worth acting on, returned by :meth:`SleepCycle.observe`.

    Only the two crossings that matter are reported -- falling asleep and waking -- and
    each is reported exactly once. The once-per-close work hangs off ``slept``: the
    monitor's full reconciliation, the last daily review and the two weeklies all need to
    happen at the close and must not happen again on the next poll.
    """

    #: ``"slept"`` or ``"woke"``.
    kind: str
    at: datetime
    #: True when the *schedule* says we are outside the trading week, rather than the
    #: broker having disabled the symbols. The week's final close is a weekly one, and the
    #: weekly reviews belong to it; a symbol disabled on a Tuesday is not the weekend.
    weekly: bool
    #: When the market is next expected to open, for a human-readable "closed until".
    next_open: datetime

    @property
    def slept(self) -> bool:
        return self.kind == "slept"

    @property
    def woke(self) -> bool:
        return self.kind == "woke"


@dataclass
class SleepCycle:
    """The state machine every service shares.

    Deliberately not a loop and not a thread: it is fed a clock reading and the market
    states the caller already has, and it answers with a phase. That makes the whole of
    the weekend -- the flickering close, the confirmation, the early wake -- reachable in
    a unit test in microseconds instead of only by waiting for Friday.
    """

    schedule: WeeklySchedule = field(default_factory=WeeklySchedule)
    close_confirm_seconds: float = DEFAULT_CLOSE_CONFIRM_SECONDS
    preopen_seconds: float = DEFAULT_PREOPEN_SECONDS
    sleep_heartbeat_seconds: float = DEFAULT_SLEEP_HEARTBEAT_SECONDS
    sleep_poll_seconds: float = DEFAULT_SLEEP_POLL_SECONDS

    phase: SleepPhase = SleepPhase.AWAKE
    #: When the current unbroken run of CLOSED readings began.
    closed_since: datetime | None = None
    slept_at: datetime | None = None
    woke_at: datetime | None = None

    # ── Reading the phase ─────────────────────────────────────────────────────

    @property
    def parked(self) -> bool:
        """Whether the caller's loops should be idle.

        ``CLOSING`` is not parked: the close is unconfirmed and polling is free.
        ``WAKING`` is not parked either -- that is the entire point of waking early.
        """
        return self.phase is SleepPhase.ASLEEP

    @property
    def asleep(self) -> bool:
        """Whether the market is confirmed closed, whether or not the open is imminent."""
        return self.phase in {SleepPhase.ASLEEP, SleepPhase.WAKING}

    def heartbeat_seconds(self, awake_cadence: float) -> float:
        """The heartbeat cadence for the current phase."""
        return self.sleep_heartbeat_seconds if self.parked else awake_cadence

    def poll_seconds(self, awake_cadence: float) -> float:
        """How long to wait before the next loop iteration."""
        return self.sleep_poll_seconds if self.parked else awake_cadence

    def next_open(self, now: datetime) -> datetime:
        return self.schedule.next_open(to_utc(now))

    def describe(self, now: datetime) -> str:
        """One line for a status embed or a log."""
        if not self.asleep:
            return f"{self.phase.value}"
        return f"{self.phase.value}, next open {self.next_open(now):%a %d %b %H:%M} UTC"

    # ── Feeding it ────────────────────────────────────────────────────────────

    def observe(
        self, states: dict[str, MarketState], *, now: datetime
    ) -> SleepTransition | None:
        """Advance the machine one tick; return a crossing if one happened.

        ``states`` maps every configured symbol to its current classification. A missing
        symbol is not the same as a closed one, so the caller passes the whole set or the
        machine cannot tell "all closed" from "we only asked about one".
        """
        moment = to_utc(now)
        all_closed = bool(states) and all(s is MarketState.CLOSED for s in states.values())
        any_open = any(s is MarketState.OPEN for s in states.values())
        any_preopen = any(s is MarketState.PREOPEN for s in states.values())

        if not all_closed:
            self.closed_since = None
        elif self.closed_since is None:
            self.closed_since = moment

        if self.phase in {SleepPhase.AWAKE, SleepPhase.CLOSING}:
            return self._while_awake(moment, all_closed=all_closed)
        return self._while_asleep(
            moment, all_closed=all_closed, any_open=any_open, any_preopen=any_preopen
        )

    def _while_awake(self, moment: datetime, *, all_closed: bool) -> SleepTransition | None:
        if not all_closed:
            self.phase = SleepPhase.AWAKE
            return None
        assert self.closed_since is not None  # set by observe
        held = (moment - self.closed_since).total_seconds()
        if held < self.close_confirm_seconds:
            self.phase = SleepPhase.CLOSING
            return None
        self.phase = SleepPhase.ASLEEP
        self.slept_at = moment
        return SleepTransition(
            kind="slept",
            at=moment,
            weekly=not self.schedule.is_open(moment),
            next_open=self.schedule.next_open(moment),
        )

    def _while_asleep(
        self,
        moment: datetime,
        *,
        all_closed: bool,
        any_open: bool,
        any_preopen: bool,
    ) -> SleepTransition | None:
        if any_open:
            # The broker is the authority: if it is quoting a tradeable symbol the weekend
            # is over, whatever the calendar thinks.
            if self.phase is SleepPhase.WAKING:
                # Already reported when the loops restarted. The wake work runs once, at
                # the crossing out of ASLEEP -- reporting the open as a second wake would
                # re-run the observer's backfill against a live feed.
                self.phase = SleepPhase.AWAKE
                return None
            return self._wake(moment)
        if self.phase is SleepPhase.ASLEEP and (
            any_preopen or self._open_is_imminent(moment)
        ):
            self.phase = SleepPhase.WAKING
            return self._wake(moment, phase=SleepPhase.WAKING)
        if (
            self.phase is SleepPhase.WAKING
            and all_closed
            and not any_preopen
            and not self._open_is_imminent(moment)
        ):
            # The lead window moved out from under us -- a schedule change, or a clock that
            # jumped. Back to sleep rather than burning a weekend polling.
            self.phase = SleepPhase.ASLEEP
        return None

    def _open_is_imminent(self, moment: datetime) -> bool:
        lead = (self.schedule.next_open(moment) - moment).total_seconds()
        return lead <= self.preopen_seconds

    def _wake(
        self, moment: datetime, *, phase: SleepPhase = SleepPhase.AWAKE
    ) -> SleepTransition:
        self.phase = phase
        self.woke_at = moment
        self.closed_since = None
        return SleepTransition(
            kind="woke",
            at=moment,
            weekly=not self.schedule.is_open(moment),
            next_open=self.schedule.next_open(moment),
        )


#: The order services come back up in, and why.
#:
#: The observer first, because everything downstream reads what it writes: a monitor that
#: woke first would reconcile against a stale system_state, and Discord would render the
#: previous week's snapshot as if it were live. The executor last of the three, because it
#: is the only one that can move money and the guard's freshness checks should be reading
#: quotes the observer has already refreshed. Discord last of all: it is the operator's
#: window, and it should not show "live" until the things behind it are.
WAKE_ORDER: tuple[str, ...] = ("observer", "monitor", "executor", "discord")


@dataclass
class SleepGate:
    """One service's sleep cycle, shaped for a poll loop.

    Every service in the system runs the same loop -- ``while not stopped: poll_once();
    wait(cadence)`` -- so the decision to park one is the same three steps in four places:
    read the market states, feed the cycle, act on a crossing. Writing that three times
    invites three slightly different versions of "what counts as closed", which is exactly
    the drift that ``SleepCycle`` exists to prevent.

    **Nothing here raises.** A sleep decision is an optimisation; a service that crashed
    because it could not work out whether the market was open would be strictly worse than
    one that stayed awake unnecessarily. So every callback and every read is guarded, and
    every failure leaves the phase where it was -- which, from AWAKE, means awake.
    """

    cycle: SleepCycle
    #: Every configured symbol's current state. Called once per poll.
    states: Callable[[], dict[str, MarketState]]
    #: The clock. A provider's, not the process's, wherever there is one to ask.
    clock: Callable[[], datetime]
    on_sleep: Callable[[SleepTransition], None] | None = None
    on_wake: Callable[[SleepTransition], None] | None = None
    #: Named in log lines, so four services parking at the same minute are distinguishable.
    service: str = "service"

    def tick(self) -> SleepTransition | None:
        """Advance the cycle one poll. Call this FIRST in the loop body.

        First, because a decision taken after the work could only park the loop from the
        next iteration -- and at the open that costs a poll for no reason.
        """
        try:
            states = self.states()
            now = self.clock()
        except Exception:  # noqa: BLE001 - see the class docstring
            log.exception("%s: could not read the market state; staying as we are", self.service)
            return None
        try:
            crossing = self.cycle.observe(states, now=now)
        except Exception:  # noqa: BLE001
            log.exception("%s: sleep cycle raised; staying as we are", self.service)
            return None
        if crossing is None:
            return None
        if crossing.slept:
            log.info(
                "%s: market closed, parking until %s", self.service, crossing.next_open
            )
            self._call(self.on_sleep, crossing)
        else:
            log.info("%s: market opening, resuming", self.service)
            self._call(self.on_wake, crossing)
        return crossing

    def _call(
        self, hook: Callable[[SleepTransition], None] | None, crossing: SleepTransition
    ) -> None:
        if hook is None:
            return
        try:
            hook(crossing)
        except Exception:  # noqa: BLE001
            log.exception("%s: %s hook failed", self.service, crossing.kind)

    @property
    def parked(self) -> bool:
        return self.cycle.parked

    def pace(self, awake_cadence: float) -> float:
        return self.cycle.poll_seconds(awake_cadence)

    def heartbeat(self, awake_cadence: float) -> float:
        return self.cycle.heartbeat_seconds(awake_cadence)
