"""Turning a broker day's M5 bars into the two documents that describe it (11D).

Pure functions, taking bars and returning models. The observer calls them at a day rollover
and the repository writes what comes back; nothing here touches Firestore, so every awkward
case -- an empty day, a day still trading, a day with more bars than the cap -- is a unit test
rather than an emulator run.

## Complete means "a later day has arrived"

There is no fixed bar count for a broker day: a holiday, an early close or a late open makes
any number wrong. The only evidence available that a day has ended is a bar belonging to a
LATER one, which is exactly the rule ``mtf.aggregate`` uses for its daily bars. So
``complete`` is passed in by the caller, who is the one holding the next bar, rather than
guessed at here from a clock -- a clock would call a day complete at midnight whether or not
the bars arrived, and the whole point of the flag is that somebody downstream is about to build
an EMA out of it.
"""

from __future__ import annotations

from collections.abc import Sequence
import math

import pandas as pd

from aureon.engine.indicators import ema, rsi
from aureon.engine.mtf import aggregate
from aureon.models.enums import Timeframe
from aureon.models.market import Candle
from aureon.models.market_day import (
    CACHED,
    MAX_BARS,
    FrameBar,
    MarketDay,
    MarketDayFrame,
)


def bars_of(candles: Sequence[Candle]) -> list[FrameBar]:
    """The storage shape, ordered by open time and deduplicated.

    Deduplicated because the observer overlaps by one candle on restart, and the model refuses
    a duplicate outright -- better to collapse it here, where the reason is visible, than to
    fail a whole day's write on a bar that was simply seen twice.
    """
    seen: dict[object, FrameBar] = {}
    for candle in candles:
        seen[candle.open_time.utc] = FrameBar(
            at=candle.open_time.utc,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            tick_volume=candle.tick_volume,
        )
    return [seen[key] for key in sorted(seen)]


def build_frame(
    symbol: str,
    market_date: str,
    timeframe: Timeframe,
    candles: Sequence[Candle],
    *,
    market_tz: str,
    complete: bool,
) -> MarketDayFrame:
    """One timeframe's bars for one day.

    ``candles`` are this day's M5 bars; anything above M5 is aggregated from them here rather
    than fetched, for the reasons in ``aureon/engine/mtf.py``. A timeframe whose buckets are
    incomplete simply yields fewer bars -- the last partial hour of a trading day is absent,
    which is correct: its close is not its close.
    """
    bars = bars_of(
        candles if timeframe is Timeframe.M5 else aggregate(candles, timeframe)
    )
    truncated = len(bars) > MAX_BARS
    if truncated:
        # The FIRST bars are kept, not the last. A day is read forwards, and a truncated tail
        # is visible as a day that stops early; a truncated head would silently move the day's
        # open, which is a number reviews and charts both quote.
        bars = bars[:MAX_BARS]
    return MarketDayFrame(
        symbol=symbol.upper(),
        market_date=market_date,
        timeframe=timeframe,
        market_tz=market_tz,
        bars=tuple(bars),
        truncated=truncated,
        complete=complete,
    )



def build_live_analysis_frame(
    symbol: str,
    market_date: str,
    candles: Sequence[Candle],
    *,
    market_tz: str,
    ema_fast_period: int,
    ema_slow_period: int,
    rsi_period: int = 14,
) -> MarketDayFrame:
    """Current-day M5 chart frame with observer-computed EMA/RSI series.

    The values are computed here in the observer process and stored with the bars. Discord and
    the chart renderer only display them; neither has to re-run strategy indicators.
    """
    ordered = sorted(candles, key=lambda one: one.open_time.utc)
    if not ordered:
        return MarketDayFrame(
            symbol=symbol.upper(),
            market_date=market_date,
            timeframe=Timeframe.M5,
            market_tz=market_tz,
            complete=False,
        )

    closes = pd.Series([one.close for one in ordered], dtype="float64")
    fast = ema(closes, ema_fast_period)
    slow = ema(closes, ema_slow_period)
    strength = rsi(closes, rsi_period)

    def clean(value: object) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(number) else number

    bars = tuple(
        FrameBar(
            at=candle.open_time.utc,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            tick_volume=candle.tick_volume,
            ema_fast=clean(fast.iloc[index]),
            ema_slow=clean(slow.iloc[index]),
            rsi=clean(strength.iloc[index]),
        )
        for index, candle in enumerate(ordered)
    )
    return MarketDayFrame(
        symbol=symbol.upper(),
        market_date=market_date,
        timeframe=Timeframe.M5,
        market_tz=market_tz,
        bars=bars,
        truncated=False,
        complete=False,
    )



def build_day(
    symbol: str,
    market_date: str,
    candles: Sequence[Candle],
    *,
    market_tz: str,
    complete: bool,
    frames: Sequence[Timeframe] = CACHED,
) -> MarketDay:
    """The day's shape, from its own M5 bars.

    The OHLC is computed from the bars rather than copied from a provider's daily candle. Same
    reason the higher timeframes are aggregated: a broker's own daily bar is its own
    aggregation, and a difference between it and ours would be indistinguishable from a bug in
    either. This way the day and every frame under it are the same arithmetic over the same
    bars (§82).
    """
    ordered = bars_of(candles)
    if not ordered:
        return MarketDay(
            symbol=symbol.upper(),
            market_date=market_date,
            market_tz=market_tz,
            complete=complete,
            frames=(),
        )
    return MarketDay(
        symbol=symbol.upper(),
        market_date=market_date,
        market_tz=market_tz,
        open=ordered[0].open,
        high=max(bar.high for bar in ordered),
        low=min(bar.low for bar in ordered),
        close=ordered[-1].close,
        bars=len(ordered),
        tick_volume=sum(bar.tick_volume for bar in ordered),
        first_bar_at=ordered[0].at,
        last_bar_at=ordered[-1].at,
        complete=complete,
        frames=tuple(frames),
    )


def candles_from(frames: Sequence[MarketDayFrame]) -> list[Candle]:
    """Stored frames back into ``Candle`` models, oldest first.

    What makes an H4 or D1 bias possible at all: those need weeks of history and a live
    observer's buffer holds hours, so the frames are read back and re-aggregated. Only M5
    frames are accepted, because ``mtf.aggregate`` takes M5 and re-aggregating an already
    aggregated bar would double-count silently -- it refuses one, but refusing it here names
    the reason.
    """
    from aureon.models.base import MarketTime

    out: list[Candle] = []
    for frame in frames:
        if frame.timeframe is not Timeframe.M5:
            raise ValueError(
                f"candles_from() takes M5 frames; got {frame.timeframe.value} for "
                f"{frame.symbol} {frame.market_date}"
            )
        for bar in frame.bars:
            out.append(
                Candle(
                    symbol=frame.symbol,
                    timeframe=Timeframe.M5,
                    open_time=MarketTime.from_utc(bar.at, frame.market_tz),
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    tick_volume=bar.tick_volume,
                )
            )
    return sorted(out, key=lambda one: one.open_time.utc)
