"""Exactly-once executor claim against Aureon's local SQLite backend."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from aureon.models.enums import TradeRequestStatus
from aureon.models.market import QuoteSnapshot
from aureon.storage.local_database import LocalDatabase
from aureon.storage.postgres.repositories.trade_requests import (
    ClaimRejected,
    TradeRequestRepository,
)
from tests.postgres.factories import CLOSE, a_trade_request


def test_two_sqlite_executors_racing_one_confirmation_produce_one_claim(tmp_path) -> None:
    database = LocalDatabase(tmp_path / "aureon.db")
    database.ensure_schema()
    seed = TradeRequestRepository(database)
    seed.create(a_trade_request("race-1"))
    seed.confirm(
        "race-1",
        "trader",
        QuoteSnapshot(
            symbol="XAUUSD",
            bid=2403.4,
            ask=2403.6,
            captured_at=CLOSE,
        ),
        confirmation_ttl_seconds=600,
        now=CLOSE,
    )

    barrier = Barrier(2)

    def claim(executor_id: str):
        repository = TradeRequestRepository(database)
        barrier.wait(timeout=5)
        try:
            return repository.claim(
                "race-1",
                executor_id,
                lease_seconds=60,
                now=CLOSE + timedelta(seconds=1),
            )
        except ClaimRejected as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("executor-a", "executor-b")))

    claimed = [result for result in results if not isinstance(result, ClaimRejected)]
    refused = [result for result in results if isinstance(result, ClaimRejected)]

    assert len(claimed) == 1
    assert len(refused) == 1

    stored = seed.get("race-1")
    assert stored is not None
    assert stored.status is TradeRequestStatus.EXECUTING
    assert stored.executor_instance_id in {"executor-a", "executor-b"}

    audit = seed.audit.for_document("trade_requests", "race-1")
    claims = [row for row in audit if row.action == "trade_request.claim"]
    assert len(claims) == 1
    database.dispose()
