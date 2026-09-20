"""The live-candle archive and the live-vs-replay comparison (§82).

The parity test proves the engine is deterministic given the same candles. It cannot prove
the live path was *given* the same candles, because the live input only ever existed in
memory. That is the gap these two pieces close, and the property that makes them worth
anything is a round trip: a candle written and read back must produce a byte-identical
detection, or every comparison would report differences that were only ever a
serialisation artefact.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureon.data.live_candle_archive import (
    COLUMNS,
    LiveCandleArchive,
    archive_path,
    read_archive,
)
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.models.base import MarketTime
from aureon.models.enums import Timeframe
from aureon.models.market import Candle
from tests.conftest import ACCOUNT_SCOPE, MARKET_TZ, cross_agent

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "compare_live_vs_replay", REPO_ROOT / "scripts" / "compare_live_vs_replay.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


compare_module = _load_script()


def candle(index: int, *, price: float = 2400.0, symbol: str = "XAUUSD") -> Candle:
    # 2026-09-16 07:00 UTC, so a 5-minute grid stays inside one broker day.
    opened = datetime(2026, 9, 16, 7, 0, tzinfo=UTC) + timedelta(minutes=5 * index)
    return Candle(
        symbol=symbol,
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(opened, MARKET_TZ),
        open=price,
        high=price + 1.5,
        low=price - 1.5,
        close=price + 0.25,
        tick_volume=100 + index,
        real_volume=200 + index,
    )


# ── The archive ───────────────────────────────────────────────────────────────


def test_a_candle_round_trips_byte_identically(tmp_path: Path) -> None:
    """The property everything else rests on.

    A float that lost precision through the file would make every comparison report a
    difference that was never in the data -- and a reader would go looking for an engine
    bug that does not exist.
    """
    archive = LiveCandleArchive(root=tmp_path)
    original = [candle(i, price=2400.0 + i * 0.37) for i in range(12)]
    for item in original:
        archive.add(item)
    archive.flush_all()

    read_back = read_archive(
        "XAUUSD", Timeframe.M5, "2026-09-16", market_tz=MARKET_TZ, root=tmp_path
    )

    assert len(read_back) == len(original)
    for before, after in zip(original, read_back, strict=True):
        assert after.model_dump(mode="json") == before.model_dump(mode="json")


def test_the_file_is_named_by_the_broker_date_not_the_utc_one(tmp_path: Path) -> None:
    """A 22:00 UTC candle belongs to the next trading day in Athens.

    Filed under the UTC date it would split one session across two files, so a comparison
    for "Monday" would silently miss its first three hours.
    """
    archive = LiveCandleArchive(root=tmp_path)
    late = Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(
            datetime(2026, 9, 16, 22, 0, tzinfo=UTC), MARKET_TZ
        ),
        open=2400.0,
        high=2401.0,
        low=2399.0,
        close=2400.5,
    )
    assert late.open_time.utc.date().isoformat() == "2026-09-16"
    assert late.open_time.market_date == "2026-09-17"

    archive.add(late)
    archive.flush_all()

    assert archive_path("XAUUSD", Timeframe.M5, "2026-09-17", root=tmp_path).exists()
    assert not archive_path("XAUUSD", Timeframe.M5, "2026-09-16", root=tmp_path).exists()


def test_a_day_rollover_flushes_the_previous_day(tmp_path: Path) -> None:
    archive = LiveCandleArchive(root=tmp_path)
    archive.add(candle(0))
    # 2026-09-16 21:00 UTC is already 2026-09-17 in Athens.
    archive.add(
        Candle(
            symbol="XAUUSD",
            timeframe=Timeframe.M5,
            open_time=MarketTime.from_utc(
                datetime(2026, 9, 16, 21, 0, tzinfo=UTC), MARKET_TZ
            ),
            open=2400.0,
            high=2401.0,
            low=2399.0,
            close=2400.5,
        )
    )

    assert archive_path("XAUUSD", Timeframe.M5, "2026-09-16", root=tmp_path).exists()
    assert archive.buffered() == 1, "the new day should still be buffered"


def test_a_restart_mid_day_merges_rather_than_truncating(tmp_path: Path) -> None:
    """A truncated archive would report every missing morning detection as a mismatch."""
    morning = LiveCandleArchive(root=tmp_path)
    for i in range(5):
        morning.add(candle(i))
    morning.flush_all()

    afternoon = LiveCandleArchive(root=tmp_path)
    for i in range(5, 10):
        afternoon.add(candle(i))
    afternoon.flush_all()

    read_back = read_archive(
        "XAUUSD", Timeframe.M5, "2026-09-16", market_tz=MARKET_TZ, root=tmp_path
    )
    assert len(read_back) == 10
    assert [c.open_time.utc for c in read_back] == sorted(c.open_time.utc for c in read_back)


def test_a_re_archived_candle_does_not_duplicate(tmp_path: Path) -> None:
    """The restart backfill replays candles it already processed."""
    archive = LiveCandleArchive(root=tmp_path)
    for i in range(3):
        archive.add(candle(i))
    archive.flush_all()
    again = LiveCandleArchive(root=tmp_path)
    again.add(candle(2))
    again.flush_all()

    read_back = read_archive(
        "XAUUSD", Timeframe.M5, "2026-09-16", market_tz=MARKET_TZ, root=tmp_path
    )
    assert len(read_back) == 3


def test_the_stored_schema_is_fixed(tmp_path: Path) -> None:
    """A file written by one version must read in another."""
    import pandas as pd

    archive = LiveCandleArchive(root=tmp_path)
    archive.add(candle(0))
    archive.flush_all()
    frame = pd.read_parquet(
        archive_path("XAUUSD", Timeframe.M5, "2026-09-16", root=tmp_path)
    )
    assert tuple(frame.columns) == COLUMNS


def test_a_missing_archive_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="observer writes these as it runs"):
        read_archive(
            "XAUUSD", Timeframe.M5, "2026-01-01", market_tz=MARKET_TZ, root=tmp_path
        )


# ── The comparison ────────────────────────────────────────────────────────────


def detections_from(candles: list[Candle]):
    engine = AnalysisEngine(
        [cross_agent()], account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ
    )
    return engine.feed(candles)


def test_identical_inputs_compare_clean(candles: list[Candle]) -> None:
    produced = detections_from(candles[:900])
    result = compare_module.compare(
        produced, produced, market_date="2026-09-16", symbol="XAUUSD",
        timeframe="M5", candles=900,
    )
    assert result.identical
    assert result.matched == len(produced)
    assert "IDENTICAL" in result.render()


def test_a_detection_the_live_run_never_stored_is_missing(
    candles: list[Candle],
) -> None:
    produced = detections_from(candles[:900])
    assert produced, "the fixture produced no detections to drop"
    result = compare_module.compare(
        produced[:-1], produced, market_date="2026-09-16", symbol="XAUUSD",
        timeframe="M5", candles=900,
    )
    assert not result.identical
    assert result.missing == [produced[-1].detection_id]
    assert result.extra == []
    assert "DIFFERENCES FOUND" in result.render()


def test_a_detection_the_replay_does_not_produce_is_extra(
    candles: list[Candle],
) -> None:
    produced = detections_from(candles[:900])
    result = compare_module.compare(
        produced, produced[:-1], market_date="2026-09-16", symbol="XAUUSD",
        timeframe="M5", candles=900,
    )
    assert result.extra == [produced[-1].detection_id]
    assert result.missing == []


def test_the_same_id_with_different_content_is_mismatched(
    candles: list[Candle],
) -> None:
    """The most serious case: the id is a pure function of the candle."""
    produced = detections_from(candles[:900])
    tampered = list(produced)
    tampered[0] = produced[0].model_copy(update={"price": 1.0})

    result = compare_module.compare(
        tampered, produced, market_date="2026-09-16", symbol="XAUUSD",
        timeframe="M5", candles=900,
    )
    assert len(result.mismatched) == 1
    detection_id, fields = result.mismatched[0]
    assert detection_id == produced[0].detection_id
    assert "price" in fields
    assert "differs on" in result.render()


def test_a_schema_version_bump_is_not_a_mismatch(candles: list[Candle]) -> None:
    """Document metadata, not an observation.

    Without this, a schema bump would report thousands of mismatched detections and bury
    a real difference in the noise.
    """
    produced = detections_from(candles[:900])
    bumped = [d.model_copy(update={"schema_version": 99}) for d in produced]
    result = compare_module.compare(
        bumped, produced, market_date="2026-09-16", symbol="XAUUSD",
        timeframe="M5", candles=900,
    )
    assert result.identical


def test_replaying_the_archive_reproduces_the_live_detections(tmp_path: Path, candles) -> None:
    """End to end: archive a session, replay the file, compare.

    This is the whole mechanism in one test. If the round trip lost anything -- a float, a
    timezone, a volume -- the comparison would find differences here.
    """
    subset = candles[:900]
    archive = LiveCandleArchive(root=tmp_path)
    for item in subset:
        archive.add(item)
    archive.flush_all()

    live = detections_from(subset)
    # Read back whichever day the archive actually wrote, so the test does not depend on
    # the fixture's first date.
    dates = sorted({c.open_time.market_date for c in subset})
    replayed = []
    for day in dates:
        try:
            replayed.extend(
                read_archive(
                    "XAUUSD", Timeframe.M5, day, market_tz=MARKET_TZ, root=tmp_path
                )
            )
        except FileNotFoundError:
            continue
    replayed_detections = detections_from(sorted(replayed, key=lambda c: c.open_time.utc))

    result = compare_module.compare(
        live, replayed_detections, market_date=dates[0], symbol="XAUUSD",
        timeframe="M5", candles=len(subset),
    )
    assert result.identical, result.render()


# ── 9A: the same parity claim, on either symbol ───────────────────────────────


@pytest.mark.parametrize("symbol", ["XAUUSD", "XAGUSD"])
def test_archive_replay_parity_holds_for_either_symbol(
    tmp_path: Path, request: pytest.FixtureRequest, symbol: str
) -> None:
    """§82 on both instruments (9A).

    The archive is keyed by symbol and broker date, and the detection id is salted with the
    symbol, so this is also what proves one symbol's archive cannot be replayed into the
    other's comparison and report itself identical.
    """
    candles = request.getfixturevalue(
        "candles" if symbol == "XAUUSD" else "silver_candles"
    )[:900]
    assert {c.symbol for c in candles} == {symbol}

    archive = LiveCandleArchive(root=tmp_path)
    for candle in candles:
        archive.add(candle)
    archive.flush_all()

    live = detections_from(candles)
    assert live, f"{symbol} produced no detections, so parity would be vacuous"

    replayed = []
    for day in sorted({c.open_time.market_date for c in candles}):
        try:
            replayed.extend(
                read_archive(symbol, Timeframe.M5, day, market_tz=MARKET_TZ, root=tmp_path)
            )
        except FileNotFoundError:
            continue

    result = compare_module.compare(
        live,
        detections_from(replayed),
        market_date="2026-09-16",
        symbol=symbol,
        timeframe="M5",
        candles=len(candles),
    )
    assert result.identical, result.render()
    assert result.matched == len(live)


def test_one_symbols_archive_is_not_the_others(
    tmp_path: Path, candles: list[Candle], silver_candles: list[Candle]
) -> None:
    """Both archived side by side, and each reads back only its own (9A)."""
    archive = LiveCandleArchive(root=tmp_path)
    for candle in [*candles[:300], *silver_candles[:300]]:
        archive.add(candle)
    archive.flush_all()

    day = candles[0].open_time.market_date
    gold = read_archive("XAUUSD", Timeframe.M5, day, market_tz=MARKET_TZ, root=tmp_path)
    silver = read_archive("XAGUSD", Timeframe.M5, day, market_tz=MARKET_TZ, root=tmp_path)
    assert gold and silver
    assert {c.symbol for c in gold} == {"XAUUSD"}
    assert {c.symbol for c in silver} == {"XAGUSD"}
    # Different instruments, so different prices: an archive serving one file for both
    # would make these equal.
    assert gold[0].close != silver[0].close
