"""One broker day, and the bars it was made of (11D).

Two documents per symbol per day, and they answer different questions.

``MarketDay`` is the day's shape: its open, high, low and close, how many bars it held, and
whether it is finished. Small, one read, and the thing a review or a status screen wants when
it asks "what did Tuesday do".

``MarketDayFrame`` is the bars themselves, one document per timeframe, so a process with no
broker connection can rebuild a chart or a higher-timeframe bias without a terminal. That is
the point of the whole cache: ``aureon/engine/mtf.py`` derives H4 and D1 from M5, an H4
EMA(50) needs two hundred hours of history, and a live observer's buffer holds a few. Reading
the stored frames is how a bias above H1 becomes possible at all.

## M1 is not here, and never will be

Tick data never goes to Firestore (CLAUDE.md), and M1 is the closest thing to it this system
keeps. It exists for one job -- reconstructing a position's excursions for a stretch the
monitor was down (§45) -- and lives in parquet, where a day is one file and a week is a
directory. A day of M1 is 1440 bars per symbol; storing it here would be roughly a document a
day of data nothing reads twice.

## Why the bars are capped

A Firestore document is limited to about a megabyte, and a full day of M5 is 288 bars -- well
inside it, but a bad aggregation or a misconfigured timeframe is not, and a write that fails
at the size limit fails the whole day. So the list is capped and ``truncated`` says so, which
turns a hard failure into a visible partial one. The cap is deliberately far above a normal
day: hitting it means something is wrong, not that the market was busy.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from aureon.models.base import AureonDocument, AureonModel, UtcDatetime
from aureon.models.enums import Timeframe

#: The timeframes cached in Firestore. M5 because it is the source of everything and what a
#: chart wants; M15 and H1 because they are what a human reads a day on.
#:
#: M30, H4 and D1 are deliberately absent: each is derivable from the stored H1 or M5 by
#: ``mtf.aggregate``, and a second stored copy is a second thing that can disagree with the
#: first. The rule is the same one §59 applies to indicators -- store the source, derive the
#: rest, never both.
CACHED: tuple[Timeframe, ...] = (Timeframe.M5, Timeframe.M15, Timeframe.H1)

#: How many bars one frame document will hold. Far above a normal day (288 M5 bars): hitting
#: it means something is wrong with the aggregation, not that the market was busy.
MAX_BARS = 600


class FrameBar(AureonModel):
    """One bar, flattened for storage.

    A bare shape rather than ``Candle``: a stored bar needs no ``MarketTime`` (the day's
    timezone is on the parent document, once) and no symbol or timeframe (both are in the
    document id). Repeating them on 288 bars would triple the document for nothing.
    """

    model_config = ConfigDict(extra="forbid")

    #: The bar's open, in UTC. The only timestamp, because everything else about when it was
    #: is derivable from it and the parent's ``market_tz``.
    at: UtcDatetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = Field(default=0, ge=0)
    # Optional observer-computed presentation context for the LIVE M5 chart. Completed
    # historical frames may legitimately omit these values.
    ema_fast: float | None = None
    ema_slow: float | None = None
    rsi: float | None = None


class MarketDayFrame(AureonDocument):
    """One symbol's bars for one broker day at one timeframe.

    Stored at ``{prefix}_market_day_frames/{symbol}_{market_date}_{timeframe}``.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    market_date: str
    timeframe: Timeframe
    market_tz: str
    bars: tuple[FrameBar, ...] = ()
    #: True when the day held more bars than ``MAX_BARS``. A visible partial rather than a
    #: failed write -- see the module docstring.
    truncated: bool = False
    #: False while the day is still trading. A frame for a day in progress is useful (a chart
    #: wants it) and must never be mistaken for the finished article: an aggregation that read
    #: an incomplete day as a complete one would produce a daily bar whose close is not the
    #: close, which is the failure ``mtf._complete`` exists to prevent.
    complete: bool = False
    updated_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _bars_are_ordered_and_within_the_cap(self) -> MarketDayFrame:
        if len(self.bars) > MAX_BARS:
            raise ValueError(
                f"{self.symbol} {self.market_date} {self.timeframe.value}: "
                f"{len(self.bars)} bars exceeds the {MAX_BARS} cap"
            )
        times = [bar.at for bar in self.bars]
        if times != sorted(times):
            # Out of order is not a cosmetic problem: every consumer takes the LAST bar as the
            # most recent, and an unsorted list makes that silently the wrong one.
            raise ValueError(
                f"{self.symbol} {self.market_date} {self.timeframe.value}: bars out of order"
            )
        if len(set(times)) != len(times):
            raise ValueError(
                f"{self.symbol} {self.market_date} {self.timeframe.value}: duplicate bars"
            )
        return self


class MarketDay(AureonDocument):
    """One symbol's broker day, in one small document.

    Stored at ``{prefix}_market_days/{symbol}_{market_date}``. The broker date, not the UTC
    one: a candle at 22:00 UTC belongs to the next trading day in Athens, and filing it under
    the UTC date would split one session across two documents so that neither was the day
    anybody traded (§8).
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    market_date: str
    market_tz: str
    #: The day's shape, from its own M5 bars. ``None`` before any bar has arrived.
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    bars: int = Field(default=0, ge=0)
    tick_volume: int = Field(default=0, ge=0)
    first_bar_at: UtcDatetime | None = None
    last_bar_at: UtcDatetime | None = None
    #: False while the day is still trading. Set when a bar from a LATER broker day arrives,
    #: which is the only evidence available that this one has ended -- there is no fixed bar
    #: count for a day, because a holiday or an early close makes any number wrong.
    complete: bool = False
    #: Which timeframes have a stored frame document. A reader can tell "no H1 frame was
    #: written" from "the H1 frame is empty" without a second read that finds nothing.
    frames: tuple[Timeframe, ...] = ()
    updated_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _a_day_with_bars_has_a_shape(self) -> MarketDay:
        if self.bars and None in (self.open, self.high, self.low, self.close):
            raise ValueError(
                f"{self.symbol} {self.market_date}: {self.bars} bars but no OHLC"
            )
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError(f"{self.symbol} {self.market_date}: high below low")
        return self
