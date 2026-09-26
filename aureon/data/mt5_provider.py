"""Live market data from a MetaTrader 5 terminal (§8).

One of only TWO modules permitted to import MetaTrader5 (CLAUDE.md), and it does
so **lazily inside ``connect()``** so that importing this module -- which the test
suite does, to exercise the time conversion -- works on Linux and macOS where the
package does not exist.

## Server time is not UTC

MT5 reports candle and tick times as an integer that *looks* like a Unix timestamp
but is really the broker's wall clock encoded as though it were UTC. A broker on
Europe/Athens in summer reports 16:00 for a candle that opened at 13:00 UTC.
Interpreting that number directly as UTC shifts every candle by the broker's
offset, which silently misfiles sessions, day boundaries and detection ids.

``server_epoch_to_utc`` does the correct conversion and is a plain function with no
MT5 dependency, so the part most likely to be wrong is the part that is unit
tested.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aureon.data.base_provider import (
    BaseMarketDataProvider,
    MarketDataError,
    floor_to_timeframe,
)
from aureon.models.base import MarketTime, utc_now
from aureon.models.enums import AccountMode, FillingMode, Timeframe
from aureon.models.market import Candle, QuoteSnapshot, SymbolInfo

# MT5 timeframe constants, by name. Resolved at call time against the imported
# module so this mapping needs no MetaTrader5 import to exist.
_MT5_TIMEFRAME_ATTR: dict[Timeframe, str] = {
    Timeframe.M1: "TIMEFRAME_M1",
    Timeframe.M5: "TIMEFRAME_M5",
    Timeframe.M15: "TIMEFRAME_M15",
    Timeframe.M30: "TIMEFRAME_M30",
    Timeframe.H1: "TIMEFRAME_H1",
    Timeframe.H4: "TIMEFRAME_H4",
    Timeframe.D1: "TIMEFRAME_D1",
}

# Filling-mode bit flags (SYMBOL_FILLING_FOK = 1, SYMBOL_FILLING_IOC = 2).
_FILLING_FOK = 1
_FILLING_IOC = 2

# Trade mode constants (SYMBOL_TRADE_MODE_*).
_TRADE_MODE_NAMES = {0: "disabled", 1: "longonly", 2: "shortonly", 3: "close_only", 4: "full"}


def server_epoch_to_utc(epoch_seconds: float, market_tz: str) -> datetime:
    """Convert an MT5 server timestamp to a real UTC instant.

    MT5's value is the broker's wall clock encoded as a UTC epoch. So: decode it as
    UTC to recover the broker's wall-clock fields, reinterpret those fields in the
    broker's zone, then convert to true UTC.

    DST is handled by ``ZoneInfo``, which is why the broker zone must be a real
    IANA zone (``Europe/Athens``) and not a fixed offset -- a fixed offset would be
    wrong for half the year.
    """
    wall = datetime.fromtimestamp(epoch_seconds, tz=UTC).replace(tzinfo=None)
    return wall.replace(tzinfo=ZoneInfo(market_tz)).astimezone(UTC)


def utc_to_server_epoch(moment: datetime, market_tz: str) -> float:
    """Inverse of ``server_epoch_to_utc``, for building MT5 query bounds."""
    if moment.tzinfo is None:
        raise ValueError("moment must be tz-aware")
    wall = moment.astimezone(ZoneInfo(market_tz)).replace(tzinfo=UTC)
    return wall.timestamp()


class MT5DataProvider(BaseMarketDataProvider):
    """Reads candles, quotes and symbol metadata from a live MT5 terminal."""

    def __init__(
        self,
        *,
        market_tz: str,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        terminal_path: str | None = None,
        candle_grace_seconds: float = 2.0,
    ) -> None:
        self.market_tz = market_tz
        self._login = login
        self._password = password
        self._server = server
        self._terminal_path = terminal_path
        # Small grace before trusting the terminal to have finalised a bar. Without
        # it, polling exactly on the boundary can read a bar the terminal is still
        # writing.
        self.candle_grace_seconds = candle_grace_seconds
        self._mt5: Any | None = None

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Initialise the terminal. Imports MetaTrader5 lazily (CLAUDE.md)."""
        try:
            import MetaTrader5 as mt5  # noqa: N813  (vendor's own casing)
        except ImportError as exc:
            raise MarketDataError(
                "MetaTrader5 is unavailable. It is Windows-only; install the 'mt5' "
                "extra there, or use HistoricalDataProvider / a fake for tests."
            ) from exc

        kwargs: dict[str, Any] = {}
        if self._terminal_path:
            kwargs["path"] = self._terminal_path
        if self._login:
            kwargs.update(login=self._login, password=self._password, server=self._server)

        if not mt5.initialize(**kwargs):
            raise MarketDataError(f"MT5 initialize failed: {mt5.last_error()}")
        self._mt5 = mt5

    def close(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
            self._mt5 = None

    @property
    def mt5(self) -> Any:
        if self._mt5 is None:
            raise MarketDataError("provider is not connected; call connect() first")
        return self._mt5

    # ── Data ──────────────────────────────────────────────────────────────────

    def get_closed_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        from_utc: datetime,
        to_utc: datetime,
    ) -> list[Candle]:
        """Closed candles in ``[from_utc, to_utc)``.

        The forming bar is dropped here rather than left to callers: one caller
        forgetting would emit a detection on a price that later changed.
        """
        mt5 = self.mt5
        tf = self._timeframe_constant(timeframe)
        start = utc_to_server_epoch(from_utc, self.market_tz)
        end = utc_to_server_epoch(to_utc, self.market_tz)

        rates = mt5.copy_rates_range(symbol, tf, start, end)
        if rates is None:
            raise MarketDataError(
                f"copy_rates_range({symbol}, {timeframe}) failed: {mt5.last_error()}"
            )

        cutoff = self._last_closed_open(timeframe)
        candles: list[Candle] = []
        for rate in rates:
            opened = server_epoch_to_utc(float(rate["time"]), self.market_tz)
            if opened > cutoff:
                continue  # the forming bar, or one not yet final
            candles.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=MarketTime.from_utc(opened, self.market_tz),
                    open=float(rate["open"]),
                    high=float(rate["high"]),
                    low=float(rate["low"]),
                    close=float(rate["close"]),
                    tick_volume=int(rate["tick_volume"]),
                    real_volume=(
                        int(rate["real_volume"]) if "real_volume" in rate.dtype.names else 0
                    ),
                    spread=int(rate["spread"]) if "spread" in rate.dtype.names else None,
                )
            )
        candles.sort(key=lambda c: c.open_time.utc)
        return candles

    def _last_closed_open(self, timeframe: Timeframe) -> datetime:
        """Open time of the most recent bar that is certainly closed."""
        now = self.now_utc() - timedelta(seconds=self.candle_grace_seconds)
        return floor_to_timeframe(now, timeframe) - timedelta(minutes=timeframe.minutes)

    def get_quote(self, symbol: str) -> QuoteSnapshot:
        mt5 = self.mt5
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MarketDataError(f"symbol_info_tick({symbol}) failed: {mt5.last_error()}")
        info = self.symbol_info(symbol)
        return QuoteSnapshot(
            symbol=symbol,
            bid=float(tick.bid),
            ask=float(tick.ask),
            captured_at=server_epoch_to_utc(float(tick.time), self.market_tz),
            point=info.point,
        )

    def symbol_info(self, symbol: str) -> SymbolInfo:
        mt5 = self.mt5
        info = mt5.symbol_info(symbol)
        if info is None:
            raise MarketDataError(f"symbol_info({symbol}) failed: {mt5.last_error()}")
        return SymbolInfo(
            symbol=symbol,
            point=float(info.point),
            digits=int(info.digits),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            stops_level=int(getattr(info, "trade_stops_level", 0)),
            filling_modes=decode_filling_modes(int(getattr(info, "filling_mode", 0))),
            trade_mode=_TRADE_MODE_NAMES.get(int(getattr(info, "trade_mode", -1)), "unknown"),
            spread=int(getattr(info, "spread", 0)) or None,
        )

    def symbol_economics(self, symbol: str) -> dict[str, Any]:
        """Broker economics needed to translate historical price moves into money."""
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise MarketDataError(f"symbol_info({symbol}) failed: {self.mt5.last_error()}")
        return {
            "symbol": symbol,
            "contract_size": float(getattr(info, "trade_contract_size", 0.0)),
            "currency_base": str(getattr(info, "currency_base", "")),
            "currency_profit": str(getattr(info, "currency_profit", "")),
            "currency_margin": str(getattr(info, "currency_margin", "")),
        }

    def account_info(self) -> dict[str, Any]:
        """Who the terminal is logged in as, for the preflight identity check (P-2).

        Returns a plain dict rather than the vendor's named tuple so callers outside
        this module never hold an MT5 object -- the same reason every other method here
        returns an Aureon model. Deliberately omits the balance: preflight's output gets
        pasted into session evidence, and an account balance is not evidence of
        anything the session is testing.
        """
        info = self.mt5.account_info()
        if info is None:
            raise MarketDataError(f"account_info() failed: {self.mt5.last_error()}")
        return {
            "login": int(getattr(info, "login", 0)),
            "server": str(getattr(info, "server", "")),
            "currency": str(getattr(info, "currency", "")),
            "leverage": int(getattr(info, "leverage", 0)),
            "trade_allowed": bool(getattr(info, "trade_allowed", False)),
            # 11A F-3. The integer AND the name: the integer so an operator can check it
            # against MT5's own documentation, the name so nothing downstream has to.
            "trade_mode": getattr(info, "trade_mode", None),
            "account_mode": AccountMode.from_trade_mode(
                getattr(info, "trade_mode", None)
            ).value,
        }

    def terminal_info(self) -> dict[str, Any]:
        """The terminal's own identity: build, name, connection state.

        The build number is what ``docs/MT5_SESSION_CHECKLIST.md`` asks to be recorded
        beside a session's results -- a comparison is evidence about one terminal, and
        without the build it is an anecdote about an unknown one. The installation path
        is left out on purpose: it names a machine, and it goes nowhere useful.
        """
        info = self.mt5.terminal_info()
        if info is None:
            raise MarketDataError(f"terminal_info() failed: {self.mt5.last_error()}")
        return {
            "build": int(getattr(info, "build", 0)),
            "name": str(getattr(info, "name", "")),
            "company": str(getattr(info, "company", "")),
            "connected": bool(getattr(info, "connected", False)),
            "trade_allowed": bool(getattr(info, "trade_allowed", False)),
        }

    def last_tick_time(self, symbol: str) -> datetime | None:
        """When the symbol last ticked, for staleness checks (§10)."""
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None or not getattr(tick, "time", 0):
            return None
        return server_epoch_to_utc(float(tick.time), self.market_tz)

    def now_utc(self) -> datetime:
        """Wall clock in UTC.

        The local clock is acceptable here only because it is read in UTC and used
        solely to decide which bar has closed; every timestamp that gets *stored*
        comes from the broker's own candle and tick times via
        ``server_epoch_to_utc``.
        """
        return utc_now()

    def _timeframe_constant(self, timeframe: Timeframe) -> int:
        attr = _MT5_TIMEFRAME_ATTR.get(timeframe)
        if attr is None:
            raise MarketDataError(f"unsupported timeframe {timeframe}")
        return int(getattr(self.mt5, attr))


def decode_filling_modes(flags: int) -> tuple[FillingMode, ...]:
    """Decode MT5's filling-mode bitmask (§39).

    Phase 6 builds the execution-mode selector from this, so a symbol that does not
    accept FOK never offers it -- the human sees "FOK not supported" instead of a
    broker rejection after confirming.
    """
    modes: list[FillingMode] = []
    if flags & _FILLING_FOK:
        modes.append(FillingMode.FOK)
    if flags & _FILLING_IOC:
        modes.append(FillingMode.IOC)
    return tuple(modes)
