"""Replay: walk history through the live analysis engine (§79).

Adds no analysis. Its only job is to pull candles from a file-backed provider and
hand them to ``AnalysisEngine`` in chronological order -- the same object, in the
same order, that the live path uses. If this file contained any decision logic, the
parity test would stop proving anything.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

from aureon.data.base_provider import BaseMarketDataProvider
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.models.detection import Detection
from aureon.models.enums import Timeframe
from aureon.models.market import Candle


class ReplayEngine:
    """Feeds historical candles through an ``AnalysisEngine``."""

    def __init__(
        self,
        provider: BaseMarketDataProvider,
        engine: AnalysisEngine,
        *,
        symbol: str,
        timeframe: Timeframe,
    ) -> None:
        self.provider = provider
        self.engine = engine
        self.symbol = symbol
        self.timeframe = timeframe

    def candles(
        self, from_utc: datetime | None = None, to_utc: datetime | None = None
    ) -> list[Candle]:
        """Every closed candle in range, ascending."""
        start = from_utc or datetime(1970, 1, 1, tzinfo=UTC)
        end = to_utc or (self.provider.now_utc() + timedelta(days=1))
        return self.provider.get_closed_candles(self.symbol, self.timeframe, start, end)

    def run(
        self,
        *,
        from_utc: datetime | None = None,
        to_utc: datetime | None = None,
        on_detection: Callable[[Detection], None] | None = None,
    ) -> list[Detection]:
        """Replay a range, returning every detection in order.

        ``on_detection`` receives each detection as it is produced, so a caller can
        enqueue to the outbox during a backfill instead of buffering the whole run.
        """
        detections: list[Detection] = []
        for candle in self.candles(from_utc, to_utc):
            for detection in self.engine.on_closed_candle(candle):
                detections.append(detection)
                if on_detection is not None:
                    on_detection(detection)
        return detections

    def iter_detections(
        self, *, from_utc: datetime | None = None, to_utc: datetime | None = None
    ) -> Iterator[Detection]:
        """Streaming variant, for backfills too large to hold in memory."""
        for candle in self.candles(from_utc, to_utc):
            yield from self.engine.on_closed_candle(candle)
