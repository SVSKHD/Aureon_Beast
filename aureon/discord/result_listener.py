"""Report a confirmed request's eventual outcome back to Discord.

The runtime is local-only, so there is no Firestore document watch. A lightweight local
poll checks the authoritative trade_request row; this is intentionally independent of
broker execution and can never place an order. The poll is cheap SQLite I/O and stops as
soon as the first reportable state is observed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aureon.models.enums import TradeRequestStatus
from aureon.models.trade import TradeRequest

log = logging.getLogger(__name__)

REPORTABLE: frozenset[TradeRequestStatus] = frozenset(
    {
        TradeRequestStatus.FILLED,
        TradeRequestStatus.PARTIALLY_FILLED,
        TradeRequestStatus.PENDING,
        TradeRequestStatus.FAILED,
        TradeRequestStatus.FAILED_STALE,
        TradeRequestStatus.FAILED_RECONCILIATION,
        TradeRequestStatus.CANCELLED,
        TradeRequestStatus.EXPIRED,
    }
)


class RequestResultListener:
    """Poll one local request row and report its outcome once."""

    def __init__(
        self,
        repository: Any,
        request_id: str,
        report: Callable[[TradeRequest], Awaitable[None]],
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        poll_seconds: float = 2.0,
    ) -> None:
        # Callers may pass the repository directly (new runtime) or a storage bundle.
        self._requests = getattr(repository, "trade_requests", repository)
        self.request_id = request_id
        self._report = report
        self._loop = loop
        self._poll_seconds = max(0.5, poll_seconds)
        self._task: Any | None = None
        self._stop = asyncio.Event()
        self.reported = False

    def start(self) -> None:
        """Start a local poll without blocking the Discord event loop."""
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                log.warning("cannot watch request %s: no event loop", self.request_id)
                return
        self._task = asyncio.run_coroutine_threadsafe(self._poll(), self._loop)

    async def _poll(self) -> None:
        while not self._stop.is_set() and not self.reported:
            try:
                request = await asyncio.to_thread(self._requests.get, self.request_id)
                if request is not None and request.status in REPORTABLE:
                    self.reported = True
                    await self._report(request)
                    self._stop.set()
                    return
            except Exception:  # noqa: BLE001
                log.exception("could not read local request %s", self.request_id)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._task = None
