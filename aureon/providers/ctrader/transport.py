"""Transport boundary for cTrader Open API.

The concrete network client is deliberately separate from the provider so credentials,
OAuth/token refresh and protobuf/websocket concerns never leak into Aureon's agents.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from aureon.models.enums import Timeframe


class CTraderTransport(ABC):
    """Minimal read-only contract required by CTraderDataProvider."""

    @abstractmethod
    def connect(self) -> None:
        """Authenticate/connect to the configured cTrader account."""

    @abstractmethod
    def close(self) -> None:
        """Release the cTrader connection."""

    @abstractmethod
    def candles(
        self, symbol: str, timeframe: Timeframe, from_utc: datetime, to_utc: datetime
    ) -> list[dict[str, Any]]:
        """Return cTrader trend bars ordered or unordered within the requested interval."""

    @abstractmethod
    def quote(self, symbol: str) -> dict[str, Any]:
        """Return current bid/ask and source timestamp."""

    @abstractmethod
    def symbol(self, symbol: str) -> dict[str, Any]:
        """Return symbol metadata required by Aureon's SymbolInfo contract."""
