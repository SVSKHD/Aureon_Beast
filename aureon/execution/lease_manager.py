"""Keeping a claim alive while a send is in flight (§29).

A lease is a promise with an expiry: "this executor owns this request until T". Renewing
it is not bookkeeping -- it is how a *slow* executor stays distinguishable from a *dead*
one. Without renewal, a send that takes longer than the lease would let a second
executor conclude the first had died and take over, and then both would resolve one
request.

The other half matters just as much: **once a lease is lost, this instance stops
touching the request**, mid-flight or not. Another worker may already own it, and an
order that was already sent is reconciliation's problem, not this instance's.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from aureon.models.base import to_utc, utc_now
from aureon.storage.trade_request_repository import LeaseLost, TradeRequestRepository

log = logging.getLogger(__name__)

# Renew at this fraction of the lease. Renewing at the very end leaves no room for the
# renewal's own round trip to be slow.
RENEW_AT_FRACTION = 0.5


class LeaseManager:
    """Renews a request's lease in the background while work is in flight."""

    def __init__(
        self,
        repository: TradeRequestRepository,
        executor_id: str,
        *,
        lease_seconds: float = 60.0,
    ) -> None:
        self.repository = repository
        self.executor_id = executor_id
        self.lease_seconds = lease_seconds
        self._lost: set[str] = set()
        self._lock = threading.Lock()

    def renew(self, request_id: str, *, now: datetime | None = None) -> bool:
        """Attempt one renewal. False means the lease is gone -- stand down.

        A lost lease is remembered, so a caller that keeps working cannot accidentally
        succeed on a later renewal after another worker has taken over.
        """
        try:
            self.repository.renew_lease(
                request_id,
                self.executor_id,
                lease_seconds=self.lease_seconds,
                now=to_utc(now or utc_now()),
            )
        except LeaseLost as exc:
            with self._lock:
                self._lost.add(request_id)
            log.warning("lease lost on %s: %s", request_id, exc)
            return False
        return True

    def lost(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._lost

    def forget(self, request_id: str) -> None:
        with self._lock:
            self._lost.discard(request_id)

    def renew_interval_seconds(self) -> float:
        return max(self.lease_seconds * RENEW_AT_FRACTION, 1.0)

    def keepalive(self, request_id: str, stop: threading.Event) -> threading.Thread:
        """Renew in a background thread until ``stop`` is set.

        Daemon, so a hung send cannot keep the process alive. If renewal fails the
        thread exits and ``lost()`` reports it; the worker checks that before resolving.
        """

        def loop() -> None:
            while not stop.wait(self.renew_interval_seconds()):
                if not self.renew(request_id):
                    return

        thread = threading.Thread(
            target=loop, name=f"lease-{request_id[:8]}", daemon=True
        )
        thread.start()
        return thread
