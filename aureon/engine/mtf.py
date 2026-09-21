"""Higher timeframes, built from the M5 bars already in hand (11D).

The observer polls one stream per symbol: M5. Everything above it is **aggregated from those
bars**, and that is the whole design constraint -- no extra broker call, no second poll loop,
no separate cursor. Three reasons, in the order they matter:

* **parity.** Replay feeds the same engine the same M5 bars, so an H1 bias derived from them
  is reproducible from the archive. An H1 bar fetched live from the terminal would not be:
  a broker's own H1 is its own aggregation, and a difference between it and ours would be
  indistinguishable from a bug in either (§82).
* **one cursor.** A second stream is a second thing to seed, a second thing to get wrong on
  restart, and a second place a gap can appear.
* **cost.** Five more timeframes is five more requests a second for data that is a `groupby`
  away.

## A higher bar exists only when every M5 bar inside it has closed

The forming-bar rule, one level up. An H1 bucket holding eleven of its twelve M5 bars has a
high that may still be exceeded and a close that is not the close -- and a bias computed from
it would flip when the twelfth arrived, which is exactly the "detection encoding a price that
never finally existed" that ``MarketEngine``'s grace period exists to prevent. So a bucket is
emitted only when it is **complete and contiguous**: twelve bars for H1, none missing.

That is stricter than "the clock has passed the boundary", deliberately. A missing M5 bar in
the middle of an hour -- a feed hiccup, a restart -- would otherwise produce an H1 bar whose
range is a subset of the real one, and nothing downstream could tell.

## D1 is the BROKER day; everything else is a UTC bucket

The same split the rest of the system makes. ``market_date`` is the broker's trading day
(Athens by default, §8), and a candle at 22:00 UTC belongs to the next one -- so a UTC-bucketed
"daily" bar would split one session across two and neither would be the day anybody traded.
Intraday buckets are anchored to the UTC day, matching ``floor_to_timeframe``, because that is
what the cursor and every existing horizon already use.

## What an ``MtfContext`` is, and what it is not

Per timeframe: the EMA pair at that timeframe's last closed bar, and the bias that follows.
It is **context attached to a detection**, not a signal: no agent gates on it, nothing refuses
a trade because H4 disagrees, and ``mtf_alignment`` is recorded so a review can ask later
whether alignment mattered. Answering that is what the evaluation rules are for; guessing it
now and building a filter on the guess is how an unresearched threshold becomes folklore
(the failure ``symbol_tuning`` exists to document).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from aureon.models.base import MarketTime
from aureon.models.enums import Direction, MtfAlignment, Timeframe, TrendBias
from aureon.models.market import Candle

#: The timeframes built from M5, smallest first.
#:
#: M1 is absent because it cannot be aggregated UP from M5 -- and it is not wanted here
#: anyway: the M1 archive exists for reconstructing a position's excursions offline (§45) and
#: lives in parquet, never in Firestore.
DERIVED: tuple[Timeframe, ...] = (
    Timeframe.M15,
    Timeframe.M30,
    Timeframe.H1,
    Timeframe.H4,
    Timeframe.D1,
)

#: The source. One stream, polled once.
SOURCE = Timeframe.M5

#: How many M5 bars a complete bucket holds, per timeframe. D1 is absent: a broker day's bar
#: count depends on the session, and a weekend or a holiday makes any fixed number wrong.
BARS_PER: dict[Timeframe, int] = {
    timeframe: timeframe.minutes // SOURCE.minutes
    for timeframe in (Timeframe.M15, Timeframe.M30, Timeframe.H1, Timeframe.H4)
}


def bucket_open(moment: datetime, timeframe: Timeframe) -> datetime:
    """The start of ``moment``'s bucket, anchored to the UTC day.

    The same arithmetic as ``floor_to_timeframe``, repeated here rather than imported so
    ``aureon.engine`` does not depend on ``aureon.data`` for a calendar fact. D1 is not
    handled: a daily bar is keyed on the broker date, which is a property of the candle and
    not of a UTC instant.
    """
    if timeframe is Timeframe.D1:
        raise ValueError("a daily bucket is the BROKER date; use market_date")
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((moment - day).total_seconds() // 60)
    return day + timedelta(minutes=(elapsed // timeframe.minutes) * timeframe.minutes)


def aggregate(
    candles: Sequence[Candle], timeframe: Timeframe, *, market_tz: str | None = None
) -> list[Candle]:
    """Closed ``timeframe`` bars built from closed M5 bars.

    Only **complete and contiguous** buckets are emitted -- see the module docstring. An
    incomplete final bucket is simply absent, which is the same answer the M5 stream gives
    about a bar that has not closed.

    Duplicate M5 bars are collapsed and out-of-order input is sorted, because the caller may
    be an observer that overlapped by one candle on restart and a buffer that shifted by one
    bar would change every aggregate above it.
    """
    if timeframe is SOURCE:
        return sorted(_unique(candles), key=lambda c: c.open_time.utc)
    if timeframe not in DERIVED:
        raise ValueError(f"{timeframe.value} is not derived from {SOURCE.value}")

    ordered = sorted(_unique(candles), key=lambda c: c.open_time.utc)
    buckets: dict[datetime | str, list[Candle]] = {}
    for candle in ordered:
        if candle.timeframe is not SOURCE:
            raise ValueError(
                f"aggregate() takes {SOURCE.value} bars; got {candle.timeframe.value}"
            )
        key = (
            candle.open_time.market_date
            if timeframe is Timeframe.D1
            else bucket_open(candle.open_time.utc, timeframe)
        )
        buckets.setdefault(key, []).append(candle)

    keys = sorted(buckets, key=str)
    out: list[Candle] = []
    for index, key in enumerate(keys):
        members = buckets[key]
        last = index == len(keys) - 1
        if not _complete(members, timeframe, is_last=last):
            continue
        out.append(_merge(members, timeframe, market_tz=market_tz))
    return out


def _unique(candles: Sequence[Candle]) -> list[Candle]:
    """One bar per open time, last one winning.

    Last rather than first: a re-fetched bar is the broker's more considered answer about the
    same minute, and the observer's overlap-by-one on restart is exactly that case.
    """
    seen: dict[datetime, Candle] = {}
    for candle in candles:
        seen[candle.open_time.utc] = candle
    return list(seen.values())


def _complete(
    members: Sequence[Candle], timeframe: Timeframe, *, is_last: bool
) -> bool:
    """Whether this bucket holds every M5 bar it should, with none missing.

    Intraday buckets have a fixed count, so the check is that count plus contiguity: a feed
    hiccup in the middle of an hour would otherwise produce an H1 bar whose range is a subset
    of the real one, and nothing downstream could tell.

    **D1 has no fixed count.** A broker day's bar count depends on the session, and a holiday
    or an early close makes any number wrong. So a daily bar is complete when a LATER day is
    present in the input -- the arrival of the next day is the only evidence available that
    this one has ended. The first version had no such rule and happily emitted the forming
    day, whose close is not the close and whose high may still be exceeded: exactly the
    "price that never finally existed" the M5 grace period exists to prevent, one level up.

    The limit, stated: this cannot tell whether the FIRST day in the input is partial, because
    a day missing its early bars looks identical to a day that opened late. The caller's input
    boundary is the caller's business -- ``market_day_frames`` stores whole archived days for
    that reason -- and the cost of getting it wrong is one bar at the far end of a window,
    where ``frames_from``'s EMA has the least weight.
    """
    if not members:
        return False
    span = timedelta(minutes=SOURCE.minutes)
    contiguous = all(
        later.open_time.utc - earlier.open_time.utc == span
        for earlier, later in zip(members, members[1:], strict=False)
    )
    if timeframe is Timeframe.D1:
        return contiguous and not is_last
    expected = BARS_PER[timeframe]
    if len(members) != expected:
        return False
    return contiguous


def _merge(
    members: Sequence[Candle], timeframe: Timeframe, *, market_tz: str | None
) -> Candle:
    """One higher-timeframe bar from its M5 members.

    Open from the first, close from the last, high and low from the extremes, tick volume
    summed. The summed tick volume is honest arithmetic on a number that is already a count
    of price CHANGES rather than of contracts (11A, F-5) -- adding twelve such counts gives
    the hour's count of price changes, which is what it says and nothing more.
    """
    first, last = members[0], members[-1]
    return Candle(
        symbol=first.symbol,
        timeframe=timeframe,
        open_time=MarketTime.from_utc(
            first.open_time.utc, market_tz or first.open_time.market_tz
        ),
        open=first.open,
        high=max(one.high for one in members),
        low=min(one.low for one in members),
        close=last.close,
        tick_volume=sum(one.tick_volume for one in members),
        real_volume=sum(one.real_volume for one in members),
    )


# ── The context a detection carries ──────────────────────────────────────────


@dataclass(frozen=True)
class Frame:
    """One timeframe's last closed bar and the EMA pair at it."""

    timeframe: Timeframe
    #: The last closed bar's open time, so a reader can see how old the read is.
    at: datetime
    ema_fast: float | None
    ema_slow: float | None
    close: float

    @property
    def bias(self) -> TrendBias:
        """Fast over slow, or SIDEWAYS when either is missing.

        SIDEWAYS on a missing EMA rather than a guess: a timeframe with too few bars to seed
        an EMA has no opinion, and inventing one would make an alignment look stronger than
        the history behind it.
        """
        if self.ema_fast is None or self.ema_slow is None:
            return TrendBias.SIDEWAYS
        if self.ema_fast > self.ema_slow:
            return TrendBias.BULLISH
        if self.ema_fast < self.ema_slow:
            return TrendBias.BEARISH
        return TrendBias.SIDEWAYS


#: The three answers ``alignment`` gives. Aliases for the enum members, so a caller reads
#: ``ALIGNED`` rather than ``MtfAlignment.ALIGNED`` and the stored value is still the enum.
ALIGNED = MtfAlignment.ALIGNED
MIXED = MtfAlignment.MIXED
AGAINST = MtfAlignment.AGAINST


def alignment(frames: Sequence[Frame], direction: Direction | None) -> MtfAlignment:
    """Whether the higher timeframes agree with a detection's direction (11D).

    Three answers and no fourth:

    * ``aligned`` -- every timeframe with an opinion agrees;
    * ``against`` -- every timeframe with an opinion disagrees;
    * ``mixed`` -- anything else, INCLUDING the case where nobody has an opinion.

    A SIDEWAYS timeframe is not counted either way; it is an absence of evidence, and
    treating it as agreement is how "aligned" comes to mean "we could not tell". A detection
    with no direction -- a context signal rather than a directional one -- is always mixed,
    because there is nothing for a bias to agree with.

    Recorded, never gated on. Whether alignment predicts anything is a question for the
    evaluation rules, and building a filter on the assumption that it does would be a
    threshold nobody researched (the failure ``symbol_tuning`` documents at length).
    """
    if direction is None:
        return MIXED
    wanted = TrendBias.BULLISH if direction is Direction.BUY else TrendBias.BEARISH
    opinions = [frame.bias for frame in frames if frame.bias is not TrendBias.SIDEWAYS]
    if not opinions:
        return MIXED
    if all(bias is wanted for bias in opinions):
        return ALIGNED
    if all(bias is not wanted for bias in opinions):
        return AGAINST
    return MIXED


def frames_from(
    candles: Sequence[Candle],
    *,
    fast: int,
    slow: int,
    timeframes: Sequence[Timeframe] = DERIVED,
    market_tz: str | None = None,
) -> list[Frame]:
    """One ``Frame`` per timeframe, from one set of closed M5 bars.

    A timeframe with fewer than ``slow`` aggregated bars is left out entirely rather than
    given a half-seeded EMA: a recursive indicator depends on how much history it was given
    (the reason ``AnalysisEngine`` fixes its window), and an EMA seeded on four bars is not a
    shorter version of the same number.
    """
    import pandas as pd

    from aureon.engine.indicators import ema

    out: list[Frame] = []
    for timeframe in timeframes:
        bars = aggregate(candles, timeframe, market_tz=market_tz)
        if len(bars) < slow:
            continue
        closes = pd.Series([bar.close for bar in bars], dtype="float64")
        fast_line, slow_line = ema(closes, fast), ema(closes, slow)
        out.append(
            Frame(
                timeframe=timeframe,
                at=bars[-1].open_time.utc,
                ema_fast=float(fast_line.iloc[-1]),
                ema_slow=float(slow_line.iloc[-1]),
                close=bars[-1].close,
            )
        )
    return out
