from datetime import UTC, datetime, timedelta

from aureon.engine.structure import structure_points
from aureon.models.base import MarketTime
from aureon.models.enums import Timeframe
from aureon.models.market import Candle
from aureon.services.market_day_builder import build_frame


TZ = "Europe/Athens"
START = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)


def candle(i: int, high: float, low: float, close: float | None = None) -> Candle:
    mid = (high + low) / 2 if close is None else close
    return Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(START + timedelta(minutes=5 * i), TZ),
        open=mid,
        high=high,
        low=low,
        close=mid,
        tick_volume=100 + i,
    )


def sample() -> list[Candle]:
    # strength=2 pivots: high at 2, lower high at 6; low at 4, lower low at 8.
    highs = [10, 12, 16, 13, 11, 12, 15, 12, 10, 11, 9]
    lows =  [ 7,  8, 11,  8,  5,  8, 10,  7,  3,  6, 4]
    return [candle(i, h, l) for i, (h, l) in enumerate(zip(highs, lows))]


def test_structure_labels_highs_and_lows_against_previous_swings() -> None:
    points = structure_points(sample(), strength=2)
    labels = [(p.at, p.label) for p in points]
    assert (START + timedelta(minutes=10), "SH") in labels
    assert (START + timedelta(minutes=20), "SL") in labels
    assert (START + timedelta(minutes=30), "LH") in labels
    assert (START + timedelta(minutes=40), "LL") in labels


def test_a_pivot_is_not_visible_until_future_confirmation_bars_exist() -> None:
    bars = sample()
    # Index 8 can only be confirmed after indices 9 and 10 have closed.
    early = structure_points(bars[:10], strength=2)
    full = structure_points(bars[:11], strength=2)
    assert not any(p.at == START + timedelta(minutes=40) for p in early)
    assert any(p.at == START + timedelta(minutes=40) and p.label == "LL" for p in full)


def test_market_day_frame_persists_structure_labels_for_discord() -> None:
    frame = build_frame(
        "XAUUSD",
        "2026-09-23",
        Timeframe.M5,
        sample(),
        market_tz=TZ,
        complete=False,
    )
    by_time = {bar.at: bar.structure_labels for bar in frame.bars}
    assert "LH" in by_time[START + timedelta(minutes=30)]
    assert "LL" in by_time[START + timedelta(minutes=40)]
