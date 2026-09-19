"""Data provider behaviour (§7-§9).

The MT5 time conversion gets the most attention here. It is the single most
bug-prone piece of the data layer -- an off-by-one-timezone error silently misfiles
every session, day boundary and detection id -- and because the conversion is a pure
function it is testable without a Windows terminal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureon.data.base_provider import MarketDataError, floor_to_timeframe, is_closed
from aureon.data.historical_provider import HistoricalDataProvider
from aureon.data.mt5_provider import (
    decode_filling_modes,
    server_epoch_to_utc,
    utc_to_server_epoch,
)
from aureon.models.enums import FillingMode, Timeframe
from tests.conftest import FakeLiveProvider

ATHENS = "Europe/Athens"


# ── MT5 server time ───────────────────────────────────────────────────────────


def test_server_time_is_not_utc_and_is_converted() -> None:
    """A broker on Athens reports 16:00 for a candle that opened at 13:00 UTC.

    Reading MT5's integer as a real Unix timestamp shifts every candle by the
    broker's offset.
    """
    reported = datetime(2026, 9, 18, 16, 0, tzinfo=UTC).timestamp()
    assert server_epoch_to_utc(reported, ATHENS) == datetime(
        2026, 9, 18, 13, 0, tzinfo=UTC
    )


def test_the_conversion_follows_dst() -> None:
    """Athens is +3 in summer and +2 in winter.

    A fixed offset would be wrong for half the year, which is why the broker zone
    must be a real IANA zone.
    """
    summer = datetime(2026, 9, 18, 16, 0, tzinfo=UTC).timestamp()
    winter = datetime(2026, 1, 15, 15, 0, tzinfo=UTC).timestamp()
    assert server_epoch_to_utc(summer, ATHENS).hour == 13
    assert server_epoch_to_utc(winter, ATHENS).hour == 13


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 9, 18, 13, 0, tzinfo=UTC),
        datetime(2026, 1, 15, 13, 0, tzinfo=UTC),
        datetime(2026, 3, 29, 3, 0, tzinfo=UTC),  # near a DST switch
    ],
)
def test_the_conversion_round_trips(moment: datetime) -> None:
    assert server_epoch_to_utc(utc_to_server_epoch(moment, ATHENS), ATHENS) == moment


def test_a_utc_broker_needs_no_shift() -> None:
    reported = datetime(2026, 9, 18, 16, 0, tzinfo=UTC).timestamp()
    assert server_epoch_to_utc(reported, "Etc/UTC").hour == 16


def test_naive_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        utc_to_server_epoch(datetime(2026, 9, 18, 13, 0), ATHENS)


@pytest.mark.parametrize(
    "flags,expected",
    [
        (0, ()),
        (1, (FillingMode.FOK,)),
        (2, (FillingMode.IOC,)),
        (3, (FillingMode.FOK, FillingMode.IOC)),
    ],
)
def test_filling_modes_decode(flags: int, expected: tuple[FillingMode, ...]) -> None:
    """Phase 6 offers only what the broker accepts (§39)."""
    assert decode_filling_modes(flags) == expected


# ── Candle boundaries ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "timeframe,expected_minute",
    [(Timeframe.M1, 7), (Timeframe.M5, 5), (Timeframe.M15, 0), (Timeframe.M30, 0)],
)
def test_flooring_to_a_timeframe_bucket(timeframe: Timeframe, expected_minute: int) -> None:
    moment = datetime(2026, 9, 18, 13, 7, 42, tzinfo=UTC)
    assert floor_to_timeframe(moment, timeframe).minute == expected_minute


def test_a_candle_is_closed_only_once_its_end_has_passed() -> None:
    opened = datetime(2026, 9, 18, 13, 5, tzinfo=UTC)
    assert not is_closed(opened, Timeframe.M5, opened + timedelta(minutes=4, seconds=59))
    assert is_closed(opened, Timeframe.M5, opened + timedelta(minutes=5))


# ── Historical provider ───────────────────────────────────────────────────────


def test_the_fixture_loads_and_is_ordered(historical: HistoricalDataProvider) -> None:
    candles = historical.candles
    assert len(candles) > 1000
    opens = [c.open_time.utc for c in candles]
    assert opens == sorted(opens)
    assert len(set(opens)) == len(opens)


def test_the_fixture_contains_the_weekend_gap(historical: HistoricalDataProvider) -> None:
    """Gap handling and the INVALID-horizon path need a real discontinuity."""
    candles = historical.candles
    gaps = [
        (b.open_time.utc - a.open_time.utc).total_seconds()
        for a, b in zip(candles, candles[1:], strict=False)
        if (b.open_time.utc - a.open_time.utc).total_seconds() > 300
    ]
    assert gaps, "fixture has no gap; weekend handling would be untested"
    assert max(gaps) > 24 * 3600


def test_range_queries_are_half_open(historical: HistoricalDataProvider) -> None:
    """So consecutive calls can tile a range without double-counting a boundary."""
    start = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
    end = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    first = historical.get_closed_candles("XAUUSD", Timeframe.M5, start, end)
    second = historical.get_closed_candles(
        "XAUUSD", Timeframe.M5, end, end + timedelta(hours=1)
    )
    assert len(first) == 12
    assert {c.open_time.utc for c in first} & {c.open_time.utc for c in second} == set()


def test_a_mismatched_symbol_or_timeframe_is_refused(
    historical: HistoricalDataProvider,
) -> None:
    window = (
        datetime(2026, 9, 14, tzinfo=UTC),
        datetime(2026, 9, 15, tzinfo=UTC),
    )
    with pytest.raises(MarketDataError):
        historical.get_closed_candles("EURUSD", Timeframe.M5, *window)
    with pytest.raises(MarketDataError):
        historical.get_closed_candles("XAUUSD", Timeframe.M15, *window)


def test_a_missing_file_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(MarketDataError, match="not found"):
        HistoricalDataProvider(tmp_path / "nope.csv").load()


def test_replay_now_is_the_data_not_the_wall_clock(
    historical: HistoricalDataProvider,
) -> None:
    """A replay over old data with a live "now" would misjudge every gap."""
    assert historical.now_utc() == historical.candles[-1].close_time
    pinned = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    historical.set_clock(pinned)
    assert historical.now_utc() == pinned


def test_ohlc_is_validated_on_load(tmp_path: Path) -> None:
    """A malformed row must fail at load, not silently become a bad detection."""
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "open_time,open,high,low,close,tick_volume\n"
        "2026-09-14T00:00:00+00:00,2400,2399,2401,2400,10\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        HistoricalDataProvider(bad).load()


def test_a_duplicate_candle_in_a_file_is_rejected(tmp_path: Path) -> None:
    """A duplicate open would make one bar detectable twice."""
    dup = tmp_path / "dup.csv"
    row = "2026-09-14T00:00:00+00:00,2400,2401,2399,2400,10\n"
    dup.write_text("open_time,open,high,low,close,tick_volume\n" + row + row, encoding="utf-8")
    with pytest.raises(MarketDataError, match="duplicate"):
        HistoricalDataProvider(dup).load()


# ── The fake live provider (used by parity) ────────────────────────────────────


def test_the_fake_live_provider_withholds_the_forming_bar(
    fake_live: FakeLiveProvider, candles: list
) -> None:
    """If the fake leaked the forming bar, the parity test would prove nothing."""
    target = candles[200]
    fake_live.set_clock(target.open_time.utc + timedelta(minutes=2))
    visible = fake_live.get_closed_candles(
        "XAUUSD", Timeframe.M5, candles[0].open_time.utc, target.close_time
    )
    assert target.open_time.utc not in {c.open_time.utc for c in visible}


def test_the_fake_live_provider_reveals_candles_as_the_clock_advances(
    fake_live: FakeLiveProvider, candles: list
) -> None:
    window = (candles[0].open_time.utc, candles[-1].close_time)
    fake_live.set_clock(candles[0].open_time.utc)
    assert fake_live.get_closed_candles("XAUUSD", Timeframe.M5, *window) == []
    fake_live.advance_to_close_of(candles[9])
    assert len(fake_live.get_closed_candles("XAUUSD", Timeframe.M5, *window)) == 10
