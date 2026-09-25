"""Is the market actually open? (§10)

Four inputs decide it, and the order they are checked in matters:

1. **the broker's ``trade_mode``** -- if the broker says the symbol is disabled or
   close-only, nothing else is relevant;
2. **the weekly schedule** -- gold and FX close over the weekend, so a perfectly
   healthy feed is expected to go quiet;
3. **tick age** -- inside trading hours, a feed that has stopped ticking is STALE,
   which is a *fault*, not a closure;
4. otherwise OPEN.

The distinction in (2) versus (3) is the whole point of this service. "Quiet because
it is Sunday morning" and "quiet because our connection died" look identical from a
tick timestamp alone, and only one of them should raise an alarm.

``PREOPEN`` covers the run-up to the weekly open, when a broker may accept a
connection and publish a quote while not yet accepting trades -- the state that makes
Sunday 21:30 UTC different from Sunday 23:30 UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from aureon.models.base import to_utc
from aureon.models.enums import MarketState
from aureon.models.market import SymbolInfo

# Weekly schedule for gold/FX, in UTC. Sunday 22:00 open, Friday 21:00 close.
# UTC rather than market-local: the weekly boundary is a market-wide convention,
# not a property of whichever broker's clock we happen to be reading.
SUNDAY = 6
FRIDAY = 4
DEFAULT_WEEK_OPEN = time(22, 0)
DEFAULT_WEEK_CLOSE = time(21, 0)

# How long before the weekly open the market counts as PREOPEN.
DEFAULT_PREOPEN_MINUTES = 60.0

# Inside trading hours, a feed quiet for longer than this is a fault.
DEFAULT_STALE_TICK_SECONDS = 120.0


@dataclass(frozen=True)
class WeeklySchedule:
    """When the market is open across the week, in UTC."""

    open_weekday: int = SUNDAY
    open_time: time = DEFAULT_WEEK_OPEN
    close_weekday: int = FRIDAY
    close_time: time = DEFAULT_WEEK_CLOSE
    preopen_minutes: float = DEFAULT_PREOPEN_MINUTES

    def is_open(self, moment: datetime) -> bool:
        """Whether ``moment`` (UTC) falls inside the trading week."""
        weekday, clock = moment.weekday(), moment.time()
        if weekday == self.open_weekday:
            return clock >= self.open_time
        if weekday == self.close_weekday:
            return clock < self.close_time
        # Saturday is the only remaining day outside Mon-Fri trading.
        return weekday not in {5}

    def is_preopen(self, moment: datetime) -> bool:
        """Whether ``moment`` is in the window just before the weekly open."""
        if self.is_open(moment):
            return False
        if moment.weekday() != self.open_weekday:
            return False
        opens_at = moment.replace(
            hour=self.open_time.hour, minute=self.open_time.minute, second=0, microsecond=0
        )
        delta = (opens_at - moment).total_seconds()
        return 0 < delta <= self.preopen_minutes * 60

    def _next_weekly(self, moment: datetime, weekday: int, clock: time) -> datetime:
        """The next occurrence of ``weekday`` at ``clock``, strictly after ``moment``.

        Strictly: at exactly Sunday 22:00 the market is open, so "the next open" is a week
        away, not now. The alternative reading would have a service that wakes at the open
        immediately compute a wake deadline in the past.

        No DST arithmetic, because the whole schedule is UTC. That is deliberate -- the
        weekly boundary is a market-wide convention, and a broker whose clock observes
        summer time would otherwise move the weekend twice a year.
        """
        candidate = moment.replace(
            hour=clock.hour, minute=clock.minute, second=0, microsecond=0
        )
        candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
        if candidate <= moment:
            candidate += timedelta(days=7)
        return candidate

    def next_open(self, moment: datetime) -> datetime:
        """When the market next opens, for "closed until ..." and for the wake deadline."""
        return self._next_weekly(to_utc(moment), self.open_weekday, self.open_time)

    def next_close(self, moment: datetime) -> datetime:
        """When the market next closes."""
        return self._next_weekly(to_utc(moment), self.close_weekday, self.close_time)

    def spans_a_close(self, start: datetime, end: datetime) -> bool:
        """Whether a bar running ``start`` to ``end`` contains a weekly close (11B).

        A bar that straddles the close is not a bar. Its open is the last price before the
        weekend and its close is the first price after it, so its range IS the weekend gap:
        an EMA fed that bar is wrong for the next fifty, and any detection from it encodes
        a move that took two days and no trading.

        The boundaries are exclusive on purpose. The last legitimate bar of the week ENDS
        at the close -- 20:55 to 21:00 on a Friday is a real five minutes of trading -- and
        treating it as spanning would throw away the week's final candle every week.

        The limit, stated rather than papered over: a bar whose NOMINAL length runs past the
        close even though its data stops there would be refused. A daily bar is the obvious
        example -- MT5's Friday D1 runs 00:00 to 00:00 and holds only trading it saw, so
        this would call it spanning. It is unreachable here, because the polled streams are
        intraday and the higher timeframes are aggregated from closed M5 bars that never
        span a close themselves. A deployment that starts POLLING a daily stream has to
        revisit this.
        """
        return self.close_spanned_by(start, end) is not None

    def close_spanned_by(self, start: datetime, end: datetime) -> datetime | None:
        """The weekly close a bar straddles, or None. See :meth:`spans_a_close`.

        Returns the instant rather than a bool so a caller can say WHICH close it refused a
        bar for -- a log line naming Friday 21:00 is diagnosable and "invalid candle" is not.
        """
        boundary = self.next_close(to_utc(start))
        return boundary if boundary < to_utc(end) else None


@dataclass(frozen=True)
class AlwaysOpenSchedule:
    """Schedule for broker-traded instruments expected to quote through weekends.

    Broker trade_mode and tick freshness still decide whether the symbol is actually usable.
    This schedule only prevents a generic gold/FX calendar from closing crypto by itself.
    """

    preopen_minutes: float = 0.0

    def is_open(self, moment: datetime) -> bool:
        return True

    def is_preopen(self, moment: datetime) -> bool:
        return False

    def next_open(self, moment: datetime) -> datetime:
        return to_utc(moment)

    def next_close(self, moment: datetime) -> datetime:
        return to_utc(moment) + timedelta(days=3650)

    def spans_a_close(self, start: datetime, end: datetime) -> bool:
        return False

    def close_spanned_by(self, start: datetime, end: datetime) -> datetime | None:
        return None


@dataclass(frozen=True)
class MarketStateResult:
    """The classification plus why, so a status embed can explain itself."""

    state: MarketState
    reason: str
    tick_age_seconds: float | None = None

    @property
    def tradeable(self) -> bool:
        return self.state is MarketState.OPEN


def classify_market_state(
    *,
    now: datetime,
    symbol_info: SymbolInfo | None = None,
    last_tick_at: datetime | None = None,
    schedule: WeeklySchedule | None = None,
    stale_tick_seconds: float = DEFAULT_STALE_TICK_SECONDS,
) -> MarketStateResult:
    """Classify a symbol's tradability (§10).

    A pure function: same inputs, same answer, no clock read of its own. That makes
    the awkward cases -- the weekly boundary, a dead feed on a Tuesday -- directly
    unit-testable instead of reachable only by waiting for Sunday.
    """
    moment = to_utc(now)
    schedule = schedule or WeeklySchedule()

    # 1. The broker's own verdict wins.
    if symbol_info is None:
        return MarketStateResult(MarketState.UNKNOWN, "no symbol info available")
    if symbol_info.trade_mode == "unknown":
        return MarketStateResult(MarketState.UNKNOWN, "broker reports unknown trade_mode")
    if not symbol_info.is_tradeable:
        return MarketStateResult(
            MarketState.CLOSED, f"broker trade_mode={symbol_info.trade_mode}"
        )

    # 2. The weekly schedule: quiet is expected here.
    if not schedule.is_open(moment):
        if schedule.is_preopen(moment):
            return MarketStateResult(MarketState.PREOPEN, "within the pre-open window")
        return MarketStateResult(MarketState.CLOSED, "outside the trading week")

    # 3. Inside hours, silence is a fault.
    if last_tick_at is None:
        return MarketStateResult(MarketState.STALE, "no tick seen yet")
    age = (moment - to_utc(last_tick_at)).total_seconds()
    if age > stale_tick_seconds:
        return MarketStateResult(
            MarketState.STALE, f"last tick {age:.0f}s ago (limit {stale_tick_seconds:.0f}s)", age
        )

    return MarketStateResult(MarketState.OPEN, "trading", age)


class MarketStateService:
    """Classifies market state for configured symbols, using a data provider."""

    def __init__(
        self,
        provider: object,
        *,
        schedule: WeeklySchedule | None = None,
        schedule_resolver: object | None = None,
        stale_tick_seconds: float = DEFAULT_STALE_TICK_SECONDS,
    ) -> None:
        self.provider = provider
        self.schedule = schedule or WeeklySchedule()
        self.schedule_resolver = schedule_resolver
        self.stale_tick_seconds = stale_tick_seconds

    def state_for(self, symbol: str, *, now: datetime | None = None) -> MarketStateResult:
        """Classify one symbol, tolerating a provider that cannot answer.

        A provider error becomes UNKNOWN rather than an exception: the observer's
        state write must still happen, and "we could not tell" is itself the useful
        thing to report.
        """
        provider = self.provider
        moment = to_utc(now) if now is not None else provider.now_utc()  # type: ignore[attr-defined]

        try:
            info = provider.symbol_info(symbol)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return MarketStateResult(MarketState.UNKNOWN, f"symbol_info failed: {exc}")

        last_tick = None
        getter = getattr(provider, "last_tick_time", None)
        if callable(getter):
            try:
                last_tick = getter(symbol)
            except Exception:  # noqa: BLE001 - absence is handled below
                last_tick = None
        else:
            try:
                last_tick = provider.get_quote(symbol).captured_at  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                last_tick = None

        schedule = self.schedule
        if callable(self.schedule_resolver):
            try:
                schedule = self.schedule_resolver(symbol)
            except Exception:  # noqa: BLE001 - fall back to the configured default
                schedule = self.schedule

        return classify_market_state(
            now=moment,
            symbol_info=info,
            last_tick_at=last_tick,
            schedule=schedule,
            stale_tick_seconds=self.stale_tick_seconds,
        )
