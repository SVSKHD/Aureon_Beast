"""The broker-day cache on a real server (11D, S-3b).

Nothing gates on these rows, so there is no transaction to prove. What matters is that
``complete`` really does exclude the day in progress -- an unfinished day's last bar is not
its last bar, and an aggregation fed one produces a daily bar whose close is not the close.
"""

from __future__ import annotations

import pytest

from aureon.models.enums import Timeframe
from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.market_days import (
    MarketDayRepository,
    market_day_frame_row_id,
    market_day_row_id,
)
from tests.postgres.factories import CLOSE, TZ, a_market_day, a_market_day_frame

pytestmark = pytest.mark.postgres


@pytest.fixture
def repo(schema: Database) -> MarketDayRepository:
    return MarketDayRepository(schema)


# ── The ids ───────────────────────────────────────────────────────────────────


def test_the_day_id_puts_the_symbol_first() -> None:
    """So a prefix scan reads as one instrument's history. "Every instrument on Tuesday"
    is not a question anything asks."""
    assert market_day_row_id("xauusd", "2026-09-22") == "XAUUSD_2026-09-22"


def test_the_frame_id_adds_the_timeframe() -> None:
    assert (
        market_day_frame_row_id("XAUUSD", "2026-09-22", Timeframe.M15)
        == "XAUUSD_2026-09-22_M15"
    )


@pytest.mark.parametrize(("symbol", "date"), [("", "2026-09-22"), ("XAUUSD", "")])
def test_the_day_id_refuses_an_empty_part(symbol: str, date: str) -> None:
    with pytest.raises(ValueError):
        market_day_row_id(symbol, date)


# ── The day ───────────────────────────────────────────────────────────────────


def test_a_day_survives_the_database_unchanged(repo: MarketDayRepository) -> None:
    written = repo.write_day(a_market_day(), now=CLOSE)

    assert repo.get_day("XAUUSD", "2026-09-22") == written


def test_writing_a_day_stamps_when_it_was_written(repo: MarketDayRepository) -> None:
    written = repo.write_day(a_market_day(), now=CLOSE)

    assert written.updated_at == CLOSE


def test_the_frames_list_comes_back_as_a_list(repo: MarketDayRepository) -> None:
    repo.write_day(a_market_day(), now=CLOSE)

    stored = repo.get_day("XAUUSD", "2026-09-22")
    assert stored is not None
    assert stored.frames == (Timeframe.M15, Timeframe.H1)


def test_the_market_zone_survives_the_round_trip(repo: MarketDayRepository) -> None:
    repo.write_day(a_market_day(), now=CLOSE)

    stored = repo.get_day("XAUUSD", "2026-09-22")
    assert stored is not None and stored.market_tz == TZ


def test_rewriting_a_day_overwrites_rather_than_duplicates(
    repo: MarketDayRepository,
) -> None:
    repo.write_day(a_market_day(bars=100, complete=False), now=CLOSE)
    repo.write_day(a_market_day(bars=288, complete=True), now=CLOSE)

    stored = repo.get_day("XAUUSD", "2026-09-22")
    assert stored is not None and stored.bars == 288


def test_the_day_in_progress_is_excluded_from_the_finished_ones(
    repo: MarketDayRepository,
) -> None:
    """A tuning report that averaged a half-finished day alongside finished ones would
    report a range that is simply wrong, and nothing downstream could tell."""
    repo.write_day(a_market_day("2026-09-21", complete=True), now=CLOSE)
    repo.write_day(a_market_day("2026-09-22", complete=False), now=CLOSE)

    found = repo.complete_days("XAUUSD")
    assert [d.market_date for d in found] == ["2026-09-21"]


def test_finished_days_read_oldest_first(repo: MarketDayRepository) -> None:
    for date in ("2026-09-18", "2026-09-21", "2026-09-22"):
        repo.write_day(a_market_day(date), now=CLOSE)

    found = repo.complete_days("XAUUSD")
    assert [d.market_date for d in found] == ["2026-09-18", "2026-09-21", "2026-09-22"]


def test_finished_days_take_the_most_recent_when_limited(
    repo: MarketDayRepository,
) -> None:
    for date in ("2026-09-18", "2026-09-21", "2026-09-22"):
        repo.write_day(a_market_day(date), now=CLOSE)

    found = repo.complete_days("XAUUSD", limit=2)
    assert [d.market_date for d in found] == ["2026-09-21", "2026-09-22"]


def test_an_uncached_day_reads_as_none(repo: MarketDayRepository) -> None:
    assert repo.get_day("XAUUSD", "2026-01-01") is None


# ── The bars ──────────────────────────────────────────────────────────────────


def test_a_frame_survives_the_database_unchanged(repo: MarketDayRepository) -> None:
    written = repo.write_frame(a_market_day_frame(), now=CLOSE)

    assert repo.get_frame("XAUUSD", "2026-09-22", Timeframe.M15) == written


def test_the_bars_come_back_in_order(repo: MarketDayRepository) -> None:
    repo.write_frame(a_market_day_frame(), now=CLOSE)

    stored = repo.get_frame("XAUUSD", "2026-09-22", Timeframe.M15)
    assert stored is not None
    assert [bar.close for bar in stored.bars] == [2404.0, 2403.5]


def test_two_timeframes_of_one_day_do_not_collide(repo: MarketDayRepository) -> None:
    repo.write_frame(a_market_day_frame(timeframe=Timeframe.M15), now=CLOSE)
    repo.write_frame(a_market_day_frame(timeframe=Timeframe.H1), now=CLOSE)

    assert repo.get_frame("XAUUSD", "2026-09-22", Timeframe.M15) is not None
    assert repo.get_frame("XAUUSD", "2026-09-22", Timeframe.H1) is not None


def test_recent_frames_reads_the_days_asked_for_oldest_first(
    repo: MarketDayRepository,
) -> None:
    for date in ("2026-09-18", "2026-09-21", "2026-09-22"):
        repo.write_frame(a_market_day_frame(date), now=CLOSE)

    found = repo.recent_frames("XAUUSD", Timeframe.M15, ["2026-09-22", "2026-09-18"])
    assert [f.market_date for f in found] == ["2026-09-18", "2026-09-22"]


def test_recent_frames_skips_a_day_that_was_never_cached(
    repo: MarketDayRepository,
) -> None:
    """The cache is an optimisation: a bias built from eight of the ten days asked for is
    a weaker claim, not an error."""
    repo.write_frame(a_market_day_frame("2026-09-21"), now=CLOSE)

    found = repo.recent_frames("XAUUSD", Timeframe.M15, ["2026-09-21", "2026-09-22"])
    assert [f.market_date for f in found] == ["2026-09-21"]


def test_recent_frames_excludes_the_day_in_progress(repo: MarketDayRepository) -> None:
    """``mtf._complete`` refuses one level down; this is the same refusal one level up."""
    repo.write_frame(a_market_day_frame("2026-09-21", complete=True), now=CLOSE)
    repo.write_frame(a_market_day_frame("2026-09-22", complete=False), now=CLOSE)

    found = repo.recent_frames("XAUUSD", Timeframe.M15, ["2026-09-21", "2026-09-22"])
    assert [f.market_date for f in found] == ["2026-09-21"]


def test_a_chart_can_ask_for_the_day_in_progress_by_name(
    repo: MarketDayRepository,
) -> None:
    repo.write_frame(a_market_day_frame(complete=False), now=CLOSE)

    found = repo.recent_frames(
        "XAUUSD", Timeframe.M15, ["2026-09-22"], complete_only=False
    )
    assert [f.market_date for f in found] == ["2026-09-22"]
    assert repo.get_frame("XAUUSD", "2026-09-22", Timeframe.M15) is not None


def test_recent_frames_does_not_mix_timeframes(repo: MarketDayRepository) -> None:
    repo.write_frame(a_market_day_frame(timeframe=Timeframe.H1), now=CLOSE)

    assert repo.recent_frames("XAUUSD", Timeframe.M15, ["2026-09-22"]) == []


def test_asking_for_no_days_reads_nothing(repo: MarketDayRepository) -> None:
    assert repo.recent_frames("XAUUSD", Timeframe.M15, []) == []


def test_a_truncated_frame_says_so(repo: MarketDayRepository) -> None:
    """A reader that mistook a truncated frame for a complete one would compute an EMA
    over a window that silently starts in the middle of the session."""
    repo.write_frame(a_market_day_frame(truncated=True), now=CLOSE)

    stored = repo.get_frame("XAUUSD", "2026-09-22", Timeframe.M15)
    assert stored is not None and stored.truncated is True
