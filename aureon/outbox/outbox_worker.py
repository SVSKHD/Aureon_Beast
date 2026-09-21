"""Background delivery of queued detections (§83).

Drains ``LocalOutbox`` into Firestore. The contract it upholds:

* **never drops** -- a row stays pending until a delivery actually succeeds;
* **marks only after success** -- so a crash mid-delivery costs a retry, never a
  detection;
* **backs off** -- a Firestore outage is global, not per-row, so the worker backs
  off as a whole instead of hammering the same failure once per queued detection.

Delivery is an idempotent ``set()`` keyed by ``detection_id`` (see
``DetectionRepository``), which is what makes a retry after an ambiguous failure
safe: if the first attempt actually landed, the retry overwrites it with identical
content.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from aureon.outbox.local_outbox import LocalOutbox, OutboxRow

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 1.0
DEFAULT_BATCH = 50
DEFAULT_INITIAL_BACKOFF = 1.0
DEFAULT_MAX_BACKOFF = 60.0

#: A delivery function. Receives the stored detection payload; raises on failure.
DeliverFn = Callable[[dict[str, object]], None]


class OutboxWorker:
    """Delivers pending outbox rows, retrying with exponential backoff."""

    def __init__(
        self,
        outbox: LocalOutbox,
        deliver: DeliverFn,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        batch_size: int = DEFAULT_BATCH,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
    ) -> None:
        self.outbox = outbox
        self.deliver = deliver
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff

        self._backoff = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.delivered_total = 0
        self.failed_attempts = 0

    # ── One pass ──────────────────────────────────────────────────────────────

    def drain_once(self, *, respect_stop: bool = True) -> int:
        """Attempt one batch. Returns the number delivered.

        Stops the batch at the first failure rather than continuing: if Firestore is
        down, the remaining rows in this batch will fail too, and burning through
        them would inflate every row's attempt count for one outage.

        ``respect_stop=False`` is for the deliberate final drain -- see ``flush``.
        """
        rows = self.outbox.pending(limit=self.batch_size)
        if not rows:
            self._backoff = 0.0
            return 0

        delivered = 0
        for row in rows:
            if respect_stop and self._stop.is_set():
                break
            if not self._deliver_row(row):
                self._grow_backoff()
                return delivered
            delivered += 1

        self._backoff = 0.0
        return delivered

    def _deliver_row(self, row: OutboxRow) -> bool:
        try:
            self.deliver(row.payload)
        except Exception as exc:  # noqa: BLE001 - any failure must leave the row pending
            self.failed_attempts += 1
            self.outbox.record_failure(row.detection_id, f"{type(exc).__name__}: {exc}")
            log.warning(
                "outbox delivery failed for %s (attempt %d): %s",
                row.detection_id,
                row.attempts + 1,
                exc,
            )
            return False

        # ONLY here, after the remote write returned successfully.
        self.outbox.mark_delivered(row.detection_id)
        self.delivered_total += 1
        return True

    def flush(self, *, max_passes: int = 1000, final: bool = False) -> int:
        """Drain until empty or nothing more can be delivered.

        Used at startup (§75), at a market close (11B) and at graceful shutdown, where
        waiting matters more than returning quickly. Stops as soon as a pass delivers
        nothing, so a persistent outage cannot spin here forever.

        ``final=True`` drains a worker whose thread has already been stopped, and exists
        because the obvious spelling was silently a no-op. ``drain_once`` checks the stop
        flag before every row so that ``stop()`` aborts a long batch promptly -- which
        also meant that ``stop(); flush()``, the order the observer's shutdown has used
        since Phase 2, delivered **zero** rows every time. The log line reporting how many
        were delivered during shutdown could not fire, and the module docstring's promise
        that "a clean stop leaves nothing queued" was false. A caller who has stopped the
        thread and is now asking, synchronously, for the queue to be drained means it.
        """
        total = 0
        for _ in range(max_passes):
            delivered = self.drain_once(respect_stop=not final)
            total += delivered
            if delivered == 0:
                break
        return total

    def _grow_backoff(self) -> None:
        self._backoff = (
            self.initial_backoff
            if self._backoff == 0.0
            else min(self._backoff * 2, self.max_backoff)
        )

    @property
    def backoff(self) -> float:
        return self._backoff

    # ── Thread ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Loop until stopped, honouring backoff after failures."""
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception:  # noqa: BLE001 - the worker must not die
                log.exception("outbox worker pass failed")
                self._grow_backoff()
            self._stop.wait(self._backoff or self.poll_seconds)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("worker already started")
        # daemon=True so a hung delivery cannot prevent interpreter shutdown; the
        # outbox is durable, so anything undelivered is simply picked up next boot.
        self._thread = threading.Thread(target=self.run, name="outbox-worker", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def __enter__(self) -> OutboxWorker:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
