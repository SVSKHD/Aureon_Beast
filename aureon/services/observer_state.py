"""The observer's durable cursor (§75).

Records the last candle the observer fully processed, per stream, so a restart
neither reprocesses old candles nor skips the ones that closed while it was down.

Written with an **atomic replace**: the new content goes to a temporary file in the
same directory and is then renamed over the old one. A plain in-place write that is
interrupted leaves a truncated or half-written JSON file, and an observer that
cannot parse its own cursor on boot would fall back to a lookback window -- quietly
re-deriving detections and, worse, potentially missing the gap it was supposed to
fill.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from aureon.models.base import to_utc
from aureon.models.enums import Timeframe

log = logging.getLogger(__name__)


class ObserverState:
    """A small JSON file mapping ``symbol|timeframe`` to the last candle open."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cursors: dict[str, str] = {}
        self.load()

    @staticmethod
    def _key(symbol: str, timeframe: Timeframe) -> str:
        return f"{symbol}|{timeframe.value}"

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            self._cursors = {}
            return self._cursors
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._cursors = dict(data.get("cursors", {}))
        except (json.JSONDecodeError, OSError):
            # A corrupt cursor file is recoverable: the observer falls back to its
            # lookback window. Losing the file is far better than trusting a
            # half-written value that would silently skip candles.
            log.exception("observer state at %s is unreadable; starting without cursors", self.path)
            self._cursors = {}
        return self._cursors

    def get(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        raw = self._cursors.get(self._key(symbol, timeframe))
        return datetime.fromisoformat(raw) if raw else None

    def set(self, symbol: str, timeframe: Timeframe, last_open: datetime) -> None:
        """Advance a cursor. Never moves it backwards.

        Monotonic on purpose: a late-arriving candle from a provider glitch must not
        rewind the cursor and cause the range in between to be re-processed.
        """
        moment = to_utc(last_open)
        current = self.get(symbol, timeframe)
        if current is not None and moment <= current:
            return
        self._cursors[self._key(symbol, timeframe)] = moment.isoformat()

    def save(self) -> None:
        """Persist atomically."""
        payload = json.dumps({"cursors": self._cursors}, indent=2, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", dir=str(self.path.parent), delete=False, encoding="utf-8", suffix=".tmp"
        )
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())  # the data must be on disk before the rename
            handle.close()
            os.replace(handle.name, self.path)
        except BaseException:
            handle.close()
            Path(handle.name).unlink(missing_ok=True)
            raise

    def set_and_save(self, symbol: str, timeframe: Timeframe, last_open: datetime) -> None:
        self.set(symbol, timeframe, last_open)
        self.save()

    @property
    def cursors(self) -> dict[str, str]:
        return dict(self._cursors)
