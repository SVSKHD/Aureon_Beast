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
from datetime import datetime, time

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
        stale_tick_seconds: float = DEFAULT_STALE_TICK_SECONDS,
    ) -> None:
        self.provider = provider
        self.schedule = schedule or WeeklySchedule()
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

        return classify_market_state(
            now=moment,
            symbol_info=info,
            last_tick_at=last_tick,
            schedule=self.schedule,
            stale_tick_seconds=self.stale_tick_seconds,
        )
