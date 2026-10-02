from __future__ import annotations

import threading
import time

from aureon.engine.market_engine import MarketEngine
from aureon.models.enums import Timeframe


class _Provider:
    def now_utc(self):  # pragma: no cover - overridden stream never calls provider
        raise AssertionError("not used")


class _Engine:
    pass


class _IsolationProbe(MarketEngine):
    def __init__(self) -> None:
        super().__init__(
            _Provider(),
            _Engine(),
            symbols=("XAUUSD", "XAGUSD"),
            timeframes=(Timeframe.M5,),
            parallel_streams=True,
        )
        self.gold_started = threading.Event()
        self.silver_started = threading.Event()

    def _poll_stream(self, symbol: str, timeframe: Timeframe):
        if symbol == "XAUUSD":
            self.gold_started.set()
            assert self.silver_started.wait(1.0), "Silver was blocked behind Gold"
            time.sleep(0.01)
        else:
            self.silver_started.set()
            assert self.gold_started.wait(1.0), "Gold was blocked behind Silver"
        return []


def test_parallel_streams_start_gold_and_silver_independently() -> None:
    engine = _IsolationProbe()
    assert engine.poll_once() == []
    assert engine.gold_started.is_set()
    assert engine.silver_started.is_set()


class _FailureProbe(MarketEngine):
    def __init__(self) -> None:
        super().__init__(
            _Provider(),
            _Engine(),
            symbols=("XAUUSD", "XAGUSD"),
            timeframes=(Timeframe.M5,),
            parallel_streams=True,
        )
        self.silver_completed = threading.Event()

    def _poll_stream(self, symbol: str, timeframe: Timeframe):
        if symbol == "XAUUSD":
            raise RuntimeError("gold analysis failure")
        self.silver_completed.set()
        return []


def test_one_symbol_failure_does_not_cancel_sibling_stream() -> None:
    engine = _FailureProbe()
    assert engine.poll_once() == []
    assert engine.silver_completed.is_set()
