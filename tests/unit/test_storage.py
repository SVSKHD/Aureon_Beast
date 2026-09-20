"""Repository behaviour (§19, §59, §67).

Runs against an in-memory double. It cannot validate Firestore's own semantics --
transactions and listeners need the emulator, which Phase 4 introduces -- but it does
pin the properties the repositories are responsible for: idempotent writes, and the
throttling that keeps the observer from writing state on every tick.
"""

from __future__ import annotations

from datetime import timedelta

from aureon.engine.analysis_engine import AnalysisEngine
from aureon.models.base import utc_now
from aureon.models.enums import MarketState, Timeframe
from aureon.models.market import Candle
from aureon.models.system import SymbolState, SystemState
from aureon.storage import paths
from aureon.storage.detection_repository import DetectionRepository
from aureon.storage.system_state_repository import (
    HeartbeatRepository,
    SystemStateRepository,
)
from tests.conftest import ACCOUNT_SCOPE, MARKET_TZ, InMemoryFirestore, cross_agent


def some_detections(candles: list[Candle], count: int = 5) -> list:
    engine = AnalysisEngine(
        [cross_agent()], account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ
    )
    return engine.feed(candles)[:count]


# ── Detections ────────────────────────────────────────────────────────────────


def test_a_detection_is_written_at_its_deterministic_id(
    firestore: InMemoryFirestore, candles: list[Candle]
) -> None:
    detection = some_detections(candles, 1)[0]
    path = DetectionRepository(firestore).upsert(detection)
    assert path == paths.detection_path(detection.detection_id)
    assert path in firestore.docs


def test_upsert_is_idempotent(firestore: InMemoryFirestore, candles: list[Candle]) -> None:
    """Writing the same detection twice leaves one document.

    This is what makes retrying an ambiguous outbox delivery safe.
    """
    detection = some_detections(candles, 1)[0]
    repository = DetectionRepository(firestore)
    repository.upsert(detection)
    repository.upsert(detection)
    assert len(firestore.docs) == 1
    assert firestore.writes == 2  # two writes, one document


def test_a_stored_detection_round_trips(
    firestore: InMemoryFirestore, candles: list[Candle]
) -> None:
    """The stored form must validate back into the model, or Phase 3 cannot read it."""
    detection = some_detections(candles, 1)[0]
    repository = DetectionRepository(firestore)
    repository.upsert(detection)

    loaded = repository.get(detection.detection_id)
    assert loaded is not None
    assert loaded.detection_id == detection.detection_id
    assert loaded.event_key == detection.event_key
    assert loaded.model_dump(mode="json") == detection.model_dump(mode="json")


def test_a_missing_detection_reads_as_none(firestore: InMemoryFirestore) -> None:
    repository = DetectionRepository(firestore)
    assert repository.get("nope") is None
    assert repository.exists("nope") is False


def test_a_payload_without_an_id_is_refused(firestore: InMemoryFirestore) -> None:
    """An empty doc id makes Firestore auto-generate one, defeating idempotency."""
    repository = DetectionRepository(firestore)
    try:
        repository.upsert_payload({"symbol": "XAUUSD"})
    except ValueError as exc:
        assert "detection_id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a ValueError")


# ── System state: one document per symbol (9A) ────────────────────────────────


def gold(**overrides) -> SymbolState:
    params = {
        "symbol": "XAUUSD",
        "timeframe": Timeframe.M5,
        "market_state": MarketState.OPEN,
    }
    params.update(overrides)
    return SymbolState(**params)


def silver(**overrides) -> SymbolState:
    return gold(symbol="XAGUSD", **overrides)


def test_state_writes_are_throttled(firestore: InMemoryFirestore) -> None:
    """An unthrottled tick-rate write costs money and tells a reader nothing new."""
    repository = SystemStateRepository(firestore, min_interval_seconds=5.0)
    now = utc_now()
    state = SystemState(symbols=(gold(),))

    assert repository.write(state, now=now) is True
    assert repository.write(state, now=now + timedelta(seconds=1)) is False
    assert repository.write(state, now=now + timedelta(seconds=4.9)) is False
    assert repository.write(state, now=now + timedelta(seconds=5)) is True
    assert firestore.writes == 2


def test_the_throttle_is_per_symbol(firestore: InMemoryFirestore) -> None:
    """The reason the documents were split at all.

    With one shared document, gold's candle close resets the timer and silver's state is
    suppressed for the next few seconds -- so the symbol a reader is looking at can be
    stale because of a symbol they are not.
    """
    repository = SystemStateRepository(firestore, min_interval_seconds=5.0)
    now = utc_now()

    assert repository.write(SystemState(symbols=(gold(),)), now=now) is True
    # One second later, silver writes for the first time and must NOT be throttled by
    # gold's write.
    later = now + timedelta(seconds=1)
    assert repository.write(SystemState(symbols=(silver(),)), now=later) is True
    assert firestore.writes == 2
    # And gold is still throttled on its own timer.
    assert (
        repository.write(SystemState(symbols=(gold(),)), now=now + timedelta(seconds=2))
        is False
    )


def test_each_document_holds_only_its_own_symbol(firestore: InMemoryFirestore) -> None:
    """A reader of XAGUSD_M5 must not find gold's panel inside it and have to work out
    which one is current."""
    repository = SystemStateRepository(firestore)
    repository.write(SystemState(symbols=(gold(), silver())))

    assert set(firestore.docs) == {
        f"{paths.SYSTEM_STATE}/XAUUSD_M5",
        f"{paths.SYSTEM_STATE}/XAGUSD_M5",
    }
    one = repository.read_symbol("XAGUSD", Timeframe.M5)
    assert one is not None
    assert [s.symbol for s in one.symbols] == ["XAGUSD"]


def test_a_state_with_no_symbols_writes_nothing(firestore: InMemoryFirestore) -> None:
    """Its only other fields are derived copies of the heartbeats and settings documents,
    so there is nothing to record that is not already a document of its own."""
    assert SystemStateRepository(firestore).write(SystemState()) is False
    assert firestore.writes == 0


def test_a_candle_close_bypasses_the_throttle(firestore: InMemoryFirestore) -> None:
    """A new closed candle IS new information and must not wait for a timer."""
    repository = SystemStateRepository(firestore, min_interval_seconds=60.0)
    now = utc_now()
    repository.write(SystemState(symbols=(gold(),)), now=now)
    assert (
        repository.write(
            SystemState(symbols=(gold(),)), force=True, now=now + timedelta(seconds=1)
        )
        is True
    )
    assert firestore.writes == 2


def test_updated_at_is_stamped_by_the_repository(firestore: InMemoryFirestore) -> None:
    """Freshness is computed from this, so it must reflect the write, not the caller."""
    repository = SystemStateRepository(firestore)
    now = utc_now()
    repository.write(
        SystemState(updated_at=now - timedelta(hours=1), symbols=(gold(),)), now=now
    )
    stored = repository.read()
    assert stored is not None
    assert stored.updated_at == now


def test_a_merged_read_takes_the_newest_freshness(firestore: InMemoryFirestore) -> None:
    """A merged freshness that took the oldest would make a quiet symbol look like a dead
    observer."""
    repository = SystemStateRepository(firestore)
    now = utc_now()
    repository.write(SystemState(symbols=(gold(),)), now=now - timedelta(minutes=10))
    repository.write(SystemState(symbols=(silver(),)), now=now)

    merged = repository.read()
    assert merged is not None
    assert merged.updated_at == now
    assert [s.symbol for s in merged.symbols] == ["XAGUSD", "XAUUSD"]


def test_a_merged_read_ignores_the_pre_split_document(
    firestore: InMemoryFirestore,
) -> None:
    """A deployment that ran before 9A still has one, and merging it would report every
    symbol twice -- once live, once frozen at the split."""
    repository = SystemStateRepository(firestore)
    legacy = f"{paths.SYSTEM_STATE}/{paths.LEGACY_SYSTEM_STATE_DOC}"
    firestore.docs[legacy] = SystemState(symbols=(gold(), silver())).model_dump(
        mode="json"
    )
    repository.write(SystemState(symbols=(gold(),)))

    merged = repository.read()
    assert merged is not None
    assert [s.symbol for s in merged.symbols] == ["XAUUSD"], "the legacy doc is skipped"


def test_reading_a_symbol_that_was_never_written_is_none(
    firestore: InMemoryFirestore,
) -> None:
    assert SystemStateRepository(firestore).read_symbol("XAGUSD", Timeframe.M5) is None
    assert SystemStateRepository(firestore).read() is None


def test_symbol_state_round_trips(firestore: InMemoryFirestore) -> None:
    repository = SystemStateRepository(firestore)
    state = SystemState(
        symbols=(
            SymbolState(
                symbol="XAUUSD", timeframe=Timeframe.M5, market_state=MarketState.OPEN
            ),
        )
    )
    repository.write(state, force=True)
    stored = repository.read()
    assert stored is not None
    assert stored.symbols[0].symbol == "XAUUSD"
    assert stored.symbols[0].market_state is MarketState.OPEN


# ── Heartbeats ────────────────────────────────────────────────────────────────


def test_heartbeats_are_per_service_and_throttled(firestore: InMemoryFirestore) -> None:
    repository = HeartbeatRepository(firestore, min_interval_seconds=10.0)
    now = utc_now()

    assert repository.beat("observer", now=now) is True
    assert repository.beat("observer", now=now + timedelta(seconds=2)) is False
    # A different service has its own throttle.
    assert repository.beat("executor", now=now + timedelta(seconds=2)) is True

    assert paths.heartbeat_path("observer") in firestore.docs
    assert paths.heartbeat_path("executor") in firestore.docs


def test_a_heartbeat_round_trips_with_its_instance_id(firestore: InMemoryFirestore) -> None:
    """Two instances of one service must be distinguishable in /status."""
    repository = HeartbeatRepository(firestore)
    repository.beat("observer", instance_id="abc123", detail={"candles": 42})
    beat = repository.read("observer")
    assert beat is not None
    assert beat.service == "observer"
    assert beat.instance_id == "abc123"
    assert beat.detail["candles"] == 42


def test_read_all_returns_only_services_that_have_beaten(
    firestore: InMemoryFirestore,
) -> None:
    repository = HeartbeatRepository(firestore)
    repository.beat("observer")
    beats = repository.read_all()
    assert set(beats) == {"observer"}


def test_freshness_is_derived_not_stored(firestore: InMemoryFirestore) -> None:
    """A stored 'live' flag would keep claiming liveness after the writer died."""
    from aureon.models.enums import Freshness

    repository = HeartbeatRepository(firestore)
    now = utc_now()
    repository.beat("observer", now=now)
    beat = repository.read("observer")
    assert beat is not None
    assert beat.freshness(now=now) is Freshness.LIVE
    assert beat.freshness(now=now + timedelta(seconds=46)) is Freshness.STALE
    assert beat.freshness(now=now + timedelta(seconds=200)) is Freshness.OFFLINE
