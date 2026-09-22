"""Shared test fixtures for Phase 2.

The important one is ``FakeLiveProvider``. The parity test (§82) has to compare the
replay path against the *live* path, so it needs something that behaves like a live
terminal -- candles appearing one at a time as a clock advances, the forming bar
withheld -- while serving byte-identical data to the file-backed provider. Anything
less and a parity failure could not be distinguished from a data difference.
"""

from __future__ import annotations

import os

# BEFORE any aureon import: aureon.storage.paths reads AUREON_COLLECTION_PREFIX at
# import time and freezes it, so setting this later would have no effect at all while
# looking as though it had. A suite writing to the production prefix would, the one time
# someone runs it against a real project, overwrite live documents and report a clean
# pass.
os.environ.setdefault("AUREON_COLLECTION_PREFIX", "aureon_test")

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aureon.data.base_provider import BaseMarketDataProvider
from aureon.data.historical_provider import DEFAULT_SYMBOL_INFO, HistoricalDataProvider
from aureon.models.base import to_utc
from aureon.models.enums import Timeframe
from aureon.models.market import Candle, QuoteSnapshot, SymbolInfo
from aureon.storage.paths import DEFAULT_COLLECTION_PREFIX, PREFIX

if PREFIX == DEFAULT_COLLECTION_PREFIX and not os.environ.get("FIRESTORE_EMULATOR_HOST"):
    raise RuntimeError(
        f"AUREON_COLLECTION_PREFIX resolved to {PREFIX!r}, the production default, and "
        "no FIRESTORE_EMULATOR_HOST is set. Refusing to run the suite."
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CSV = REPO_ROOT / "aureon" / "data" / "fixtures" / "XAUUSD_M5.csv"
SILVER_FIXTURE_CSV = REPO_ROOT / "aureon" / "data" / "fixtures" / "XAGUSD_M5.csv"

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


@pytest.fixture
def silver_candles() -> list[Candle]:
    """The XAGUSD week: same trading hours and session shape, its own random walk."""
    assert SILVER_FIXTURE_CSV.exists(), (
        f"{SILVER_FIXTURE_CSV} is missing; regenerate with "
        "`python scripts/gen_fixtures.py`"
    )
    return HistoricalDataProvider(
        SILVER_FIXTURE_CSV, market_tz=MARKET_TZ, symbol="XAGUSD"
    ).candles


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
        #: Quote reads, so a test can see that the observer did NOT read one (9C).
        self.quote_calls = 0
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
            # Filtered by SYMBOL as a real terminal does. Without it, polling XAGUSD
            # returns gold's candles and the observer routes them straight back to gold's
            # engine -- every multi-symbol test would then pass while proving nothing.
            if c.symbol == symbol
            and c.timeframe is timeframe
            and start <= c.open_time.utc < end
            and c.close_time <= self._clock
        ]

    def get_quote(self, symbol: str) -> QuoteSnapshot:
        """This symbol's own last price, at this symbol's own tick.

        Counted in ``quote_calls`` (9C): "the observer did NOT read a quote for a symbol
        nobody has an alert on" is otherwise an invisible property, and a test that cannot
        see it would pass whatever the code did.

        A quote built from another symbol's candles is the same class of error as a
        candle served under the wrong symbol, and harder to notice: the number looks
        like a price.
        """
        self.quote_calls += 1
        mine = [c for c in self._all if c.symbol == symbol] or self._all
        visible = [c for c in mine if c.close_time <= self._clock]
        price = visible[-1].close if visible else mine[0].open
        point = self.symbol_info(symbol).point
        spread = point * 15
        return QuoteSnapshot(
            symbol=symbol,
            bid=round(price - spread, 5),
            ask=round(price + spread, 5),
            captured_at=self._clock,
            point=point,
        )

    def symbol_info(self, symbol: str) -> SymbolInfo:
        """The requested symbol's own metadata, including its tick.

        Silver's point is 0.001 and gold's is 0.01; returning one for the other is how a
        lot validation or a threshold conversion comes out ten times wrong.
        """
        from aureon.config.symbol_tuning import tuning_for

        if symbol == self._info.symbol:
            return self._info
        point = tuning_for(symbol).point
        digits = max(0, round(-math.log10(point)))
        return self._info.model_copy(
            update={"symbol": symbol, "point": point, "digits": digits}
        )

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


class FakeAlreadyExists(Exception):
    """What the double raises for a ``create`` over an existing document (9C).

    Named for the Firestore error it stands in for, because the repository matches on the
    exception's NAME rather than importing ``google.api_core`` -- which would make a
    Firestore package a hard dependency of the module these doubles exercise.
    """


class _FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None, doc_id: str | None = None) -> None:
        self._data = data
        self.exists = data is not None
        # Real snapshots carry their document id, and code reads it: the detection
        # repository logs it for an unreadable document, and the state repository uses it
        # to skip the pre-9A whole-system document. Without it here, both paths are
        # exercised against something the real client does not look like.
        self.id = doc_id

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

    def create(self, payload: dict[str, Any]) -> None:
        """Write only if the document does not exist, as the real client does.

        Needed by the notification repository, whose whole exactly-once mechanism is a
        create that fails on an existing document (9C). A double whose ``create`` behaved
        like ``set`` would make the dedup tests pass while proving nothing.
        """
        if self._path in self._store.docs:
            raise FakeAlreadyExists(self._path)
        self.set(payload)

    def get(
        self, field_paths: Any | None = None, transaction: Any | None = None
    ) -> _FakeSnapshot:
        # The real signature is get(field_paths=None, transaction=None, ...), and getting it
        # wrong here would hide a caller that passed the transaction POSITIONALLY -- which the
        # real client reads as a field-path mask and rejects with "'Transaction' object is not
        # iterable". The setup repository did exactly that, and only the emulator caught it.
        #
        # `transaction` is accepted and ignored: reads inside a transaction see the same store,
        # which is exactly the no-isolation caveat above.
        return _FakeSnapshot(
            self._store.docs.get(self._path), self._path.rsplit("/", 1)[-1]
        )

    def delete(self) -> None:
        self._store.docs.pop(self._path, None)


class _FakeTransaction:
    """Applies writes immediately. See ``InMemoryFirestore.transaction``."""

    def __init__(self, store: InMemoryFirestore) -> None:
        self._store = store

    def set(self, ref: _FakeDoc, payload: dict[str, Any]) -> None:
        ref.set(payload)

    def create(self, ref: _FakeDoc, payload: dict[str, Any]) -> None:
        """Create-if-absent inside a transaction, raising as the real client does.

        The setup repository's event write uses it (12, T-6): the deterministic event id plus a
        create is what makes a re-processed candle a no-op instead of a duplicate row.
        """
        ref.create(payload)

    def delete(self, ref: _FakeDoc) -> None:
        ref.delete()


def _field(doc: dict[str, Any], field: str) -> Any:
    """Read a possibly-dotted field path, as a Firestore query does.

    ``where("detected_at.utc", ">=", ...)`` addresses a value nested inside a map. A
    double that only did ``doc.get("detected_at.utc")`` would find nothing, drop the
    filter's effect on the floor, and pass every test of a windowed read while the
    window did nothing.
    """
    value: Any = doc
    for part in field.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a is not None and a > b,
    ">=": lambda a, b: a is not None and a >= b,
    "<": lambda a, b: a is not None and a < b,
    "<=": lambda a, b: a is not None and a <= b,
    "in": lambda a, b: a in b,
    "not-in": lambda a, b: a not in b,
}


class _FakeCollection:
    def __init__(self, store: InMemoryFirestore, path: str) -> None:
        self._store = store
        self._path = path
        self._filters: list[tuple[str, str, Any]] = []
        self._order: tuple[str, str] | None = None
        self._limit: int | None = None

    def where(self, field: str, op: str, value: Any) -> _FakeCollection:
        if op not in _OPS:
            # Louder than returning everything: an unimplemented operator that silently
            # matched every document is how a filter stops filtering without a red test.
            raise NotImplementedError(f"InMemoryFirestore: unsupported operator {op!r}")
        self._filters.append((field, op, value))
        return self

    def order_by(self, field: str, direction: str = "ASCENDING") -> _FakeCollection:
        self._order = (field, direction)
        return self

    def limit(self, count: int) -> _FakeCollection:
        self._limit = count
        return self

    def stream(self) -> list[_FakeSnapshot]:
        # DIRECT children only. A prefix match alone would also return the documents of every
        # sub-collection -- so a query over `setups` would hand back its `events` rows as if
        # they were setups (12, T-6). Real Firestore does not do that, and a double that did
        # would make every test of a collection read pass while the read returned the wrong
        # documents.
        rows = [
            (path.rsplit("/", 1)[-1], data)
            for path, data in self._store.docs.items()
            if path.startswith(f"{self._path}/")
            and "/" not in path[len(self._path) + 1 :]
        ]
        for field, op, value in self._filters:
            test = _OPS[op]
            rows = [(i, r) for i, r in rows if test(_field(r, field), value)]
        if self._order is not None:
            field, direction = self._order
            # Ordered BEFORE the limit, as the server does: limiting first and sorting
            # after returns the oldest N in a "newest first, limit N" query -- the exact
            # opposite of what the caller asked for, and it looks right in a fixture whose
            # documents happen to be written in order.
            rows.sort(
                key=lambda row: (_field(row[1], field) is None, _field(row[1], field)),
                reverse=direction.upper().startswith("DESC"),
            )
        if self._limit is not None:
            rows = rows[: self._limit]
        return [_FakeSnapshot(data, doc_id) for doc_id, data in rows]


@pytest.fixture
def firestore() -> InMemoryFirestore:
    return InMemoryFirestore()
