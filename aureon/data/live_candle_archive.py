"""Persisting the closed candles MT5 actually served (§82).

The replay/live parity test proves the ENGINE is deterministic given the same candles. It
cannot prove the two paths were given the same candles, because the live path's input only
ever existed in memory. That is the gap this closes: the observer writes every closed
candle it processed to ``data/live_candles/{symbol}_{tf}_{date}.parquet``, and
``scripts/compare_live_vs_replay.py`` replays exactly those bytes and diffs the result
against what was stored.

Without this, a live session that produced different detections from a replay would leave
no way to tell whether the engine drifted or the broker simply served different candles --
and those two have completely different fixes.

## Why parquet, and why one file per day

Parquet because a day of M5 candles is a few hundred rows with a typed schema, and a CSV
round trip turns a float into a string and back, which is exactly where a comparison
acquires a difference that was never in the data. One file per broker day because that is
the unit the comparison runs on, and because an append-only daily file can be written
without ever rewriting history.

Tick data is still never stored. A closed candle is not a tick: it is the same bounded,
final record the detections are derived from.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd

from aureon.models.base import MarketTime
from aureon.models.enums import Timeframe
from aureon.models.market import Candle

log = logging.getLogger(__name__)

DEFAULT_ARCHIVE_DIR = Path("data/live_candles")

#: The stored schema. Fixed and ordered, so a file written by one version reads in
#: another and a comparison never has to guess a column's meaning.
COLUMNS: tuple[str, ...] = (
    "open_time_utc",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "real_volume",
)


def archive_path(
    symbol: str, timeframe: Timeframe, market_date: str, *, root: Path | None = None
) -> Path:
    """``{root}/{symbol}_{tf}_{date}.parquet``, on the BROKER date.

    The broker date, not the UTC one: a candle at 22:00 UTC belongs to the next trading
    day in Athens, and filing it under the UTC date would split one session across two
    files -- so a comparison for "Monday" would silently miss its first three hours.
    """
    directory = root or DEFAULT_ARCHIVE_DIR
    return directory / f"{symbol}_{timeframe.value}_{market_date}.parquet"


class LiveCandleArchive:
    """Appends closed candles to one parquet file per symbol, timeframe and broker day.

    Buffered in memory and flushed on day rollover and on shutdown. Buffering because
    rewriting a parquet file on every candle would cost more than the archive is worth;
    flushing on rollover because that is the moment the file is complete and will never
    grow again.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or DEFAULT_ARCHIVE_DIR
        self._buffers: dict[tuple[str, Timeframe, str], list[Candle]] = {}

    def add(self, candle: Candle) -> None:
        """Buffer one closed candle, flushing the previous day when the date rolls."""
        market_date = candle.open_time.market_date
        key = (candle.symbol, candle.timeframe, market_date)

        # Any buffered day for this symbol/timeframe other than the current one is
        # finished: candles arrive in order, so a new date means the old one is closed.
        for existing in [
            k
            for k in self._buffers
            if k[0] == candle.symbol and k[1] == candle.timeframe and k[2] != market_date
        ]:
            self.flush(*existing)

        self._buffers.setdefault(key, []).append(candle)

    def flush(
        self, symbol: str, timeframe: Timeframe, market_date: str
    ) -> Path | None:
        """Write one buffered day. Merges with anything already on disk.

        Merging rather than overwriting, because a restart mid-day would otherwise
        truncate the morning -- and a comparison run against a truncated file would
        report every missing morning detection as a live/replay mismatch.
        """
        key = (symbol, timeframe, market_date)
        candles = self._buffers.pop(key, [])
        if not candles:
            return None

        path = archive_path(symbol, timeframe, market_date, root=self.root)
        path.parent.mkdir(parents=True, exist_ok=True)

        frame = to_frame(candles)
        if path.exists():
            try:
                existing = pd.read_parquet(path)
                frame = (
                    pd.concat([existing, frame])
                    .drop_duplicates(subset=["open_time_utc"], keep="last")
                    .sort_values("open_time_utc")
                    .reset_index(drop=True)
                )
            except Exception:  # noqa: BLE001 - a corrupt archive must not stop observing
                log.exception("could not merge %s; writing the buffer alone", path)

        frame.to_parquet(path, index=False)
        log.info("archived %d candle(s) to %s", len(frame), path)
        return path

    def flush_all(self) -> list[Path]:
        """Flush every buffered day. Called on shutdown."""
        written: list[Path] = []
        for key in list(self._buffers):
            path = self.flush(*key)
            if path is not None:
                written.append(path)
        return written

    def buffered(self) -> int:
        return sum(len(v) for v in self._buffers.values())


def to_frame(candles: list[Candle]) -> pd.DataFrame:
    """Candles as the stored schema, sorted and de-duplicated by open time."""
    frame = pd.DataFrame(
        {
            "open_time_utc": [c.open_time.utc for c in candles],
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "tick_volume": [c.tick_volume for c in candles],
            "real_volume": [c.real_volume for c in candles],
        },
        columns=list(COLUMNS),
    )
    return (
        frame.drop_duplicates(subset=["open_time_utc"], keep="last")
        .sort_values("open_time_utc")
        .reset_index(drop=True)
    )


def read_archive(
    symbol: str,
    timeframe: Timeframe,
    market_date: str | date,
    *,
    market_tz: str,
    root: Path | None = None,
) -> list[Candle]:
    """Read one archived day back as ``Candle`` models.

    Returns them in the same order and with the same values the observer processed, which
    is the whole point: the comparison replays these, not a fresh broker fetch, so a
    difference cannot be the broker having changed its mind.
    """
    day = market_date if isinstance(market_date, str) else market_date.isoformat()
    path = archive_path(symbol, timeframe, day, root=root)
    if not path.exists():
        raise FileNotFoundError(
            f"no live-candle archive at {path}. The observer writes these as it runs; "
            "a comparison needs the session to have been recorded."
        )

    frame = pd.read_parquet(path).sort_values("open_time_utc").reset_index(drop=True)
    return [
        Candle(
            symbol=symbol,
            timeframe=timeframe,
            open_time=MarketTime.from_utc(row.open_time_utc.to_pydatetime(), market_tz),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            tick_volume=int(row.tick_volume),
            real_volume=int(row.real_volume),
        )
        for row in frame.itertuples()
    ]
