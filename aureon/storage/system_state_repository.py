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
    """Reads and writes ``system_state/{symbol}_{timeframe}`` (9A).

    One document per symbol, not one for everything, and the reason only appears with a
    second symbol: the write throttle is per document. With a shared document, gold's
    candle close resets the timer and silver's state is suppressed for the next few
    seconds -- so the symbol a reader is looking at can be stale because of a symbol they
    are not.

    Each document carries the global fields (heartbeats, trading_enabled) as well, so a
    reader of one symbol needs one read. Those are derived copies of documents that exist
    in their own right (``heartbeats/{service}`` is the source of truth, decision 10, and
    ``settings/execution`` is), so duplicating them costs nothing and is never the truth
    anybody depends on.
    """

    def __init__(self, client: Any, *, min_interval_seconds: float = 5.0) -> None:
        self._client = client
        self.min_interval_seconds = min_interval_seconds
        #: (symbol, timeframe) -> last write. Per symbol, which is the whole point.
        self._last_writes: dict[tuple[str, str], datetime] = {}

    def write(
        self, state: SystemState, *, force: bool = False, now: datetime | None = None
    ) -> bool:
        """Write one document per symbol. True if ANY was written.

        ``force=True`` bypasses the throttle and is what a candle close uses: a new closed
        candle is genuinely new information and must not wait for a timer.

        A state carrying **no** symbols writes nothing and returns False. It has nothing to
        say that is not already a document of its own: its only other fields are derived
        copies of the heartbeats and settings documents.
        """
        moment = to_utc(now or utc_now())
        written = False
        for symbol_state in state.symbols:
            key = (symbol_state.symbol, symbol_state.timeframe.value)
            last = self._last_writes.get(key)
            if (
                not force
                and last is not None
                and (moment - last).total_seconds() < self.min_interval_seconds
            ):
                continue
            # The document holds exactly its own symbol. A reader of XAGUSD_M5 must not
            # find XAUUSD's panel inside it and have to work out which one is current.
            payload = state.model_copy(
                update={"updated_at": moment, "symbols": (symbol_state,)}
            ).model_dump(mode="json")
            self._client.document(
                paths.system_state_path(symbol_state.symbol, symbol_state.timeframe)
            ).set(payload)
            self._last_writes[key] = moment
            written = True
        return written

    def read_symbol(self, symbol: str, timeframe: Any) -> SystemState | None:
        """One symbol's document, in one read. What ``/status symbol:X`` uses."""
        snapshot = self._client.document(
            paths.system_state_path(symbol, timeframe)
        ).get()
        if not getattr(snapshot, "exists", False):
            return None
        return SystemState.model_validate(snapshot.to_dict())

    def read(self) -> SystemState | None:
        """Every symbol, merged into one ``SystemState``.

        Kept so callers that want "whatever the observer knows" -- the quote lookups on
        Discord's confirmation screens -- are unchanged by the split. ``updated_at`` is the
        NEWEST of the documents read, and the global fields come from that same newest one:
        a merged freshness that took the oldest would make a quiet symbol look like a dead
        observer.
        """
        found: list[SystemState] = []
        for doc in self._client.collection(paths.SYSTEM_STATE).stream():
            if getattr(doc, "id", None) == paths.LEGACY_SYSTEM_STATE_DOC:
                # A pre-9A whole-system document. Merging it would report every symbol
                # twice, once live and once frozen at the split.
                continue
            data = doc.to_dict() or {}
            if not data:
                continue
            found.append(SystemState.model_validate(data))
        if not found:
            return None
        newest = max(found, key=lambda s: s.updated_at)
        symbols = tuple(
            sorted(
                (s for state in found for s in state.symbols),
                key=lambda s: (s.symbol, s.timeframe.value),
            )
        )
        return newest.model_copy(update={"symbols": symbols})

    @property
    def last_write(self) -> datetime | None:
        """The most recent write across every symbol."""
        return max(self._last_writes.values()) if self._last_writes else None


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
