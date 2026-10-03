"""Normalize cTrader market data into Aureon's broker-neutral contracts."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from aureon.data.base_provider import BaseMarketDataProvider, MarketDataError, floor_to_timeframe
from aureon.models.base import MarketTime, to_utc, utc_now
from aureon.models.enums import FillingMode, Timeframe
from aureon.models.market import Candle, QuoteSnapshot, SymbolInfo
from aureon.providers.ctrader.transport import CTraderTransport


class CTraderDataProvider(BaseMarketDataProvider):
    """Read-only cTrader adapter. It contains no execution methods."""

    def __init__(
        self,
        *,
        transport: CTraderTransport,
        market_tz: str,
        candle_grace_seconds: float = 2.0,
    ) -> None:
        self.transport = transport
        self.market_tz = market_tz
        self.candle_grace_seconds = candle_grace_seconds

    def connect(self) -> None:
        self.transport.connect()

    def close(self) -> None:
        self.transport.close()

    def get_closed_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        from_utc: datetime,
        to_utc: datetime,
    ) -> list[Candle]:
        rows = self.transport.candles(symbol, timeframe, to_utc=to_utc, from_utc=from_utc)
        cutoff = floor_to_timeframe(
            self.now_utc() - timedelta(seconds=self.candle_grace_seconds), timeframe
        ) - timedelta(minutes=timeframe.minutes)
        candles: list[Candle] = []
        for row in rows:
            opened = _timestamp(row.get("open_time") or row.get("timestamp"))
            if opened is None:
                raise MarketDataError(f"cTrader candle for {symbol} has no timestamp")
            if opened < to_utc(from_utc) or opened >= to_utc(to_utc) or opened > cutoff:
                continue
            candles.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=MarketTime.from_utc(opened, self.market_tz),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    tick_volume=int(row.get("tick_volume") or row.get("volume") or 0),
                    real_volume=int(row.get("real_volume") or 0),
                    spread=(int(row["spread"]) if row.get("spread") is not None else None),
                )
            )
        candles.sort(key=lambda item: item.open_time.utc)
        return candles

    def get_quote(self, symbol: str) -> QuoteSnapshot:
        row = self.transport.quote(symbol)
        captured = _timestamp(row.get("captured_at") or row.get("timestamp")) or utc_now()
        info = self.symbol_info(symbol)
        return QuoteSnapshot(
            symbol=symbol,
            bid=float(row["bid"]),
            ask=float(row["ask"]),
            captured_at=captured,
            point=info.point,
        )

    def symbol_info(self, symbol: str) -> SymbolInfo:
        row = self.transport.symbol(symbol)
        return SymbolInfo(
            symbol=symbol,
            point=float(row["point"]),
            digits=int(row["digits"]),
            volume_min=float(row.get("volume_min") or 0.0),
            volume_max=float(row.get("volume_max") or 0.0),
            volume_step=float(row.get("volume_step") or 0.0),
            stops_level=int(row.get("stops_level") or 0),
            filling_modes=_filling_modes(row.get("filling_modes")),
            trade_mode=str(row.get("trade_mode") or "unknown"),
            spread=(int(row["spread"]) if row.get("spread") is not None else None),
        )

    def last_tick_time(self, symbol: str) -> datetime | None:
        row = self.transport.quote(symbol)
        return _timestamp(row.get("captured_at") or row.get("timestamp"))

    def now_utc(self) -> datetime:
        return utc_now()


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, (int, float)):
        # Accept seconds or milliseconds at the adapter boundary.
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=utc_now().tzinfo)
    try:
        return to_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError as exc:
        raise MarketDataError(f"invalid cTrader timestamp: {value!r}") from exc


def _filling_modes(value: Any) -> tuple[FillingMode, ...]:
    values = value if isinstance(value, (list, tuple, set)) else ()
    modes: list[FillingMode] = []
    for raw in values:
        name = str(raw).upper()
        if name == "FOK":
            modes.append(FillingMode.FOK)
        elif name == "IOC":
            modes.append(FillingMode.IOC)
    return tuple(modes)
