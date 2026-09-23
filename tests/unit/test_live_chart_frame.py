from datetime import UTC, datetime, timedelta

from aureon.models.base import MarketTime
from aureon.models.enums import Timeframe
from aureon.models.market import Candle
from main_observer import Observer


class Frames:
    def __init__(self) -> None:
        self.written = []

    def write_frame(self, frame):
        self.written.append(frame)
        return frame


def candle(at: datetime, close: float) -> Candle:
    return Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(at, "Europe/Athens"),
        open=close - 1.0,
        high=close + 1.0,
        low=close - 2.0,
        close=close,
        tick_volume=100,
    )


def test_live_chart_frame_is_published_incomplete() -> None:
    observer = Observer.__new__(Observer)
    observer.market_days = Frames()
    observer.config = type("Config", (), {"market_tz": "Europe/Athens"})()

    start = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)
    bars = [candle(start, 4318.0), candle(start + timedelta(minutes=5), 4319.0)]

    observer._write_live_chart_frame("XAUUSD", "2026-09-23", bars)

    assert len(observer.market_days.written) == 1
    frame = observer.market_days.written[0]
    assert frame.symbol == "XAUUSD"
    assert frame.timeframe is Timeframe.M5
    assert frame.complete is False
    assert len(frame.bars) == 2
