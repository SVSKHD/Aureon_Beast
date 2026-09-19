"""The market data interface every provider implements (§7-§9).

One interface, two implementations: a live MT5 terminal and a historical file
reader. Keeping the return types identical is what makes replay/live parity
*testable* -- if the historical provider returned a slightly different shape, a
parity failure could never be distinguished from a data difference.

The hard rule, restated in every implementation: **only CLOSED candles are ever
returned.** A forming bar's high, low and close all still move, so an agent that
saw one would emit a detection encoding a price that never finally existed, and
replay could never reproduce it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from aureon.models.enums import Timeframe
from aureon.models.market import Candle, QuoteSnapshot, SymbolInfo


class MarketDataError(RuntimeError):
    """A provider could not satisfy a request."""


class BaseMarketDataProvider(ABC):
    """Read-only access to market data.

    Deliberately read-only: there is no order-placing method anywhere on this
    interface, so an agent handed a provider cannot trade even by accident. The
    boundary tests enforce the same thing at the import level.
    """

    @abstractmethod
    def get_closed_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        from_utc: datetime,
        to_utc: datetime,
    ) -> list[Candle]:
        """Closed candles with ``from_utc <= open_time < to_utc``, ascending.

        Half-open on purpose, so consecutive calls can tile a range without
        double-counting the boundary candle.
        """

    @abstractmethod
    def get_quote(self, symbol: str) -> QuoteSnapshot:
        """The current bid/ask."""

    @abstractmethod
    def symbol_info(self, symbol: str) -> SymbolInfo:
        """Broker metadata: volumes, point size, stop distance, filling modes."""

    def get_latest_closed_candle(self, symbol: str, timeframe: Timeframe) -> Candle | None:
        """The most recent CLOSED candle, or None when there is none.

        Default implementation asks for a window generously wider than one candle
        and takes the last, so a provider only has to implement the range query
        correctly. A gap (weekend, holiday) simply yields fewer candles.
        """
        now = self.now_utc()
        # 200 timeframes back comfortably spans a weekend at M5 and above.
        span = timedelta(minutes=timeframe.minutes * 200)
        candles = self.get_closed_candles(symbol, timeframe, now - span, now)
        return candles[-1] if candles else None

    def now_utc(self) -> datetime:
        """The provider's notion of now, in UTC.

        Overridable so replay can drive a deterministic clock. Live providers must
        derive this from the broker's server time converted to UTC, never from the
        local OS clock (CLAUDE.md).
        """
        from aureon.models.base import utc_now

        return utc_now()

    def connect(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Establish any connection.

        Deliberately concrete and empty, not abstract: a file-backed provider has
        nothing to connect to, and forcing it to implement a no-op would add
        ceremony without adding safety.
        """

    def close(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release resources. Nothing to release for file-backed providers."""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def floor_to_timeframe(moment: datetime, timeframe: Timeframe) -> datetime:
    """Round ``moment`` down to the start of its timeframe bucket.

    Used to decide which candle is currently forming, and therefore which is the
    last closed one. Anchored to the UTC epoch day so the buckets are stable
    across processes and restarts.
    """
    minutes = timeframe.minutes
    if timeframe is Timeframe.D1:
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((moment - day_start).total_seconds() // 60)
    return day_start + timedelta(minutes=(elapsed // minutes) * minutes)


def is_closed(candle_open: datetime, timeframe: Timeframe, now: datetime) -> bool:
    """Whether the candle opening at ``candle_open`` has finished.

    A candle is closed once ``now`` has reached its end. Callers add a small grace
    period on top before trusting a broker to have finalised it.
    """
    return now >= candle_open + timedelta(minutes=timeframe.minutes)
