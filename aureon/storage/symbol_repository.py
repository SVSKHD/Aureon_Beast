"""Published symbol metadata (§39, §42).

Solves a boundary problem. Discord must build the execution-mode selector from the
symbol's supported filling modes, and must validate a lot against the broker's
min/max/step *before* showing a confirmation screen (§37, §39, §42) -- but Discord may
never call the broker (CLAUDE.md).

So a process that legitimately holds a data provider -- the observer -- publishes
``SymbolInfo`` to ``symbol_specs/{symbol}``, and Discord reads it from Firestore like
everything else (decision 79).

The copy is a **convenience, not an authority**. It can be minutes stale, a broker can
change a symbol's volume step, and Discord's validation is therefore advisory: the
execution guard re-reads the live symbol at execution time and refuses anything the
broker would reject anyway (§56). Discord's job here is to give a human a good answer
early rather than an opaque rejection after they confirmed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.market import SymbolInfo
from aureon.storage import paths

log = logging.getLogger(__name__)


class SymbolRepository:
    """Reads and publishes ``symbol_specs`` documents."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def publish(self, info: SymbolInfo, *, now: datetime | None = None) -> str:
        """Publish a symbol's metadata. Idempotent on the symbol name."""
        payload = info.model_dump(mode="json")
        payload["published_at"] = to_utc(now or utc_now()).isoformat()
        path = paths.symbol_spec_path(info.symbol)
        self._client.document(path).set(payload)
        return path

    def get(self, symbol: str) -> SymbolInfo | None:
        snapshot = self._client.document(paths.symbol_spec_path(symbol)).get()
        if not getattr(snapshot, "exists", False):
            return None
        data = dict(snapshot.to_dict() or {})
        # published_at is metadata about the copy, not part of the contract.
        data.pop("published_at", None)
        return SymbolInfo.model_validate(data)

    def published_at(self, symbol: str) -> datetime | None:
        """When this copy was written, so a reader can judge how stale it is."""
        snapshot = self._client.document(paths.symbol_spec_path(symbol)).get()
        if not getattr(snapshot, "exists", False):
            return None
        raw = (snapshot.to_dict() or {}).get("published_at")
        return datetime.fromisoformat(raw) if raw else None
