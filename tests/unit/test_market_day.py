"""One broker day, cached, and the higher-timeframe context a detection carries (11D).

Two things are under test and they share a reason for existing.

**The day cache** is what makes an H4 or D1 bias possible at all: those need weeks of history
and a live observer's buffer holds hours, so each finished broker day's bars are stored and
read back. The failure to guard against is a day that is *not* finished being read as though it
were -- its close is not the close, and a daily bar built from it would be a lie about the day
that nothing downstream could detect.

**The context on a detection** has to be attached from bars that had already closed, and has to
be identical for every agent that fired on the same candle. It is context and never a signal:
no agent reads it, nothing gates on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.models.base import MarketTime
from aureon.models.detection import Detection
from aureon.models.enums import Direction, MtfAlignment, Timeframe, TrendBias
from aureon.models.market import Candle
from aureon.models.market_day import (
    CACHED,
    MAX_BARS,
    FrameBar,
    MarketDay,
    MarketDayFrame,
)
from aureon.models.mtf import MtfContext, TimeframeRead
from aureon.services.market_day_builder import (
    build_day,
    build_frame,
    candles_from,
)

MARKET_TZ = "Europe/Athens"
SYMBOL = "XAUUSD"
DAY = "2026-09-15"
START = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)


def m5(count: int, *, start: datetime = START, base: float = 2400.0) -> list[Candle]:
    """``count`` M5 bars that rise to a peak and fall back.

    Deliberately NOT monotonic. A rising series makes the day's high equal to its LAST bar's
    high and its low equal to its FIRST bar's low, so a builder that took either end instead
    of the extreme would pass every assertion -- which is exactly what planting
    ``high=ordered[-1].high`` proved.
    """
    out: list[Candle] = []
    for i in range(count):
        # Four properties, each of which a plant proved necessary:
        #   * the high is strictly INSIDE, so `ordered[-1].high` is not the day's high;
        #   * the low is strictly inside, so `ordered[0].low` is not the day's low;
        #   * the two ends DIFFER, so first and last are not interchangeable -- with both
        #     ends equal, taking the open from the last bar and the close from the first
        #     passed every assertion.
        # A plain rise-and-fall gives none of the last two.
        if i == 0:
            rung = 4.0
        elif i == count - 1:
            rung = 6.0
        elif i % 2:
            rung = 0.0
        else:
            rung = 10.0
        price = base + rung
        out.append(
            Candle(
                symbol=SYMBOL,
                timeframe=Timeframe.M5,
                open_time=MarketTime.from_utc(start + timedelta(minutes=5 * i), MARKET_TZ),
                open=price,
                high=price + 0.5,
                low=price - 0.5,
                close=price + 0.2,
                tick_volume=10 + i,
            )
        )
    return out


# ── The day's shape ──────────────────────────────────────────────────────────


def test_a_days_ohlc_is_computed_from_its_own_bars() -> None:
    """Not copied from a provider's daily candle. A broker's own daily bar is its own
    aggregation, and a difference between it and ours would be indistinguishable from a bug in
    either -- the same reason the higher timeframes are aggregated rather than fetched (§82)."""
    bars = m5(12)
    day = build_day(SYMBOL, DAY, bars, market_tz=MARKET_TZ, complete=True)
    assert day.bars == 12
    assert day.open == bars[0].open, "the FIRST bar's open"
    assert day.close == bars[-1].close, "the LAST bar's close"
    assert day.high == max(one.high for one in bars)
    assert day.low == min(one.low for one in bars)
    assert day.tick_volume == sum(range(10, 22))
    assert day.first_bar_at == START
    assert day.last_bar_at == START + timedelta(minutes=55)

    # The assertions above are only worth anything if the extremes are not at the ends. A
    # monotonic fixture makes max(high) == bars[-1].high and min(low) == bars[0].low, and a
    # builder that took either end would pass -- which is what planting one proved.
    assert day.high != bars[-1].high and day.high != bars[0].high
    assert day.low != bars[0].low and day.low != bars[-1].low


def test_a_day_with_no_bars_has_no_shape_rather_than_zeros() -> None:
    """A zero is a measurement and an absence is not one. A day whose OHLC read 0.0 would
    show up on a chart as a crash."""
    day = build_day(SYMBOL, DAY, [], market_tz=MARKET_TZ, complete=False)
    assert day.bars == 0
    assert (day.open, day.high, day.low, day.close) == (None, None, None, None)
    assert day.frames == ()


def test_a_day_claiming_bars_without_a_shape_is_refused() -> None:
    with pytest.raises(ValueError, match="bars but no OHLC"):
        MarketDay(symbol=SYMBOL, market_date=DAY, market_tz=MARKET_TZ, bars=3)


def test_a_day_whose_high_is_below_its_low_is_refused() -> None:
    with pytest.raises(ValueError, match="high below low"):
        MarketDay(
            symbol=SYMBOL,
            market_date=DAY,
            market_tz=MARKET_TZ,
            open=1.0,
            high=1.0,
            low=2.0,
            close=1.5,
            bars=1,
        )


def test_the_symbol_is_stored_upper_case() -> None:
    """The document id is built from it, and two casings would be two documents for one day."""
    day = build_day("xauusd", DAY, m5(3), market_tz=MARKET_TZ, complete=True)
    assert day.symbol == "XAUUSD"


# ── The frames ───────────────────────────────────────────────────────────────


def test_an_m5_frame_holds_the_bars_as_given() -> None:
    frame = build_frame(SYMBOL, DAY, Timeframe.M5, m5(12), market_tz=MARKET_TZ, complete=True)
    assert len(frame.bars) == 12
    assert frame.bars[0].at == START
    assert not frame.truncated


def test_a_higher_frame_is_aggregated_rather_than_fetched() -> None:
    bars = m5(24)
    frame = build_frame(SYMBOL, DAY, Timeframe.H1, bars, market_tz=MARKET_TZ, complete=True)
    assert len(frame.bars) == 2, "two whole hours"
    assert frame.bars[0].open == bars[0].open
    assert frame.bars[0].close == bars[11].close, "the twelfth bar closes the first hour"
    assert frame.bars[0].high == max(one.high for one in bars[:12])
    assert frame.bars[0].low == min(one.low for one in bars[:12])


def test_an_incomplete_final_hour_is_absent_from_the_frame() -> None:
    """The same rule one level down: an hour holding eleven of its twelve bars has a close
    that is not the close."""
    frame = build_frame(SYMBOL, DAY, Timeframe.H1, m5(23), market_tz=MARKET_TZ, complete=True)
    assert len(frame.bars) == 1


def test_only_the_three_cached_timeframes_are_stored() -> None:
    """M30, H4 and D1 are derivable from the stored M5 or H1, and a second stored copy is a
    second thing that can disagree with the first."""
    assert CACHED == (Timeframe.M5, Timeframe.M15, Timeframe.H1)
    assert Timeframe.M1 not in CACHED, "tick-scale data never goes to Firestore"


def test_bars_over_the_cap_are_truncated_and_say_so() -> None:
    """A visible partial rather than a failed write at Firestore's document limit. Hitting
    this means the aggregation is wrong, not that the market was busy."""
    frame = build_frame(
        SYMBOL, DAY, Timeframe.M5, m5(MAX_BARS + 10), market_tz=MARKET_TZ, complete=True
    )
    assert frame.truncated
    assert len(frame.bars) == MAX_BARS
    assert frame.bars[0].at == START, "the day's OPEN is kept; a truncated tail is visible"


def test_a_frame_over_the_cap_is_refused_by_the_model() -> None:
    """The builder truncates; a hand-built document cannot smuggle one past."""
    bars = tuple(
        FrameBar(at=START + timedelta(minutes=5 * i), open=1, high=1, low=1, close=1)
        for i in range(MAX_BARS + 1)
    )
    with pytest.raises(ValueError, match="exceeds the"):
        MarketDayFrame(
            symbol=SYMBOL,
            market_date=DAY,
            timeframe=Timeframe.M5,
            market_tz=MARKET_TZ,
            bars=bars,
        )


def test_unordered_bars_are_refused() -> None:
    """Every consumer takes the LAST bar as the most recent, and an unsorted list makes that
    silently the wrong one."""
    bars = (
        FrameBar(at=START + timedelta(minutes=5), open=1, high=1, low=1, close=1),
        FrameBar(at=START, open=1, high=1, low=1, close=1),
    )
    with pytest.raises(ValueError, match="out of order"):
        MarketDayFrame(
            symbol=SYMBOL,
            market_date=DAY,
            timeframe=Timeframe.M5,
            market_tz=MARKET_TZ,
            bars=bars,
        )


def test_duplicate_bars_are_collapsed_by_the_builder_and_refused_by_the_model() -> None:
    """The observer overlaps by one candle on restart. Better to collapse it where the reason
    is visible than to fail a whole day's write on a bar that was simply seen twice."""
    doubled = m5(5) + m5(5)[:2]
    frame = build_frame(SYMBOL, DAY, Timeframe.M5, doubled, market_tz=MARKET_TZ, complete=True)
    assert len(frame.bars) == 5

    with pytest.raises(ValueError, match="duplicate bars"):
        MarketDayFrame(
            symbol=SYMBOL,
            market_date=DAY,
            timeframe=Timeframe.M5,
            market_tz=MARKET_TZ,
            bars=(
                FrameBar(at=START, open=1, high=1, low=1, close=1),
                FrameBar(at=START, open=2, high=2, low=2, close=2),
            ),
        )


def test_a_frame_defaults_to_incomplete() -> None:
    """So a document that never had the flag set cannot be read as a finished day."""
    frame = MarketDayFrame(
        symbol=SYMBOL, market_date=DAY, timeframe=Timeframe.M5, market_tz=MARKET_TZ
    )
    assert frame.complete is False


# ── Reading the cache back ───────────────────────────────────────────────────


def test_stored_m5_bars_round_trip_to_candles() -> None:
    original = m5(12)
    frame = build_frame(SYMBOL, DAY, Timeframe.M5, original, market_tz=MARKET_TZ, complete=True)
    back = candles_from([frame])
    assert [one.open_time.utc for one in back] == [one.open_time.utc for one in original]
    assert [one.close for one in back] == [one.close for one in original]
    assert all(one.timeframe is Timeframe.M5 for one in back)


def test_an_aggregated_frame_is_refused_as_a_source_of_candles() -> None:
    """``mtf.aggregate`` takes M5. Re-aggregating an already-aggregated bar would
    double-count, and refusing it here names the reason."""
    h1 = build_frame(SYMBOL, DAY, Timeframe.H1, m5(24), market_tz=MARKET_TZ, complete=True)
    with pytest.raises(ValueError, match="takes M5 frames"):
        candles_from([h1])


def test_frames_from_several_days_come_back_in_order() -> None:
    """Out of order, an EMA over them would be an EMA over a shuffled series."""
    monday = build_frame(
        SYMBOL, "2026-09-14", Timeframe.M5, m5(6, start=datetime(2026, 9, 14, 8, tzinfo=UTC)),
        market_tz=MARKET_TZ, complete=True,
    )
    tuesday = build_frame(
        SYMBOL, DAY, Timeframe.M5, m5(6), market_tz=MARKET_TZ, complete=True
    )
    back = candles_from([tuesday, monday])
    assert back == sorted(back, key=lambda one: one.open_time.utc)
    assert back[0].open_time.utc < datetime(2026, 9, 15, tzinfo=UTC)


# ── The context on a detection ───────────────────────────────────────────────


def read(timeframe: Timeframe, *, bias: TrendBias = TrendBias.BULLISH) -> TimeframeRead:
    return TimeframeRead(
        timeframe=timeframe,
        at=START,
        ema_fast=2401.0 if bias is TrendBias.BULLISH else 2399.0,
        ema_slow=2400.0,
        close=2400.5,
        bias=bias,
    )


def test_a_bias_without_its_emas_is_refused() -> None:
    """A verdict nobody can check. The engine returns SIDEWAYS in exactly this case, so
    refusing it here stops a hand-built document claiming what the code never would."""
    with pytest.raises(ValueError, match="needs both EMAs"):
        TimeframeRead(timeframe=Timeframe.H1, at=START, close=1.0, bias=TrendBias.BULLISH)
    # And SIDEWAYS with no EMAs is fine, because that is what "no view" looks like.
    assert TimeframeRead(timeframe=Timeframe.H1, at=START, close=1.0).bias is TrendBias.SIDEWAYS


def test_reads_are_smallest_first() -> None:
    """How a reader scans it and how every renderer prints it. An unordered tuple would put
    H4 above M15 on some screens and not others for no reason a reader could see."""
    with pytest.raises(ValueError, match="not smallest-first"):
        MtfContext(reads=(read(Timeframe.H1), read(Timeframe.M15)))
    assert MtfContext(reads=(read(Timeframe.M15), read(Timeframe.H1))).reads


def test_a_duplicate_timeframe_is_refused() -> None:
    with pytest.raises(ValueError, match="duplicate timeframes"):
        MtfContext(reads=(read(Timeframe.H1), read(Timeframe.H1)))


def test_a_timeframe_that_was_never_read_is_None_and_not_sideways() -> None:
    """"There was not enough history for H4" and "H4 had no view" are different, and a
    renderer that showed them alike would report a flat H4 for a symbol whose H4 nobody had
    ever computed."""
    context = MtfContext(reads=(read(Timeframe.M15),))
    assert context.bias_of(Timeframe.M15) is TrendBias.BULLISH
    assert context.bias_of(Timeframe.H4) is None


def test_the_ema_periods_are_stored_beside_the_bias() -> None:
    """A bias from 20/50 and one from 9/21 are different claims, and a document that recorded
    only the verdict could not tell them apart."""
    context = MtfContext(reads=(read(Timeframe.H1),), ema_fast_period=20, ema_slow_period=50)
    assert (context.ema_fast_period, context.ema_slow_period) == (20, 50)


def test_the_default_alignment_is_mixed() -> None:
    """So a context with no reads cannot claim agreement."""
    assert MtfContext().alignment is MtfAlignment.MIXED


# ── Through the engine ───────────────────────────────────────────────────────


def engine_with_mtf(*, mtf_bars: int = 2880):
    from aureon.agents.ema_cross_agent import EmaCrossAgent
    from aureon.engine.analysis_engine import AnalysisEngine

    return AnalysisEngine(
        [EmaCrossAgent(fast_period=20, slow_period=50)],
        account_scope="primary",
        market_tz=MARKET_TZ,
        mtf_bars=mtf_bars,
        mtf_periods=(20, 50),
    )


def fixture_candles() -> list[Candle]:
    from aureon.data.historical_provider import HistoricalDataProvider
    from tests.conftest import FIXTURE_CSV

    return HistoricalDataProvider(FIXTURE_CSV, market_tz=MARKET_TZ).candles


@pytest.fixture(scope="module")
def detections() -> list[Detection]:
    """One feed of the fixture week, shared.

    Module-scoped because feeding 1884 candles through six timeframes of aggregation takes
    seconds, and three tests want the same answer. They only read it.
    """
    return engine_with_mtf().feed(fixture_candles())


def test_a_detection_carries_the_higher_timeframe_reads(
    detections: list[Detection],
) -> None:
    assert detections, "the fixture week produces crosses"
    latest = detections[-1]
    assert latest.mtf is not None
    assert {one.timeframe for one in latest.mtf.reads} >= {Timeframe.M15, Timeframe.H1}
    assert latest.mtf.ema_fast_period == 20


def test_the_alignment_follows_the_detections_own_direction(
    detections: list[Detection],
) -> None:
    """The reads are shared across a candle's detections and the alignment is not: alignment
    needs a direction and the reads do not."""
    for one in detections:
        if one.mtf is None:
            continue
        opinions = {
            r.bias for r in one.mtf.reads if r.bias is not TrendBias.SIDEWAYS
        }
        wanted = (
            TrendBias.BULLISH if one.direction is Direction.BUY else TrendBias.BEARISH
        )
        if opinions == {wanted}:
            assert one.mtf.alignment is MtfAlignment.ALIGNED, one.detection_id
        elif opinions and wanted not in opinions:
            assert one.mtf.alignment is MtfAlignment.AGAINST, one.detection_id
        else:
            assert one.mtf.alignment is MtfAlignment.MIXED, one.detection_id


def test_an_engine_not_asked_for_mtf_attaches_none() -> None:
    """Which is what every pre-11D test wants, and what keeps the memory footprint where it
    was: the tail is not kept at all."""
    from aureon.agents.ema_cross_agent import EmaCrossAgent
    from aureon.engine.analysis_engine import AnalysisEngine

    plain = AnalysisEngine(
        [EmaCrossAgent(fast_period=20, slow_period=50)],
        account_scope="primary",
        market_tz=MARKET_TZ,
    )
    found = plain.feed(fixture_candles())
    assert found
    assert all(one.mtf is None for one in found)


def test_too_little_history_attaches_none_rather_than_a_flat_read() -> None:
    """A detection with no context and one whose H4 was sideways are different facts."""
    engine = engine_with_mtf(mtf_bars=60)
    found = engine.feed(fixture_candles()[:200])
    # 60 M5 bars is 20 M15 bars, short of the 50 an EMA(50) needs.
    assert all(one.mtf is None for one in found), [
        one.detection_id for one in found if one.mtf is not None
    ]


def test_the_agent_version_moved_for_the_new_field() -> None:
    """§12: the parameters and the version are what separate two populations. A detection
    carrying the context and one without it are different documents, and without a bump they
    would sit at ids produced by the same version -- indistinguishable and incomparable, which
    is the thing D-1 put ``agent_version`` back into the id to prevent."""
    from aureon.agents.ema_cross_agent import EmaCrossAgent

    assert EmaCrossAgent(fast_period=20, slow_period=50).agent_version == "2.2.0"


def test_every_agent_bumped_together() -> None:
    """All six carry the field, so all six fork. One left behind would make its detections
    silently incomparable with themselves across the change."""
    from aureon.agents.breakout_agent import BreakoutAgent
    from aureon.agents.liquidity_agent import LiquidityAgent
    from aureon.agents.rsi_agent import RsiAgent
    from aureon.agents.session_trend_agent import SessionTrendAgent
    from aureon.agents.wick_agent import WickAgent

    versions = {
        BreakoutAgent().agent_version,
        LiquidityAgent().agent_version,
        RsiAgent().agent_version,
        SessionTrendAgent().agent_version,
        WickAgent().agent_version,
    }
    assert versions == {"1.2.0"}, versions


# ── On the status panel ──────────────────────────────────────────────────────


def test_the_status_panel_shows_a_dash_for_a_timeframe_nobody_read() -> None:
    """Not "sideways". "Nobody computed H4" and "H4 was flat" are different facts, and this
    panel is the place a reader would otherwise conflate them."""
    from aureon.discord.service import build_live_panel
    from aureon.models.system import SymbolState

    state = SymbolState(symbol=SYMBOL, timeframe=Timeframe.M5)
    row = next(line for line in build_live_panel(state).lines if "higher tf" in line)
    assert "H4:—" in row and "D1:—" in row
    assert "sideways" not in row


def test_the_status_panel_prints_the_reads_it_has() -> None:
    from aureon.discord.service import build_live_panel
    from aureon.models.system import SymbolState

    state = SymbolState(
        symbol=SYMBOL,
        timeframe=Timeframe.M5,
        mtf=MtfContext(
            reads=(read(Timeframe.M15), read(Timeframe.H1, bias=TrendBias.BEARISH))
        ),
    )
    row = next(line for line in build_live_panel(state).lines if "higher tf" in line)
    assert "M15:bull" in row
    assert "H1:bear" in row
    assert "H4:—" in row, "and the ones with no read are still shown, as dashes"


def test_the_panel_row_is_always_present() -> None:
    """Like every other row: a block that appeared only when populated would change the
    panel's shape as data arrives, and a reader could not tell "no reads yet" from "this panel
    never had that row"."""
    from aureon.discord.service import build_live_panel
    from aureon.models.system import SymbolState

    empty = build_live_panel(SymbolState(symbol=SYMBOL, timeframe=Timeframe.M5))
    filled = build_live_panel(
        SymbolState(
            symbol=SYMBOL,
            timeframe=Timeframe.M5,
            mtf=MtfContext(reads=(read(Timeframe.M15),)),
        )
    )
    assert len(empty.lines) == len(filled.lines)
