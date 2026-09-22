"""Read-only access to setups and to the day-frame cache, for Discord (12, T-11).

Exists to resolve the same conflict ``PeriodReader`` resolves, not for tidiness:

* Firestore access must go through a repository (CLAUDE.md, decision 107);
* Discord writes only ``trade_requests``, ``settings.trading_enabled`` and ``audit_logs``
  (§71) -- and ``setups`` is the observer's, exclusively. The transaction in
  ``SetupRepository.record`` is safe precisely because one process writes.

Importing ``SetupRepository`` into ``BotContext`` would satisfy the first and break the second,
because that class can open a setup and record an event. So the composition happens HERE, inside
``aureon/storage`` where writing is allowed, and what Discord imports is this: reads, and no write
method at all.

The capability split is visible in the import graph rather than resting on nobody calling the
wrong method, and a boundary test holds it in place.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from aureon.models.enums import Timeframe
from aureon.models.market_day import MarketDayFrame
from aureon.models.setup import Setup, SetupEvent


class SetupReader:
    """Reads setups and their events. Cannot open one, and cannot record an event."""

    def __init__(self, client: Any) -> None:
        # Private, and deliberately not exposed: a caller that could reach the repository could
        # write with it, which is the whole thing this prevents.
        from aureon.storage.setup_repository import SetupRepository

        self.__setups = SetupRepository(client)

    def get(self, setup_id: str) -> Setup | None:
        return self.__setups.get(setup_id)

    def get_event(self, setup_id: str, event_id: str) -> SetupEvent | None:
        return self.__setups.get_event(setup_id, event_id)

    def events(self, setup_id: str, *, limit: int | None = None) -> list[SetupEvent]:
        return self.__setups.events(setup_id, limit=limit)

    def open_setups(
        self, *, symbol: str, market_date: str | None = None
    ) -> list[Setup]:
        return self.__setups.open_setups(symbol=symbol, market_date=market_date)

    def changed_since(self, *, symbol: str, since: datetime) -> list[Setup]:
        return self.__setups.changed_since(symbol=symbol, since=since)


class MarketDayReader:
    """Reads the day-frame cache, for a chart. Cannot write a frame or a day.

    Its own reader rather than a method on ``SetupReader``, because they are different
    collections with different owners: the observer writes both, and a reader that bundled them
    would make "what may Discord see" one question with two answers.
    """

    def __init__(self, client: Any) -> None:
        from aureon.storage.market_day_repository import MarketDayRepository

        self.__days = MarketDayRepository(client)

    def get_frame(
        self, symbol: str, market_date: str, timeframe: Timeframe
    ) -> MarketDayFrame | None:
        return self.__days.get_frame(symbol, market_date, timeframe)

    def recent_frames(
        self,
        symbol: str,
        timeframe: Timeframe,
        market_dates: Sequence[str],
        *,
        complete_only: bool = True,
    ) -> list[MarketDayFrame]:
        return self.__days.recent_frames(
            symbol, timeframe, market_dates, complete_only=complete_only
        )


__all__ = ["MarketDayReader", "SetupReader"]
