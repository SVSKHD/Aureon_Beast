"""Periodic liveness beats (§67).

A service that stops beating is reported STALE and then OFFLINE by
``freshness_of``, which is computed at read time rather than stored -- so a crashed
process looks crashed instead of leaving behind a document that still claims to be
healthy.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from typing import Any

from aureon.storage.system_state_repository import HeartbeatRepository

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 15.0


class HeartbeatService:
    """Beats one service's heartbeat on a timer, in a background thread."""

    def __init__(
        self,
        repository: HeartbeatRepository,
        service: str,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        instance_id: str | None = None,
        detail_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.repository = repository
        self.service = service
        self.interval_seconds = interval_seconds
        # A stable per-process id, so two instances of the same service are
        # distinguishable in the audit trail and in /status.
        self.instance_id = instance_id or uuid.uuid4().hex[:12]
        self.detail_provider = detail_provider

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.beats = 0

    def beat_once(self, *, force: bool = True) -> bool:
        """Write one heartbeat. Failures are logged, never raised.

        A Firestore hiccup must not take down the process it is reporting on -- the
        heartbeat simply goes stale, which is the correct signal anyway.
        """
        detail: dict[str, Any] = {}
        if self.detail_provider is not None:
            try:
                detail = self.detail_provider()
            except Exception:  # noqa: BLE001 - detail is diagnostic, never critical
                log.exception("heartbeat detail_provider failed for %s", self.service)
        try:
            written = self.repository.beat(
                self.service, instance_id=self.instance_id, detail=detail, force=force
            )
        except Exception:  # noqa: BLE001
            log.exception("heartbeat write failed for %s", self.service)
            return False
        if written:
            self.beats += 1
        return written

    def run(self) -> None:
        while not self._stop.is_set():
            self.beat_once()
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("heartbeat service already started")
        self.beat_once()  # beat immediately, so startup is visible at once
        self._thread = threading.Thread(
            target=self.run, name=f"heartbeat-{self.service}", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def __enter__(self) -> HeartbeatService:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
