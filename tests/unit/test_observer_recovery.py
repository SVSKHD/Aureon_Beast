"""Observer restart recovery (§75).

The claim: kill the observer after candle N, restart it, and the result is **neither
a duplicate detection nor a gap**.

Both halves matter and they pull against each other. Resuming too conservatively
re-processes candles and risks duplicates; resuming too aggressively skips the
candles that closed while the process was down. The cursor plus the idempotent
outbox is what lets the observer overlap safely: re-deriving a detection is harmless
because its id is a pure function of the candle, so the second enqueue is a no-op.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aureon.config import AureonConfig
from aureon.models.enums import Timeframe
from aureon.models.market import Candle
from aureon.outbox.local_outbox import LocalOutbox
from aureon.outbox.outbox_worker import OutboxWorker
from aureon.services.observer_state import ObserverState
from aureon.storage import paths
from aureon.storage.detection_repository import DetectionRepository
from main_observer import Observer
from tests.conftest import MARKET_TZ, FakeLiveProvider, InMemoryFirestore, cross_agent


def make_config(tmp_path: Path) -> AureonConfig:
    return AureonConfig.from_env(
        env={
            "AUREON_ACCOUNT_SCOPE": "primary",
            "AUREON_MARKET_TZ": MARKET_TZ,
            "AUREON_SYMBOLS": "XAUUSD",
            "AUREON_TIMEFRAMES": "M5",
            "AUREON_OUTBOX_PATH": str(tmp_path / "outbox.db"),
            "AUREON_OBSERVER_STATE_PATH": str(tmp_path / "observer_state.json"),
        }
    )


def build(
    tmp_path: Path, candles: list[Candle], firestore: InMemoryFirestore
) -> tuple[Observer, LocalOutbox, FakeLiveProvider]:
    """An observer wired to a fake feed and an in-memory Firestore."""
    config = make_config(tmp_path)
    provider = FakeLiveProvider(candles)
    outbox = LocalOutbox(config.outbox_path)
    worker = OutboxWorker(outbox, DetectionRepository(firestore).upsert_payload)
    observer = Observer(
        config,
        provider,
        outbox=outbox,
        worker=worker,
        state=ObserverState(config.observer_state_path),
        agents=[cross_agent()],
    )
    return observer, outbox, provider


def run_through(observer: Observer, provider: FakeLiveProvider, candles: list[Candle]) -> None:
    """Advance the clock candle by candle, polling as a live loop would."""
    for candle in candles:
        provider.advance_to_close_of(candle)
        observer.market_engine.poll_once()


def kill(observer: Observer, outbox: LocalOutbox) -> None:
    """Simulate an abrupt death: no graceful drain, no final state write.

    The worker thread is stopped first because a real process kill takes its threads
    with it -- leaving it running against a closed database would only be testing an
    artifact of the harness.
    """
    observer.worker.stop()
    if observer.heartbeat is not None:
        observer.heartbeat.stop()
    outbox.close()


def stored_ids(firestore: InMemoryFirestore) -> list[str]:
    # From paths.DETECTIONS, not a literal: collections are prefixed (decision 111), and a
    # hardcoded "detections/" here matched nothing once the prefix landed. The negative
    # assertions then passed vacuously (empty vs empty) while the positive one failed --
    # which is how a stale literal in a test helper wastes an afternoon.
    return sorted(
        str(doc["detection_id"]) for path, doc in firestore.docs.items()
        if path.startswith(f"{paths.DETECTIONS}/")
    )


#: Kill points, chosen rather than sampled. The fixture has a 49-hour weekend gap
#: after index 1427, and a restart whose cursor sits in the ~151 candles AFTER that gap
#: is where a reach-back measured in wall-clock MINUTES rather than in CANDLES starves:
#: the request lands inside the gap, the fresh engine's EMAs never converge, and the
#: restart silently loses a detection the uninterrupted run produced. 1500 is the point
#: that caught it; 1429 is the extreme, with a single post-gap candle to reach back
#: through. 400 and 900 are ordinary mid-history restarts.
@pytest.mark.parametrize("kill_at", [400, 900, 1429, 1500])
def test_restart_produces_no_duplicates_and_no_gap(
    tmp_path: Path, candles: list[Candle], kill_at: int
) -> None:
    firestore = InMemoryFirestore()

    # The clock starts before the first candle, so startup() has nothing to cold-start
    # from and every candle is genuinely observed live by run_through(). Pre-setting
    # the clock would let the cold-start backfill swallow the run instead.

    # ── First run: observe up to candle `kill_at`, then die abruptly ──────────
    first, outbox_a, provider_a = build(tmp_path, candles, firestore)
    first.startup()
    run_through(first, provider_a, candles[:kill_at])
    first.worker.flush()
    delivered_first = set(stored_ids(firestore))
    kill(first, outbox_a)  # no shutdown(); state was saved per candle close

    cursor = ObserverState(first.config.observer_state_path).get("XAUUSD", Timeframe.M5)
    assert cursor == candles[kill_at - 1].open_time.utc, "cursor did not survive the kill"

    # ── Second run: same state files, resuming where the first died ───────────
    second, outbox_b, provider_b = build(tmp_path, candles, firestore)
    # The clock is where the first run left it: nothing new has closed yet, so
    # startup() only re-warms the engine.
    provider_b.set_clock(candles[kill_at - 1].close_time)
    second.startup()
    run_through(second, provider_b, candles[kill_at:])
    second.worker.flush()

    delivered_all = stored_ids(firestore)

    # ── A single uninterrupted run, for comparison ────────────────────────────
    clean_store = InMemoryFirestore()
    clean, clean_outbox, clean_provider = build(tmp_path / "clean", candles, clean_store)
    clean.startup()
    run_through(clean, clean_provider, candles)
    clean.worker.flush()
    expected = stored_ids(clean_store)

    # No gap: the restart saw everything the clean run saw.
    assert delivered_all == expected
    # No duplicates: ids are unique, and the id is the document key so a repeat
    # overwrites rather than adding.
    assert len(delivered_all) == len(set(delivered_all))
    # The first run's work was not lost or re-done into a different document.
    assert delivered_first <= set(delivered_all)
    kill(clean, clean_outbox)
    kill(second, outbox_b)


def test_startup_flushes_a_backlog_before_observing(
    tmp_path: Path, candles: list[Candle]
) -> None:
    """§75 step 3: queued detections are delivered before new ones are produced.

    Simulated by queueing while Firestore is down, then restarting with it healthy.
    """
    firestore = InMemoryFirestore()
    first, outbox_a, provider_a = build(tmp_path, candles, firestore)
    firestore.fail_next = 10_000  # Firestore is down for the whole first run

    provider_a.set_clock(candles[599].close_time)
    first.startup()
    run_through(first, provider_a, candles[:600])
    queued = outbox_a.pending_count()
    assert queued > 0, "expected an undelivered backlog"
    assert not stored_ids(firestore)
    outbox_a.close()

    # Restart with Firestore healthy.
    firestore.fail_next = 0
    second, outbox_b, _ = build(tmp_path, candles, firestore)
    second.worker.flush()
    assert outbox_b.pending_count() == 0
    assert len(stored_ids(firestore)) == queued
    outbox_b.close()


def test_a_lost_cursor_falls_back_without_losing_detections(
    tmp_path: Path, candles: list[Candle]
) -> None:
    """A corrupt cursor file must degrade to a re-scan, not to silence.

    Re-deriving detections is safe (idempotent ids); silently skipping them is not.
    """
    firestore = InMemoryFirestore()
    first, outbox_a, provider_a = build(tmp_path, candles, firestore)
    provider_a.set_clock(candles[699].close_time)
    first.startup()
    run_through(first, provider_a, candles[:700])
    first.worker.flush()
    before = set(stored_ids(firestore))
    outbox_a.close()

    # Corrupt the cursor, as an interrupted non-atomic write would have.
    Path(first.config.observer_state_path).write_text("{ truncated", encoding="utf-8")

    second, outbox_b, provider_b = build(tmp_path, candles, firestore)
    provider_b.set_clock(candles[699].close_time)
    second.startup()
    second.worker.flush()
    after = set(stored_ids(firestore))

    # Nothing lost, and nothing duplicated: the same ids come back.
    assert before <= after
    assert len(after) == len({*after})
    outbox_b.close()


def test_re_polling_without_new_candles_is_a_no_op(
    tmp_path: Path, candles: list[Candle]
) -> None:
    """Idle polling must not re-emit the last candle's detections."""
    firestore = InMemoryFirestore()
    observer, outbox, provider = build(tmp_path, candles, firestore)
    provider.set_clock(candles[499].close_time)
    observer.startup()
    run_through(observer, provider, candles[:500])
    observer.worker.flush()
    baseline = len(stored_ids(firestore))

    for _ in range(5):
        assert observer.market_engine.poll_once() == []
    observer.worker.flush()
    assert len(stored_ids(firestore)) == baseline
    outbox.close()
