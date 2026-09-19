"""System state and heartbeat persistence (§59, §67).

Both writers are **throttled**, and for a concrete reason: the observer sees a tick
stream, and writing system state on every tick would cost real money in Firestore
writes while telling a reader nothing a five-second-old document did not already
say. A candle close is always written immediately, because that IS new information.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.system import Heartbeat, SystemState
from aureon.storage import paths


class SystemStateRepository:
    """Reads and writes the single ``system_state/current`` document."""

    def __init__(self, client: Any, *, min_interval_seconds: float = 5.0) -> None:
        self._client = client
        self.min_interval_seconds = min_interval_seconds
        self._last_write: datetime | None = None

    def write(
        self, state: SystemState, *, force: bool = False, now: datetime | None = None
    ) -> bool:
        """Write state, honouring the throttle. Returns True if it was written.

        ``force=True`` bypasses the throttle and is what a candle close uses: a new
        closed candle is genuinely new information and must not wait for a timer.
        """
        moment = to_utc(now or utc_now())
        if not force and self._last_write is not None:
            if (moment - self._last_write).total_seconds() < self.min_interval_seconds:
                return False

        payload = state.model_copy(update={"updated_at": moment}).model_dump(mode="json")
        self._client.document(paths.system_state_path()).set(payload)
        self._last_write = moment
        return True

    def read(self) -> SystemState | None:
        snapshot = self._client.document(paths.system_state_path()).get()
        if not getattr(snapshot, "exists", False):
            return None
        return SystemState.model_validate(snapshot.to_dict())

    @property
    def last_write(self) -> datetime | None:
        return self._last_write


class HeartbeatRepository:
    """Writes ``heartbeats/{service}`` -- the source of truth for liveness.

    Decision 10: this document is authoritative; the copy embedded in
    ``system_state`` is derived for one-read status views and is never written
    independently.
    """

    def __init__(self, client: Any, *, min_interval_seconds: float = 5.0) -> None:
        self._client = client
        self.min_interval_seconds = min_interval_seconds
        self._last: dict[str, datetime] = {}

    def beat(
        self,
        service: str,
        *,
        instance_id: str | None = None,
        detail: dict[str, Any] | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> bool:
        """Record a heartbeat, throttled. Returns True if written."""
        moment = to_utc(now or utc_now())
        previous = self._last.get(service)
        if not force and previous is not None:
            if (moment - previous).total_seconds() < self.min_interval_seconds:
                return False

        heartbeat = Heartbeat(
            service=service,
            updated_at=moment,
            instance_id=instance_id,
            detail=detail or {},
        )
        self._client.document(paths.heartbeat_path(service)).set(
            heartbeat.model_dump(mode="json")
        )
        self._last[service] = moment
        return True

    def read(self, service: str) -> Heartbeat | None:
        snapshot = self._client.document(paths.heartbeat_path(service)).get()
        if not getattr(snapshot, "exists", False):
            return None
        return Heartbeat.model_validate(snapshot.to_dict())

    def read_all(self) -> dict[str, Heartbeat]:
        """Every service's heartbeat, for ``/status`` and the dashboard."""
        out: dict[str, Heartbeat] = {}
        for service in paths.SERVICES:
            beat = self.read(service)
            if beat is not None:
                out[service] = beat
        return out
