"""Higher timeframes, aggregated from the M5 bars already in hand (11D).

The property the whole file is about: a higher-timeframe bar must be **as final as the M5
bars it was built from**. An H1 bar holding eleven of its twelve M5 bars has a high that may
still be exceeded and a close that is not the close, and a bias computed from it would flip
when the twelfth arrived. That is the same "price that never finally existed" the M5 grace
period exists to prevent, one level up, and it is the failure every test here is pointed at.

The awkward cases, each with its own test:

* an incomplete bucket at the end (the forming hour);
* a bucket with a HOLE in it -- a feed hiccup, a restart -- whose range is a subset of the
  real one and looks perfectly normal;
* a bucket of the right SIZE whose bars are on the wrong grid, which the count cannot see;
* the daily bar, which has no fixed bar count and so needs a different completeness rule;
* the weekend, where a UTC-bucketed "day" would split one session across two.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.engine.mtf import (
    AGAINST,
    ALIGNED,
    BARS_PER,
    DERIVED,
    MIXED,
    SOURCE,
    Frame,
    aggregate,
    alignment,
    bucket_open,
    frames_from,
)
from aureon.models.base import MarketTime
from aureon.models.enums import Direction, Timeframe, TrendBias
from aureon.models.market import Candle

MARKET_TZ = "Europe/Athens"
SYMBOL = "XAUUSD"
#: A Tuesday, on an hour boundary, so every bucket below starts where it looks like it does.
START = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)


def m5(
    count: int,
    *,
    start: datetime = START,
    step: int = 1,
    base: float = 2400.0,
    skip: set[int] | None = None,
) -> list[Candle]:
    """``count`` M5 bars from ``start``, each one point above the last.

    ``skip`` leaves holes, which is how a feed hiccup looks. ``step`` of 2 spaces the bars ten
    minutes apart, so they spread across twice as many buckets.
    """
    out: list[Candle] = []
    for i in range(count):
        if skip and i in skip:
            continue
        opened = start + timedelta(minutes=5 * i * step)
        price = base + i
        out.append(
            Candle(
                symbol=SYMBOL,
                timeframe=SOURCE,
                open_time=MarketTime.from_utc(opened, MARKET_TZ),
                open=price,
                high=price + 0.5,
                low=price - 0.5,
                close=price + 0.2,
                tick_volume=10 + i,
            )
        )
    return out


# ── Buckets ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("timeframe", "expected"),
    [
        (Timeframe.M15, datetime(2026, 9, 15, 8, 15, tzinfo=UTC)),
        (Timeframe.M30, datetime(2026, 9, 15, 8, 0, tzinfo=UTC)),
        (Timeframe.H1, datetime(2026, 9, 15, 8, 0, tzinfo=UTC)),
        (Timeframe.H4, datetime(2026, 9, 15, 8, 0, tzinfo=UTC)),
    ],
)
def test_a_bucket_is_anchored_to_the_utc_day(timeframe, expected) -> None:
    """The same anchoring ``floor_to_timeframe`` uses, so the aggregates line up with the
    cursor and with every existing horizon."""
    assert bucket_open(datetime(2026, 9, 15, 8, 25, tzinfo=UTC), timeframe) == expected


def test_a_daily_bucket_refuses_to_be_a_utc_one() -> None:
    """A UTC-bucketed "day" would split one session across two: the broker day starts at
    midnight in Athens, which is 21:00 or 22:00 UTC."""
    with pytest.raises(ValueError, match="BROKER date"):
        bucket_open(START, Timeframe.D1)


# ── A bar is as final as its parts ───────────────────────────────────────────


def test_twelve_m5_bars_make_one_h1_bar() -> None:
    bars = aggregate(m5(12), Timeframe.H1)
    assert len(bars) == 1
    one = bars[0]
    assert one.timeframe is Timeframe.H1
    assert one.open_time.utc == START
    assert one.open == 2400.0, "the first bar's open"
    assert one.close == pytest.approx(2411.2), "the last bar's close"
    assert one.high == pytest.approx(2411.5), "the highest high"
    assert one.low == pytest.approx(2399.5), "the lowest low"
    assert one.tick_volume == sum(range(10, 22)), "the summed tick count"


def test_an_incomplete_final_bucket_is_absent() -> None:
    """The forming hour. Eleven of twelve bars is not an hour, and a bias computed from it
    would flip when the twelfth arrived."""
    assert aggregate(m5(11), Timeframe.H1) == []
    assert len(aggregate(m5(13), Timeframe.H1)) == 1, "and the complete one still counts"


def test_a_bucket_with_a_HOLE_is_refused() -> None:
    """A feed hiccup in the middle of an hour. Its range is a subset of the real one and
    nothing downstream could tell.

    Caught by the COUNT, not by contiguity -- which is worth saying, because the first version
    of this file claimed the opposite and was wrong. A bucket has exactly twelve five-minute
    slots, so eleven deduplicated bars inside one cannot be contiguous either way, and the
    count is the check that fires. ``test_a_bucket_of_misaligned_bars_is_refused`` is the one
    that exercises contiguity. Found by planting the contiguity check away and watching every
    test still pass.
    """
    holed = m5(12, skip={5})
    assert len(holed) == 11
    assert aggregate(holed, Timeframe.H1) == []


def test_a_bucket_of_misaligned_bars_is_refused() -> None:
    """Twelve bars inside one hour is not twelve CONSECUTIVE five-minute bars.

    The case the count cannot see: bars two minutes apart, twelve of them, all inside the same
    hour. The count is right and the data is not -- a terminal serving a non-standard grid, or
    an aggregation fed the wrong stream -- and contiguity is the only thing that catches it.
    """
    misaligned = [
        one.model_copy(
            update={
                "open_time": MarketTime.from_utc(
                    START + timedelta(minutes=2 * i), MARKET_TZ
                )
            }
        )
        for i, one in enumerate(m5(12))
    ]
    assert len({one.open_time.utc for one in misaligned}) == 12
    assert all(one.open_time.utc < START + timedelta(hours=1) for one in misaligned)
    assert aggregate(misaligned, Timeframe.H1) == []


def test_bars_a_bucket_apart_land_in_different_buckets() -> None:
    """Ten-minute spacing puts six bars in each hour, which the count refuses."""
    assert aggregate(m5(12, step=2), Timeframe.H1) == []


@pytest.mark.parametrize("timeframe", [Timeframe.M15, Timeframe.M30, Timeframe.H1, Timeframe.H4])
def test_every_intraday_timeframe_needs_its_full_count(timeframe) -> None:
    needed = BARS_PER[timeframe]
    assert aggregate(m5(needed - 1), timeframe) == []
    assert len(aggregate(m5(needed), timeframe)) == 1


def test_duplicate_bars_are_collapsed_rather_than_counted_twice() -> None:
    """The observer overlaps by one candle on restart, and a buffer that counted the overlap
    twice would shift every bucket above it."""
    bars = m5(12)
    assert len(aggregate(bars + bars[:3], Timeframe.H1)) == 1


def test_out_of_order_input_is_sorted() -> None:
    bars = m5(12)
    assert aggregate(list(reversed(bars)), Timeframe.H1) == aggregate(bars, Timeframe.H1)


def test_a_higher_timeframe_bar_is_refused_as_input() -> None:
    """Only M5 goes in. Aggregating an already-aggregated bar would double-count silently."""
    h1 = aggregate(m5(12), Timeframe.H1)
    with pytest.raises(ValueError, match="takes M5 bars"):
        aggregate(h1, Timeframe.H4)


def test_m5_passes_through_unchanged() -> None:
    bars = m5(7)
    assert aggregate(bars, SOURCE) == bars


def test_a_timeframe_below_the_source_is_refused() -> None:
    with pytest.raises(ValueError, match="not derived"):
        aggregate(m5(12), Timeframe.M1)


# ── The daily bar ────────────────────────────────────────────────────────────


def two_days() -> list[Candle]:
    """Monday's last hour and Tuesday's first, on the broker clock.

    22:00 UTC is 01:00 in Athens, so the bars from 21:00 UTC onward already belong to the NEXT
    broker day -- which is the whole reason a daily bar is keyed on ``market_date``.
    """
    monday_evening = datetime(2026, 9, 14, 19, 0, tzinfo=UTC)
    return m5(48, start=monday_evening)  # 19:00 to 23:00 UTC, crossing the Athens midnight


def test_a_daily_bar_is_keyed_on_the_broker_date() -> None:
    bars = aggregate(two_days(), Timeframe.D1)
    assert [one.open_time.market_date for one in bars] == ["2026-09-14"], (
        "the completed broker day only; the one still in progress is absent"
    )


def test_the_day_still_in_progress_is_not_emitted() -> None:
    """The flaw the first version had.

    There is no fixed bar count for a day, so the only evidence that one has ended is a bar
    from the NEXT one. Without that rule the forming day was emitted with a close that was
    not the close -- the same failure as the forming hour, and harder to notice because a
    daily bar looks plausible whatever is in it.
    """
    one_day = m5(24, start=datetime(2026, 9, 15, 8, 0, tzinfo=UTC))
    assert {c.open_time.market_date for c in one_day} == {"2026-09-15"}
    assert aggregate(one_day, Timeframe.D1) == [], "one day, still forming, so nothing"


def test_a_day_with_a_hole_is_refused() -> None:
    holed = m5(48, start=datetime(2026, 9, 14, 19, 0, tzinfo=UTC), skip={3})
    assert aggregate(holed, Timeframe.D1) == []


def test_the_weekend_does_not_merge_friday_into_monday() -> None:
    """Two sessions separated by 49 hours are two days, not one long one. A rule that
    grouped by "the last N bars" rather than by broker date would merge them."""
    friday = m5(12, start=datetime(2026, 9, 18, 19, 0, tzinfo=UTC))
    sunday = m5(12, start=datetime(2026, 9, 20, 22, 0, tzinfo=UTC))
    monday = m5(12, start=datetime(2026, 9, 21, 8, 0, tzinfo=UTC))
    dates = {
        one.open_time.market_date for one in aggregate(friday + sunday + monday, Timeframe.D1)
    }
    # Friday's own day completes; Monday's (which Sunday 22:00 UTC already belongs to) is
    # non-contiguous across the gap between the two blocks, so it is refused rather than
    # merged into something spanning the weekend.
    assert "2026-09-18" in dates
    assert len(dates) <= 2


# ── Bias and alignment ───────────────────────────────────────────────────────


def frame(timeframe: Timeframe, *, fast: float | None, slow: float | None) -> Frame:
    return Frame(timeframe=timeframe, at=START, ema_fast=fast, ema_slow=slow, close=2400.0)


def test_a_frame_with_a_missing_ema_has_no_opinion() -> None:
    """SIDEWAYS rather than a guess: a timeframe with too few bars to seed an EMA has no
    view, and inventing one would make an alignment look stronger than its history."""
    assert frame(Timeframe.H1, fast=None, slow=2400.0).bias is TrendBias.SIDEWAYS
    assert frame(Timeframe.H1, fast=2400.0, slow=None).bias is TrendBias.SIDEWAYS


def test_equal_emas_are_sideways_rather_than_either_direction() -> None:
    assert frame(Timeframe.H1, fast=2400.0, slow=2400.0).bias is TrendBias.SIDEWAYS


def test_every_timeframe_agreeing_is_aligned() -> None:
    frames = [frame(tf, fast=2401.0, slow=2399.0) for tf in (Timeframe.M15, Timeframe.H1)]
    assert alignment(frames, Direction.BUY) == ALIGNED
    assert alignment(frames, Direction.SELL) == AGAINST


def test_one_disagreement_is_mixed_not_aligned() -> None:
    frames = [
        frame(Timeframe.M15, fast=2401.0, slow=2399.0),
        frame(Timeframe.H1, fast=2399.0, slow=2401.0),
    ]
    assert alignment(frames, Direction.BUY) == MIXED
    assert alignment(frames, Direction.SELL) == MIXED


def test_a_sideways_timeframe_is_not_counted_as_agreement() -> None:
    """An absence of evidence, not evidence. Counting it as agreement is how "aligned" comes
    to mean "we could not tell"."""
    frames = [
        frame(Timeframe.M15, fast=2401.0, slow=2399.0),
        frame(Timeframe.H1, fast=None, slow=None),
    ]
    assert alignment(frames, Direction.BUY) == ALIGNED, "the one with a view agrees"

    nobody = [frame(Timeframe.M15, fast=None, slow=None)]
    assert alignment(nobody, Direction.BUY) == MIXED, "and nobody having a view is not aligned"


def test_no_frames_at_all_is_mixed() -> None:
    assert alignment([], Direction.BUY) == MIXED


def test_a_detection_with_no_direction_is_always_mixed() -> None:
    """A context signal has nothing for a bias to agree with."""
    frames = [frame(Timeframe.H1, fast=2401.0, slow=2399.0)]
    assert alignment(frames, None) == MIXED


# ── Frames from real bars ────────────────────────────────────────────────────


def test_a_timeframe_with_too_little_history_is_left_out_entirely() -> None:
    """Rather than given a half-seeded EMA. A recursive indicator depends on how much history
    it was given -- the reason ``AnalysisEngine`` fixes its window -- so an EMA seeded on four
    bars is not a shorter version of the same number.
    """
    frames = frames_from(m5(12 * 10), fast=3, slow=5)
    built = {one.timeframe for one in frames}
    assert Timeframe.M15 in built, "ten hours is plenty of M15 bars"
    assert Timeframe.D1 not in built, "and nowhere near enough days"


def test_the_fixture_week_reaches_h1_but_not_d1() -> None:
    """A real consequence, pinned: one week of M5 cannot seed a 50-EMA above H1.

    This is why 11D caches aggregated frames per broker day -- an H4 or D1 bias needs weeks of
    history, and a live observer's buffer holds hours.
    """
    from aureon.data.historical_provider import HistoricalDataProvider
    from tests.conftest import FIXTURE_CSV

    candles = HistoricalDataProvider(FIXTURE_CSV, market_tz=MARKET_TZ).candles
    built = {one.timeframe for one in frames_from(candles, fast=20, slow=50)}
    assert {Timeframe.M15, Timeframe.M30, Timeframe.H1} <= built
    assert Timeframe.H4 not in built and Timeframe.D1 not in built


def test_a_frame_reads_the_last_CLOSED_bar_of_its_timeframe() -> None:
    bars = m5(12 * 8)  # eight whole hours
    frames = frames_from(bars, fast=3, slow=5, timeframes=(Timeframe.H1,))
    assert len(frames) == 1
    assert frames[0].at == START + timedelta(hours=7)
    assert frames[0].close == pytest.approx(
        aggregate(bars, Timeframe.H1)[-1].close
    )


def test_every_derived_timeframe_is_above_the_source() -> None:
    assert all(tf.minutes > SOURCE.minutes for tf in DERIVED)
    assert Timeframe.M1 not in DERIVED, (
        "M1 cannot be aggregated UP from M5, and the M1 archive is parquet-only anyway"
    )
