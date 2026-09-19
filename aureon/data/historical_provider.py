"""File-backed market data for replay and tests (§9).

Reads CSV or Parquet and produces exactly the same ``Candle`` objects the live MT5
provider does, which is the whole point: the parity test (§82) compares detections
from this provider against detections from a live-shaped feed, so any difference in
the data layer would masquerade as an agent bug.

Every candle in a file is by definition already closed, so there is no forming-bar
problem here -- but the half-open range semantics still match the live provider so
the two are interchangeable.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from aureon.data.base_provider import BaseMarketDataProvider, MarketDataError
from aureon.models.base import MarketTime, to_utc
from aureon.models.enums import FillingMode, Timeframe
from aureon.models.market import Candle, QuoteSnapshot, SymbolInfo

# Stand-in metadata for the synthetic fixture, shaped like a real gold symbol.
# Replaced by the broker's own values whenever a caller passes symbol_info.
DEFAULT_SYMBOL_INFO = SymbolInfo(
    symbol="XAUUSD",
    point=0.01,
    digits=2,
    volume_min=0.01,
    volume_max=50.0,
    volume_step=0.01,
    stops_level=50,
    filling_modes=(FillingMode.FOK, FillingMode.IOC),
    trade_mode="full",
    spread=30,
)

REQUIRED_COLUMNS = ("open_time", "open", "high", "low", "close")


class HistoricalDataProvider(BaseMarketDataProvider):
    """Serves candles from a file.

    Loads once and keeps the candles in memory: a week of M5 is a couple of
    thousand rows, and re-reading per query would make replay needlessly slow while
    adding a chance of the file changing mid-run.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        symbol: str = "XAUUSD",
        timeframe: Timeframe = Timeframe.M5,
        market_tz: str = "Europe/Athens",
        symbol_info: SymbolInfo | None = None,
    ) -> None:
        self.path = Path(path)
        self.symbol = symbol
        self.timeframe = timeframe
        self.market_tz = market_tz
        self._symbol_info = symbol_info or DEFAULT_SYMBOL_INFO.model_copy(
            update={"symbol": symbol}
        )
        self._candles: list[Candle] = []
        # Replay drives its own clock; see now_utc().
        self._clock: datetime | None = None

    # ── Loading ───────────────────────────────────────────────────────────────

    def connect(self) -> None:
        self.load()

    def load(self) -> list[Candle]:
        if self._candles:
            return self._candles
        if not self.path.exists():
            raise MarketDataError(f"fixture not found: {self.path}")

        rows = self._read_parquet() if self.path.suffix == ".parquet" else self._read_csv()
        candles = [self._to_candle(row) for row in rows]
        candles.sort(key=lambda c: c.open_time.utc)

        # A duplicated candle open would make the same bar detectable twice and
        # break the parity comparison in a way that is painful to trace back to
        # the file, so reject it at load time.
        seen: set[datetime] = set()
        for candle in candles:
            if candle.open_time.utc in seen:
                raise MarketDataError(
                    f"{self.path}: duplicate candle open_time {candle.open_time.utc.isoformat()}"
                )
            seen.add(candle.open_time.utc)

        self._candles = candles
        return self._candles

    def _read_csv(self) -> list[dict[str, str]]:
        with self.path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise MarketDataError(f"{self.path}: missing columns {missing}")
            return list(reader)

    def _read_parquet(self) -> list[dict[str, object]]:
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - pandas is a core dep
            raise MarketDataError("reading Parquet requires pandas") from exc
        frame = pd.read_parquet(self.path)
        missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
        if missing:
            raise MarketDataError(f"{self.path}: missing columns {missing}")
        return frame.to_dict("records")

    def _to_candle(self, row: dict[str, object]) -> Candle:
        raw_time = row["open_time"]
        if isinstance(raw_time, str):
            opened = datetime.fromisoformat(raw_time)
        elif isinstance(raw_time, datetime):
            opened = raw_time
        else:  # pandas Timestamp and friends
            opened = datetime.fromisoformat(str(raw_time))
        if opened.tzinfo is None:
            # A file without an offset is assumed UTC and said so loudly in the
            # docs, rather than silently taken as the host's local zone.
            opened = opened.replace(tzinfo=UTC)

        return Candle(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_time=MarketTime.from_utc(opened, self.market_tz),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            tick_volume=int(float(row.get("tick_volume") or 0)),
            real_volume=int(float(row.get("real_volume") or 0)),
        )

    # ── BaseMarketDataProvider ────────────────────────────────────────────────

    def get_closed_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        from_utc: datetime,
        to_utc: datetime,
    ) -> list[Candle]:
        self._check(symbol, timeframe)
        start, end = to_utc_bound(from_utc), to_utc_bound(to_utc)
        return [c for c in self.load() if start <= c.open_time.utc < end]

    def get_quote(self, symbol: str) -> QuoteSnapshot:
        """A quote derived from the last candle's close.

        Historical files carry no bid/ask, so the spread is synthesised from the
        symbol's reported spread in points. Callers that need a real quote must use
        a live provider; this exists so replay can exercise the same code paths.
        """
        self._check(symbol, self.timeframe)
        candles = self.load()
        if not candles:
            raise MarketDataError(f"{self.path}: no candles to derive a quote from")
        last = candles[-1]
        half = (self._symbol_info.spread or 0) * self._symbol_info.point / 2
        return QuoteSnapshot(
            symbol=symbol,
            bid=round(last.close - half, self._symbol_info.digits),
            ask=round(last.close + half, self._symbol_info.digits),
            captured_at=last.close_time,
            point=self._symbol_info.point,
        )

    def symbol_info(self, symbol: str) -> SymbolInfo:
        self._check(symbol, self.timeframe)
        return self._symbol_info

    def now_utc(self) -> datetime:
        """Replay's clock, if one was set; otherwise the last candle's close.

        Never the wall clock: a replay over last week's data with a live "now"
        would classify every candle as ancient and could make gap logic fire on
        data that is perfectly contiguous.
        """
        if self._clock is not None:
            return self._clock
        candles = self.load()
        return candles[-1].close_time if candles else datetime.now(UTC)

    def set_clock(self, moment: datetime | None) -> None:
        """Pin the provider's notion of now, for deterministic replay."""
        self._clock = to_utc(moment) if moment is not None else None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _check(self, symbol: str, timeframe: Timeframe) -> None:
        if symbol != self.symbol:
            raise MarketDataError(f"{self.path} serves {self.symbol}, not {symbol}")
        if timeframe != self.timeframe:
            raise MarketDataError(f"{self.path} serves {self.timeframe}, not {timeframe}")

    @property
    def candles(self) -> list[Candle]:
        return self.load()

    def __len__(self) -> int:
        return len(self.load())


def to_utc_bound(value: datetime) -> datetime:
    """Normalise a range bound to UTC, rejecting naive input."""
    return to_utc(value)
