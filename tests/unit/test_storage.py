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


# ── System state throttling ───────────────────────────────────────────────────


def test_state_writes_are_throttled(firestore: InMemoryFirestore) -> None:
    """An unthrottled tick-rate write costs money and tells a reader nothing new."""
    repository = SystemStateRepository(firestore, min_interval_seconds=5.0)
    now = utc_now()
    state = SystemState()

    assert repository.write(state, now=now) is True
    assert repository.write(state, now=now + timedelta(seconds=1)) is False
    assert repository.write(state, now=now + timedelta(seconds=4.9)) is False
    assert repository.write(state, now=now + timedelta(seconds=5)) is True
    assert firestore.writes == 2


def test_a_candle_close_bypasses_the_throttle(firestore: InMemoryFirestore) -> None:
    """A new closed candle IS new information and must not wait for a timer."""
    repository = SystemStateRepository(firestore, min_interval_seconds=60.0)
    now = utc_now()
    repository.write(SystemState(), now=now)
    assert repository.write(SystemState(), force=True, now=now + timedelta(seconds=1)) is True
    assert firestore.writes == 2


def test_updated_at_is_stamped_by_the_repository(firestore: InMemoryFirestore) -> None:
    """Freshness is computed from this, so it must reflect the write, not the caller."""
    repository = SystemStateRepository(firestore)
    now = utc_now()
    repository.write(
        SystemState(updated_at=now - timedelta(hours=1)),
        now=now,
    )
    stored = repository.read()
    assert stored is not None
    assert stored.updated_at == now


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
