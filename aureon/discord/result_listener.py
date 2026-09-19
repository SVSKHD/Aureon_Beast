"""Reporting a request's outcome back to Discord (§8, §37).

A confirmation is not an answer. The human pressed CONFIRM; what they need next is what
the broker did -- and that is the executor's account, arriving in Firestore, not something
the button could have told them.

So this listens to one request document and reports when it reaches a terminal state.
**Never silent:** a failure is reported as loudly as a fill, with the reason the executor
recorded. A human who authorised real money and heard nothing back has been failed worse
than one who was told it was rejected.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aureon.models.enums import TradeRequestStatus
from aureon.models.trade import TradeRequest
from aureon.storage import paths

log = logging.getLogger(__name__)

#: Statuses worth reporting. Every one of these is an answer the human is owed.
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
    """Watches one trade request and reports its outcome once."""

    def __init__(
        self,
        client: Any,
        request_id: str,
        report: Callable[[TradeRequest], Awaitable[None]],
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._client = client
        self.request_id = request_id
        self._report = report
        self._loop = loop
        self._watch: Any | None = None
        self.reported = False

    def start(self) -> None:
        """Begin watching. Failures here are logged, never raised.

        A listener that cannot start is a missing notification, not a reason to fail the
        trade that was already confirmed.
        """
        try:
            ref = self._client.document(paths.trade_request_path(self.request_id))
            self._watch = ref.on_snapshot(self._on_snapshot)
        except Exception:  # noqa: BLE001
            log.exception("could not watch request %s", self.request_id)

    def _on_snapshot(self, docs: Any, changes: Any, read_time: Any) -> None:
        """Firestore calls this on a background thread, so hop back to the loop."""
        if self.reported:
            return
        for doc in docs:
            data = doc.to_dict()
            if not data:
                continue
            try:
                request = TradeRequest.model_validate(data)
            except Exception:  # noqa: BLE001
                log.exception("unreadable request document %s", self.request_id)
                continue
            if request.status not in REPORTABLE:
                continue
            self.reported = True
            self.stop()
            self._dispatch(request)
            return

    def _dispatch(self, request: TradeRequest) -> None:
        if self._loop is None:
            log.info(
                "request %s reached %s (no loop to report on)",
                request.request_id,
                request.status.value,
            )
            return
        asyncio.run_coroutine_threadsafe(self._report(request), self._loop)

    def stop(self) -> None:
        if self._watch is not None:
            try:
                self._watch.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            self._watch = None
