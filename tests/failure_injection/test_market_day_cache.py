"""The broker-day cache, through real Firestore (11D).

Why this needs the emulator rather than a double: the whole point of the cache is that a
process with no broker connection can read back what another process wrote, and a round trip
through an in-memory dict proves nothing about that. The models are validated on the way out
and on the way back in, and a field that serialised badly -- a tuple of sub-models, a
timezone-aware datetime -- would pass a dict and fail a document.

The claim under test, in one sentence: a finished broker day goes in, and comes back out as the
same bars, so an H4 or D1 bias built from weeks of them is built from what the broker served.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.engine.mtf import aggregate, frames_from
from aureon.models.base import MarketTime
from aureon.models.enums import Timeframe
from aureon.models.market import Candle
from aureon.models.market_day import CACHED
from aureon.services.market_day_builder import build_day, build_frame, candles_from
from aureon.storage.market_day_repository import MarketDayRepository

pytestmark = pytest.mark.emulator

MARKET_TZ = "Europe/Athens"
SYMBOL = "XAUUSD"


def day_of(market_date: str, *, hours: int = 24, base: float = 2400.0) -> list[Candle]:
    """A whole broker day of M5 bars, starting at midnight in Athens.

    Midnight Athens rather than midnight UTC, because that is where a broker day starts: a bar
    at 22:00 UTC belongs to the NEXT trading day, and a fixture that ignored that would produce
    a "day" the system would file under two dates.
    """
    from zoneinfo import ZoneInfo

    start = datetime.fromisoformat(f"{market_date}T00:00:00").replace(
        tzinfo=ZoneInfo(MARKET_TZ)
    )
    return [
        Candle(
            symbol=SYMBOL,
            timeframe=Timeframe.M5,
            open_time=MarketTime.from_utc(
                start.astimezone(UTC) + timedelta(minutes=5 * i), MARKET_TZ
            ),
            open=base + i * 0.1,
            high=base + i * 0.1 + 0.4,
            low=base + i * 0.1 - 0.4,
            close=base + i * 0.1 + 0.2,
            tick_volume=100 + i,
        )
        for i in range(12 * hours)
    ]


@pytest.fixture
def repository(firestore_client) -> MarketDayRepository:
    return MarketDayRepository(firestore_client)


def store(repository: MarketDayRepository, market_date: str, **kwargs) -> list[Candle]:
    candles = day_of(market_date, **kwargs)
    for timeframe in CACHED:
        repository.write_frame(
            build_frame(
                SYMBOL,
                market_date,
                timeframe,
                candles,
                market_tz=MARKET_TZ,
                complete=True,
            )
        )
    repository.write_day(
        build_day(SYMBOL, market_date, candles, market_tz=MARKET_TZ, complete=True)
    )
    return candles


# ── One day, out and back ────────────────────────────────────────────────────


def test_a_stored_day_comes_back_as_the_same_bars(repository) -> None:
    candles = store(repository, "2026-09-15")
    frame = repository.get_frame(SYMBOL, "2026-09-15", Timeframe.M5)
    assert frame is not None
    assert len(frame.bars) == len(candles)

    back = candles_from([frame])
    assert [one.open_time.utc for one in back] == [one.open_time.utc for one in candles]
    assert [one.close for one in back] == [one.close for one in candles]
    assert [one.high for one in back] == [one.high for one in candles]


def test_the_days_shape_survives_the_round_trip(repository) -> None:
    candles = store(repository, "2026-09-15")
    day = repository.get_day(SYMBOL, "2026-09-15")
    assert day is not None
    assert day.bars == len(candles)
    assert day.open == candles[0].open
    assert day.close == candles[-1].close
    assert day.high == max(one.high for one in candles)
    assert day.low == min(one.low for one in candles)
    assert day.complete
    assert set(day.frames) == set(CACHED)
    assert day.updated_at is not None, "a stored document says when it was written"


def test_the_aggregated_frames_match_the_engine(repository) -> None:
    """The stored H1 must be the H1 ``mtf.aggregate`` would build from the same M5 bars. If
    they ever differ, a bias read from the cache and one read from a live buffer disagree, and
    nothing downstream could say which was right."""
    candles = store(repository, "2026-09-15")
    for timeframe in (Timeframe.M15, Timeframe.H1):
        stored = repository.get_frame(SYMBOL, "2026-09-15", timeframe)
        assert stored is not None
        expected = aggregate(candles, timeframe)
        assert [bar.at for bar in stored.bars] == [
            one.open_time.utc for one in expected
        ], timeframe
        assert [bar.close for bar in stored.bars] == [
            one.close for one in expected
        ], timeframe


def test_a_missing_day_reads_as_none_rather_than_raising(repository) -> None:
    assert repository.get_day(SYMBOL, "2026-01-01") is None
    assert repository.get_frame(SYMBOL, "2026-01-01", Timeframe.M5) is None


def test_another_symbols_day_is_not_this_ones(repository) -> None:
    store(repository, "2026-09-15")
    assert repository.get_day("XAGUSD", "2026-09-15") is None


# ── Several days, for a bias above H1 ────────────────────────────────────────


def test_a_week_of_stored_days_builds_the_bias_a_buffer_cannot(repository) -> None:
    """What the cache is FOR.

    An H4 EMA(50) needs two hundred hours of history and a live observer's buffer holds a few,
    so the aggregated frames are read back across days and re-aggregated. Five stored days is
    enough to put H4 within reach at short periods; the point is that the history comes from
    Firestore rather than from memory.
    """
    dates = [f"2026-09-{day:02d}" for day in (14, 15, 16, 17, 18)]
    for index, market_date in enumerate(dates):
        store(repository, market_date, base=2400.0 + index * 5)

    frames = repository.recent_frames(SYMBOL, Timeframe.M5, dates)
    assert len(frames) == len(dates)

    candles = candles_from(frames)
    assert len(candles) == len(dates) * 12 * 24

    reads = frames_from(candles, fast=3, slow=10)
    built = {one.timeframe for one in reads}
    assert Timeframe.H4 in built, "five stored days reaches H4 at short periods"
    assert all(one.ema_fast is not None for one in reads)


def test_an_unfinished_day_is_left_out_of_a_read(repository) -> None:
    """An unfinished day's last bar is not its last bar. Feeding one into an aggregation would
    produce a daily bar whose close is not the close -- the failure ``mtf._complete`` refuses
    one level down, and the flag is how this level refuses it."""
    candles = day_of("2026-09-15", hours=4)
    repository.write_frame(
        build_frame(
            SYMBOL,
            "2026-09-15",
            Timeframe.M5,
            candles,
            market_tz=MARKET_TZ,
            complete=False,
        )
    )
    assert repository.recent_frames(SYMBOL, Timeframe.M5, ["2026-09-15"]) == []
    # And a caller that genuinely wants it -- a chart -- asks for it by name.
    asked = repository.get_frame(SYMBOL, "2026-09-15", Timeframe.M5)
    assert asked is not None and not asked.complete


def test_a_missing_day_is_skipped_rather_than_raised_on(repository) -> None:
    """The cache is an optimisation. A bias built from four of the five days asked for is a
    weaker claim, not an error -- and ``frames_from`` refuses a timeframe with too little
    history anyway, so a short read cannot silently become a short EMA."""
    store(repository, "2026-09-15")
    frames = repository.recent_frames(
        SYMBOL, Timeframe.M5, ["2026-09-14", "2026-09-15", "2026-09-16"]
    )
    assert [one.market_date for one in frames] == ["2026-09-15"]


def test_days_come_back_oldest_first(repository) -> None:
    """Out of order, an EMA over them would be an EMA over a shuffled series."""
    dates = ["2026-09-16", "2026-09-14", "2026-09-15"]
    for market_date in dates:
        store(repository, market_date)
    frames = repository.recent_frames(SYMBOL, Timeframe.M5, dates)
    assert [one.market_date for one in frames] == sorted(dates)


# ── Through the observer ─────────────────────────────────────────────────────


def test_the_observer_caches_a_day_when_the_date_rolls_over(
    tmp_path, firestore_client, candles
) -> None:
    """The rollover is the moment a day will never grow again, and the only moment the cache is
    written. A day in progress is deliberately not stored: a document flagged incomplete is one
    mistake away from an aggregation reading it as finished."""
    observer = build_observer_for(tmp_path, firestore_client)

    days: dict[str, list[Candle]] = {}
    for one in candles:
        days.setdefault(one.open_time.market_date, []).append(one)
    ordered = sorted(days)
    first, second = ordered[0], ordered[1]

    for one in days[first] + days[second][:3]:
        observer._cache_market_day(one)

    stored = MarketDayRepository(firestore_client).get_day(SYMBOL, first)
    assert stored is not None, "the day that rolled over was written"
    assert stored.bars == len(days[first])
    assert not stored.complete, (
        "and it is PARTIAL: this process joined that day mid-session, so its open and low are "
        "not the day's"
    )

    assert MarketDayRepository(firestore_client).get_day(SYMBOL, second) is None, (
        "and the day in progress was not written at all"
    )


def test_a_day_seen_from_its_rollover_is_complete(
    tmp_path, firestore_client, candles
) -> None:
    """The distinction that keeps a half-day out of a D1 bar.

    A process that starts at 14:00 has the afternoon and none of the morning, and writing that
    as complete would be worse than writing nothing: the daily bar aggregated from it would
    have the wrong open, the wrong low and a perfectly plausible shape. Only a day whose first
    bar arrived as a ROLLOVER is complete.
    """
    observer = build_observer_for(tmp_path, firestore_client)

    days: dict[str, list[Candle]] = {}
    for one in candles:
        days.setdefault(one.open_time.market_date, []).append(one)
    first, second, third = sorted(days)[:3]

    for one in days[first] + days[second] + days[third][:2]:
        observer._cache_market_day(one)

    repository = MarketDayRepository(firestore_client)
    assert repository.get_day(SYMBOL, first).complete is False, "joined mid-day"
    assert repository.get_day(SYMBOL, second).complete is True, "seen from its rollover"
    assert repository.recent_frames(SYMBOL, Timeframe.M5, [first, second]) == [
        repository.get_frame(SYMBOL, second, Timeframe.M5)
    ], "and only the complete one is offered to an aggregation"


def test_the_weeks_last_day_is_written_at_the_close_not_on_sunday(
    tmp_path, firestore_client, candles
) -> None:
    """Otherwise a restart over the weekend loses it.

    The rollover that would announce Friday's end is Sunday night's first bar, 49 hours later,
    and the buffer holding Friday does not survive a restart. The weekly close is the moment
    that broker day genuinely ended, so that is where it is written.
    """
    observer = build_observer_for(tmp_path, firestore_client)

    days: dict[str, list[Candle]] = {}
    for one in candles:
        days.setdefault(one.open_time.market_date, []).append(one)
    first, second = sorted(days)[:2]
    for one in days[first] + days[second]:
        observer._cache_market_day(one)

    repository = MarketDayRepository(firestore_client)
    assert repository.get_day(SYMBOL, second) is None, "still buffered, not yet rolled over"

    class Weekly:
        weekly = True
        next_open = None

    observer._on_market_close(Weekly())
    assert repository.get_day(SYMBOL, second) is not None, "the close wrote it"
    assert observer._day_bars[(SYMBOL, Timeframe.M5)] == [], "and the buffer was cleared"


def build_observer_for(tmp_path, firestore_client):
    from aureon.config import AureonConfig
    from aureon.outbox.local_outbox import LocalOutbox
    from aureon.outbox.outbox_worker import OutboxWorker
    from aureon.services.observer_state import ObserverState
    from aureon.storage.detection_repository import DetectionRepository
    from main_observer import Observer
    from tests.conftest import cross_agent

    config = AureonConfig.from_env(
        env={
            "AUREON_ACCOUNT_SCOPE": "primary",
            "AUREON_MARKET_TZ": MARKET_TZ,
            "AUREON_SYMBOLS": SYMBOL,
            "AUREON_TIMEFRAMES": "M5",
            "AUREON_OUTBOX_PATH": str(tmp_path / "outbox.db"),
            "AUREON_OBSERVER_STATE_PATH": str(tmp_path / "state.json"),
        }
    )
    outbox = LocalOutbox(config.outbox_path)
    observer = Observer(
        config,
        provider=None,  # type: ignore[arg-type]
        outbox=outbox,
        worker=OutboxWorker(outbox, DetectionRepository(firestore_client).upsert_payload),
        state=ObserverState(config.observer_state_path),
        agents=[cross_agent()],
    )
    observer.market_days = MarketDayRepository(firestore_client)
    return observer
