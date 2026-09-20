"""Performing Discord's cancel and close requests (§46, §47).

The executor is the only process that calls the broker, so a cancel or a close asked for
in Discord arrives here as a ``control_requests`` document and is performed under the same
claim/lease discipline as a trade.

## Failure here is usually the market winning a race

A cancel fails because the order just filled. A close fails because the position just
closed. Neither is an error to retry -- retrying a cancel does nothing, and retrying a
close could open a new position in the opposite direction if the original had already
gone. So a broker refusal resolves the control request to ``FAILED`` with the reason, and
the **monitor** records what actually happened to the order or position (§46, §47).

That is why Discord reports the reconciled outcome rather than this worker's return value.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from aureon.execution.broker_interface import BrokerError, BrokerInterface
from aureon.models.base import utc_now
from aureon.models.control import ControlRequest
from aureon.models.enums import ControlRequestKind, ControlRequestStatus, FailureCode
from aureon.storage.control_request_repository import (
    ControlClaimRejected,
    ControlRequestRepository,
)

log = logging.getLogger(__name__)


class ControlWorker:
    """Consumes ``control_requests`` and performs them at the broker."""

    def __init__(
        self,
        repository: ControlRequestRepository,
        broker: BrokerInterface,
        *,
        executor_id: str,
        lease_seconds: float = 60.0,
        poll_seconds: float = 2.0,
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.executor_id = executor_id
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.performed = 0
        self.refused = 0

    def process(self, control_id: str, *, now: datetime | None = None) -> ControlRequest | None:
        """Claim and perform one control request.

        Returns ``None`` when another executor claimed it first.
        """
        moment = now or utc_now()
        try:
            claimed = self.repository.claim(
                control_id, self.executor_id, lease_seconds=self.lease_seconds, now=moment
            )
        except ControlClaimRejected as exc:
            log.info("not ours: %s", exc)
            return None

        try:
            result = self._perform(claimed)
        except BrokerError as exc:
            # The outcome is unknown. Left EXECUTING rather than failed or retried: the
            # monitor will show what actually happened to the order or position.
            log.error(
                "unknown outcome for control %s (%s %s): %s -- left EXECUTING",
                control_id,
                claimed.kind.value,
                claimed.target,
                exc,
            )
            return None

        if result.ok:
            self.performed += 1
            # Deliberately NOT now=moment: the lease is checked against the real
            # clock, and `moment` is when the claim happened. Passing the claim time
            # would let an operation that outran its lease still resolve, which is the
            # exact case the check exists for.
            return self.repository.resolve(
                control_id, self.executor_id, ControlRequestStatus.COMPLETED
            )

        self.refused += 1
        log.info(
            "control %s refused by the broker: %s", control_id, result.message
        )
        return self.repository.resolve(
            control_id,
            self.executor_id,
            ControlRequestStatus.FAILED,
            failure_code=result.failure_code or FailureCode.BROKER_REJECTED,
            failure_message=result.message
            or "the broker refused; the monitor will record the actual state",
        )

    def _perform(self, request: ControlRequest):
        if request.kind is ControlRequestKind.CANCEL:
            return self.broker.cancel_order(int(request.target))
        return self.broker.close_position(int(request.target), request.volume)

    def poll_once(self) -> int:
        handled = 0
        for request in self.repository.list_by_status(ControlRequestStatus.REQUESTED):
            if self._stop.is_set():
                break
            if self.process(request.control_id) is not None:
                handled += 1
        return handled

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - a poll failure must not kill the worker
                log.exception("control worker poll failed")
            self._stop.wait(self.poll_seconds)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="control-worker", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
