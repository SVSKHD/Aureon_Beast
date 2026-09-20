"""Shared test fixtures for Phase 2.

The important one is ``FakeLiveProvider``. The parity test (§82) has to compare the
replay path against the *live* path, so it needs something that behaves like a live
terminal -- candles appearing one at a time as a clock advances, the forming bar
withheld -- while serving byte-identical data to the file-backed provider. Anything
less and a parity failure could not be distinguished from a data difference.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aureon.data.base_provider import BaseMarketDataProvider
from aureon.data.historical_provider import DEFAULT_SYMBOL_INFO, HistoricalDataProvider
from aureon.models.base import to_utc
from aureon.models.enums import Timeframe
from aureon.models.market import Candle, QuoteSnapshot, SymbolInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CSV = REPO_ROOT / "aureon" / "data" / "fixtures" / "XAUUSD_M5.csv"

MARKET_TZ = "Europe/Athens"
ACCOUNT_SCOPE = "primary"

# The shipped pair (AUREON_EMA_FAST / AUREON_EMA_SLOW defaults). EmaCrossAgent takes no
# default periods on purpose, so tests name them here once rather than each inventing a
# pair -- which is how a suite ends up proving something about 9/21 while the observer
# runs 20/50.
EMA_FAST = 20
EMA_SLOW = 50


def cross_agent(**overrides: object):
    """An ``EmaCrossAgent`` on the shipped periods unless a test says otherwise."""
    from aureon.agents.ema_cross_agent import EmaCrossAgent

    params: dict[str, object] = {"fast_period": EMA_FAST, "slow_period": EMA_SLOW}
    params.update(overrides)
    return EmaCrossAgent(**params)  # type: ignore[arg-type]


@pytest.fixture
def fixture_path() -> Path:
    assert FIXTURE_CSV.exists(), (
        f"{FIXTURE_CSV} is missing; regenerate with `python scripts/gen_fixtures.py`"
    )
    return FIXTURE_CSV


@pytest.fixture
def historical(fixture_path: Path) -> HistoricalDataProvider:
    return HistoricalDataProvider(fixture_path, market_tz=MARKET_TZ)


@pytest.fixture
def candles(historical: HistoricalDataProvider) -> list[Candle]:
    return historical.candles


class FakeLiveProvider(BaseMarketDataProvider):
    """A live-shaped provider over a fixed candle list and a movable clock.

    Serves only candles that have CLOSED as of ``now_utc()``, exactly as a real
    terminal does, so the forming-bar rule is exercised rather than assumed.
    """

    def __init__(
        self,
        candles: list[Candle],
        *,
        symbol: str = "XAUUSD",
        timeframe: Timeframe = Timeframe.M5,
        symbol_info: SymbolInfo | None = None,
    ) -> None:
        self._all = sorted(candles, key=lambda c: c.open_time.utc)
        self.symbol = symbol
        self.timeframe = timeframe
        self._info = symbol_info or DEFAULT_SYMBOL_INFO
        # Start before the first candle, so nothing is visible until time advances.
        self._clock = self._all[0].open_time.utc if self._all else datetime.now().astimezone()
        self.calls = 0
        self.fail_next = 0

    # ── Clock control ─────────────────────────────────────────────────────────

    def set_clock(self, moment: datetime) -> None:
        self._clock = to_utc(moment)

    def advance_to_close_of(self, candle: Candle, *, grace_seconds: float = 3.0) -> None:
        """Move the clock just past a candle's close, as a live poll would see it."""
        self._clock = candle.close_time + timedelta(seconds=grace_seconds)

    def now_utc(self) -> datetime:
        return self._clock

    # ── Data ──────────────────────────────────────────────────────────────────

    def get_closed_candles(
        self, symbol: str, timeframe: Timeframe, from_utc: datetime, to_utc_: datetime
    ) -> list[Candle]:
        self.calls += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("simulated provider failure")
        start, end = to_utc(from_utc), to_utc(to_utc_)
        return [
            c
            for c in self._all
            if start <= c.open_time.utc < end and c.close_time <= self._clock
        ]

    def get_quote(self, symbol: str) -> QuoteSnapshot:
        visible = [c for c in self._all if c.close_time <= self._clock]
        price = visible[-1].close if visible else self._all[0].open
        return QuoteSnapshot(
            symbol=symbol, bid=price - 0.15, ask=price + 0.15, captured_at=self._clock, point=0.01
        )

    def symbol_info(self, symbol: str) -> SymbolInfo:
        return self._info

    def last_tick_time(self, symbol: str) -> datetime:
        return self._clock


@pytest.fixture
def fake_live(candles: list[Candle]) -> FakeLiveProvider:
    return FakeLiveProvider(candles)


class InMemoryFirestore:
    """A tiny Firestore double supporting the repositories' actual usage.

    Only ``document(path).set()/get()`` and enough of a collection query to exercise
    the repositories. Deliberately minimal: a fuller fake would start to encode
    assumptions about the SDK that only the emulator can really validate (Phase 4).
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.writes = 0
        self.fail_next = 0

    def document(self, path: str) -> _FakeDoc:
        return _FakeDoc(self, path)

    def collection(self, path: str) -> _FakeCollection:
        return _FakeCollection(self, path)

    def transaction(self) -> _FakeTransaction:
        """A transaction object with NO isolation and NO retry.

        Enough to exercise a repository's transactional code path in a unit test, and
        nothing more. It cannot prove the property the transactions exist for -- that a
        concurrent write aborts the loser -- because a double that defines its own
        concurrency semantics would be testing my model of Firestore rather than
        Firestore. That belongs in the emulator suite, which is where it is.
        """
        return _FakeTransaction(self)


class _FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None


class _FakeDoc:
    def __init__(self, store: InMemoryFirestore, path: str) -> None:
        self._store = store
        self._path = path

    def set(self, payload: dict[str, Any]) -> None:
        if self._store.fail_next > 0:
            self._store.fail_next -= 1
            raise ConnectionError("simulated firestore failure")
        self._store.docs[self._path] = dict(payload)
        self._store.writes += 1

    def get(self, transaction: Any | None = None) -> _FakeSnapshot:
        # `transaction` accepted and ignored: reads inside a transaction see the same
        # store, which is exactly the no-isolation caveat above.
        return _FakeSnapshot(self._store.docs.get(self._path))

    def delete(self) -> None:
        self._store.docs.pop(self._path, None)


class _FakeTransaction:
    """Applies writes immediately. See ``InMemoryFirestore.transaction``."""

    def __init__(self, store: InMemoryFirestore) -> None:
        self._store = store

    def set(self, ref: _FakeDoc, payload: dict[str, Any]) -> None:
        ref.set(payload)

    def delete(self, ref: _FakeDoc) -> None:
        ref.delete()


class _FakeCollection:
    def __init__(self, store: InMemoryFirestore, path: str) -> None:
        self._store = store
        self._path = path
        self._filters: list[tuple[str, str, Any]] = []
        self._limit: int | None = None

    def where(self, field: str, op: str, value: Any) -> _FakeCollection:
        self._filters.append((field, op, value))
        return self

    def order_by(self, field: str, direction: str = "ASCENDING") -> _FakeCollection:
        self._order = (field, direction)
        return self

    def limit(self, count: int) -> _FakeCollection:
        self._limit = count
        return self

    def stream(self) -> list[_FakeSnapshot]:
        rows = [
            data
            for path, data in self._store.docs.items()
            if path.startswith(f"{self._path}/")
        ]
        for field, op, value in self._filters:
            if op == "==":
                rows = [r for r in rows if r.get(field) == value]
        if self._limit is not None:
            rows = rows[: self._limit]
        return [_FakeSnapshot(r) for r in rows]


@pytest.fixture
def firestore() -> InMemoryFirestore:
    return InMemoryFirestore()
