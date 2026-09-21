"""Durable outbox behaviour (§83).

The §83 scenario, verbatim: Firestore raises for 50 enqueued detections, then
recovers. Assert every one is delivered **exactly once**, and that ``delivered_at``
is set only after a successful write.

That last point is the one worth being pedantic about. If the row were marked before
the remote write, a crash in between would lose the detection silently -- the row
would look delivered and nothing would ever retry it. Marking afterwards means the
worst case is a duplicate *attempt*, which the idempotent ``set()`` absorbs.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from aureon.engine.analysis_engine import AnalysisEngine
from aureon.models.base import utc_now
from aureon.models.detection import Detection
from aureon.models.market import Candle
from aureon.outbox.local_outbox import LocalOutbox
from aureon.outbox.outbox_worker import OutboxWorker
from aureon.storage.detection_repository import DetectionRepository
from tests.conftest import ACCOUNT_SCOPE, MARKET_TZ, InMemoryFirestore, cross_agent


@pytest.fixture
def outbox(tmp_path: Path) -> LocalOutbox:
    return LocalOutbox(tmp_path / "outbox.db")


@pytest.fixture
def detections(candles: list[Candle]) -> list[Detection]:
    """Fifty real detections, from the roster the observer actually runs.

    The whole roster rather than ``ema_cross`` alone: the outbox queues whatever the
    engine produces, and this fixture needs fifty of them. An earlier version used the
    cross agent alone and happened to get seventy at 9/21 -- at 20/50 the same fixture
    yields twenty-nine, so the count was resting on a tuning parameter that has nothing
    to do with the outbox.
    """
    from aureon.agents.breakout_agent import BreakoutAgent
    from aureon.agents.liquidity_agent import LiquidityAgent
    from aureon.agents.rsi_agent import RsiAgent
    from aureon.agents.wick_agent import WickAgent
    from aureon.engine.levels import LevelTracker

    levels = LevelTracker()  # shared by liquidity and breakout (§15, §17)
    engine = AnalysisEngine(
        [
            cross_agent(),
            RsiAgent(),
            WickAgent(point=0.01),
            LiquidityAgent(point=0.01, level_tracker=levels),
            BreakoutAgent(point=0.01, level_tracker=levels),
        ],
        account_scope=ACCOUNT_SCOPE,
        market_tz=MARKET_TZ,
    )
    produced = engine.feed(candles)
    assert len(produced) >= 50, f"fixture produced only {len(produced)} detections"
    return produced[:50]


def test_fifty_enqueues_survive_an_outage_and_deliver_exactly_once(
    outbox: LocalOutbox, detections: list[Detection]
) -> None:
    """The §83 acceptance scenario."""
    assert outbox.enqueue_many(detections) == 50

    deliveries: list[str] = []
    down = [True]

    def deliver(payload: dict[str, object]) -> None:
        if down[0]:
            raise ConnectionError("firestore unreachable")
        deliveries.append(str(payload["detection_id"]))

    worker = OutboxWorker(outbox, deliver, initial_backoff=0.001)

    # Outage: repeated attempts, nothing delivered, nothing lost.
    for _ in range(10):
        assert worker.drain_once() == 0
    assert outbox.pending_count() == 50
    assert outbox.delivered_count() == 0
    assert deliveries == []

    # Recovery.
    down[0] = False
    assert worker.flush() == 50

    assert outbox.pending_count() == 0
    assert outbox.delivered_count() == 50
    # Exactly once: no detection delivered twice.
    assert len(deliveries) == 50
    assert len(set(deliveries)) == 50
    assert set(deliveries) == {d.detection_id for d in detections}


def test_delivered_at_is_set_only_after_a_successful_write(
    outbox: LocalOutbox, detections: list[Detection]
) -> None:
    """A failed delivery must leave the row pending, with delivered_at unset."""
    target = detections[0]
    outbox.enqueue(target)

    def always_fails(payload: dict[str, object]) -> None:
        raise ConnectionError("nope")

    worker = OutboxWorker(outbox, always_fails, initial_backoff=0.001)
    worker.drain_once()

    row = outbox.get(target.detection_id)
    assert row is not None
    assert row.delivered_at is None
    assert row.delivered is False
    assert row.attempts == 1
    assert "ConnectionError" in (row.last_error or "")


def test_a_row_is_marked_only_after_the_sink_returns(
    outbox: LocalOutbox, detections: list[Detection]
) -> None:
    """Pin the ordering: the row is still unmarked while the sink is executing.

    Checked from inside the delivery function, which is the only place the
    intermediate state is observable.
    """
    target = detections[0]
    outbox.enqueue(target)
    observed: list[bool] = []

    def deliver(payload: dict[str, object]) -> None:
        row = outbox.get(str(payload["detection_id"]))
        observed.append(row.delivered if row else False)

    OutboxWorker(outbox, deliver).drain_once()

    assert observed == [False], "row was marked delivered before the write completed"
    assert outbox.get(target.detection_id).delivered is True


def test_enqueue_is_idempotent_on_detection_id(
    outbox: LocalOutbox, detections: list[Detection]
) -> None:
    """Re-observing a candle must not queue its detection twice.

    The observer's startup backfill re-derives detections around its cursor, so this
    is the normal path, not an edge case.
    """
    assert outbox.enqueue(detections[0]) is True
    assert outbox.enqueue(detections[0]) is False
    assert len(outbox) == 1
    assert outbox.enqueue_many(detections) == len(detections) - 1


def test_an_already_delivered_detection_is_not_resurrected(
    outbox: LocalOutbox, detections: list[Detection]
) -> None:
    """Re-enqueueing after delivery must not send it again."""
    target = detections[0]
    outbox.enqueue(target)
    outbox.mark_delivered(target.detection_id)

    assert outbox.enqueue(target) is False
    assert outbox.pending_count() == 0

    sent: list[str] = []
    OutboxWorker(outbox, lambda p: sent.append(str(p["detection_id"]))).flush()
    assert sent == []


def test_the_queue_survives_a_process_kill(tmp_path: Path, detections: list[Detection]) -> None:
    """A crash must not lose queued detections.

    Simulated by closing the connection without a graceful drain and reopening the
    same file -- which is what a killed process leaves behind.
    """
    path = tmp_path / "outbox.db"
    first = LocalOutbox(path)
    first.enqueue_many(detections)
    first.mark_delivered(detections[0].detection_id)
    first.close()  # no flush, no drain: the process "died"

    reopened = LocalOutbox(path)
    assert reopened.pending_count() == len(detections) - 1
    assert reopened.delivered_count() == 1


def test_purge_never_removes_an_undelivered_row(
    outbox: LocalOutbox, detections: list[Detection]
) -> None:
    """Housekeeping must not be able to drop something not yet in Firestore."""
    outbox.enqueue_many(detections)
    outbox.mark_delivered(detections[0].detection_id)

    removed = outbox.purge_delivered(utc_now() + timedelta(days=1))
    assert removed == 1
    assert outbox.pending_count() == len(detections) - 1


def test_delivery_through_the_real_repository_is_an_idempotent_set(
    outbox: LocalOutbox, detections: list[Detection], firestore: InMemoryFirestore
) -> None:
    """Two deliveries of one detection leave exactly one document.

    This is what makes retrying an ambiguous failure safe: the id is derived from the
    candle, so the second write overwrites the first with identical content.
    """
    repository = DetectionRepository(firestore)
    outbox.enqueue_many(detections[:5])

    worker = OutboxWorker(outbox, repository.upsert_payload)
    worker.flush()
    assert len(firestore.docs) == 5

    # Deliver the same payloads again, as a retry after an ambiguous failure would.
    for detection in detections[:5]:
        repository.upsert(detection)
    assert len(firestore.docs) == 5


def test_a_mid_batch_failure_stops_the_batch(
    outbox: LocalOutbox, detections: list[Detection]
) -> None:
    """One outage should not burn an attempt on every queued row.

    Without this, a single Firestore outage would inflate the attempt count of the
    whole backlog, making the counters useless for spotting a genuinely poisonous row.
    """
    outbox.enqueue_many(detections[:10])
    calls = [0]

    def deliver(payload: dict[str, object]) -> None:
        calls[0] += 1
        if calls[0] > 3:
            raise ConnectionError("died partway")

    worker = OutboxWorker(outbox, deliver, initial_backoff=0.001)
    delivered = worker.drain_once()

    assert delivered == 3
    assert calls[0] == 4  # three succeeded, the fourth failed and stopped the batch
    assert outbox.pending_count() == 7
    assert worker.backoff > 0


# ── 9A: the §83 guarantee on either symbol ────────────────────────────────────


def roster_for(symbol: str) -> list:
    """The observer's own roster for one symbol, with that symbol's tuning (9A)."""
    from aureon.config import AureonConfig
    from main_observer import default_agents

    config = AureonConfig(
        symbols=("XAUUSD", "XAGUSD"),
        evaluation_rules={"XAUUSD": "XAU_OUTCOME_V2", "XAGUSD": "XAG_OUTCOME_V1"},
    )
    return default_agents(config, symbol=symbol)


@pytest.mark.parametrize("symbol", ["XAUUSD", "XAGUSD"])
def test_exactly_once_delivery_holds_for_either_symbol(
    tmp_path: Path, request: pytest.FixtureRequest, symbol: str
) -> None:
    """§83 on both instruments (9A).

    The outbox keys rows by ``detection_id``, which is salted with the symbol -- so this is
    also the test that two symbols' detections cannot collide in the queue. Fifty of one
    symbol's detections, an outage, a recovery: fifty deliveries, no duplicates.
    """
    candles = request.getfixturevalue(
        "candles" if symbol == "XAUUSD" else "silver_candles"
    )
    # Half the week: the whole roster produces far more than fifty detections in it, and
    # feeding the full fixture twice per symbol costs minutes for no extra proof.
    engine = AnalysisEngine(
        roster_for(symbol), account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ
    )
    produced = engine.feed(candles[:900])[:50]
    assert len(produced) == 50, f"{symbol} produced only {len(produced)} detections"
    assert {d.symbol for d in produced} == {symbol}

    outbox = LocalOutbox(tmp_path / f"{symbol}.db")
    assert outbox.enqueue_many(produced) == 50

    deliveries: list[str] = []
    down = [True]

    def deliver(payload: dict[str, object]) -> None:
        if down[0]:
            raise ConnectionError("firestore unreachable")
        deliveries.append(str(payload["detection_id"]))

    worker = OutboxWorker(outbox, deliver, initial_backoff=0.001)
    assert worker.drain_once() == 0
    assert outbox.pending_count() == 50

    down[0] = False
    assert worker.flush() == 50
    assert outbox.pending_count() == 0
    assert sorted(deliveries) == sorted(d.detection_id for d in produced)
    assert len(set(deliveries)) == 50


def test_both_symbols_share_one_queue_without_colliding(
    tmp_path: Path, candles: list[Candle], silver_candles: list[Candle]
) -> None:
    """One observer, one outbox, two symbols (9A).

    The queue is a single file, so the thing to prove is that the id salt keeps the two
    symbols' rows distinct: a collision would silently drop one symbol's detection as an
    already-queued duplicate.
    """
    gold = AnalysisEngine(
        roster_for("XAUUSD"), account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ
    ).feed(candles[:900])[:30]
    silver = AnalysisEngine(
        roster_for("XAGUSD"), account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ
    ).feed(silver_candles[:900])[:30]

    outbox = LocalOutbox(tmp_path / "shared.db")
    assert outbox.enqueue_many(gold + silver) == 60

    delivered: list[dict[str, object]] = []
    worker = OutboxWorker(outbox, delivered.append, initial_backoff=0.001)
    assert worker.flush() == 60

    symbols = [str(row["symbol"]) for row in delivered]
    assert symbols.count("XAUUSD") == 30
    assert symbols.count("XAGUSD") == 30


# ── 11B: the shutdown flush that delivered nothing ───────────────────────────


def test_a_stopped_worker_flushes_nothing_unless_the_flush_is_final(
    outbox: LocalOutbox, detections: list[Detection]
) -> None:
    """The flaw, pinned from both sides.

    ``drain_once`` checks the stop flag before every row so that ``stop()`` aborts a long
    batch promptly. The consequence nobody noticed: ``stop(); flush()`` -- the order the
    observer's shutdown has used since Phase 2 -- delivered **zero** rows, every time. The
    "delivered N during shutdown" log line could not fire, and the module docstring's
    promise that a clean stop leaves nothing queued was false.

    Both halves are asserted, because fixing it by simply dropping the check would remove
    the abort that makes a stop prompt.
    """
    queued = outbox.enqueue_many(detections[:5])
    assert queued == 5

    delivered: list[dict[str, object]] = []
    worker = OutboxWorker(outbox, delivered.append, initial_backoff=0.001)
    worker.stop()

    assert worker.flush() == 0, "a stopped worker still refuses an ordinary flush"
    assert outbox.pending_count() == 5

    assert worker.flush(final=True) == 5
    assert outbox.pending_count() == 0
    assert len(delivered) == 5


def test_a_final_flush_still_stops_at_a_failing_row(
    outbox: LocalOutbox, detections: list[Detection]
) -> None:
    """``final`` waives the stop flag and nothing else.

    The assertion that matters is the ATTEMPT COUNT, not the pending count. A first draft
    checked only that nothing was delivered and that five rows were still queued -- and both
    of those hold just as well for a worker that hammered all five rows against a dead
    Firestore, which is the behaviour the batch-stops-at-the-first-failure rule exists to
    prevent. Planting ``continue`` for ``return`` proved the test vacuous.
    """
    outbox.enqueue_many(detections[:5])
    attempts: list[object] = []

    def refuse(payload: dict[str, object]) -> None:
        attempts.append(payload)
        raise ConnectionError("Firestore is unreachable")

    worker = OutboxWorker(outbox, refuse, initial_backoff=0.001)
    worker.stop()
    assert worker.flush(final=True) == 0
    assert outbox.pending_count() == 5
    assert len(attempts) == 1, "one outage must not cost five rows an attempt each"
