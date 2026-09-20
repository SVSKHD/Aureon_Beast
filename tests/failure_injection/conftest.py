"""Emulator-backed fixtures for the failure-injection suite (§80, §87).

These tests run against the **real Firestore emulator**, not a double. That matters:
the exactly-once guarantee rests entirely on Firestore's transaction semantics -- a
read-then-conditional-write whose transaction aborts when its read set changed. A
hand-written fake would be testing my model of those semantics rather than the
semantics, and the one thing this suite exists to prove is precisely the thing a fake
would get to define for itself.

Start it with ``make emulator`` (Node + Java, no Docker) or
``docker compose -f docker-compose.emulator.yml up -d``, then::

    make test-emulator

Without ``FIRESTORE_EMULATOR_HOST`` set, the whole suite **skips loudly** rather than
silently passing -- a green run that proved nothing would be worse than a red one.

Note on gRPC: the client honours its own proxy variables, so a session with HTTP(S)_PROXY
set must also set ``no_grpc_proxy=127.0.01,localhost``. The Makefile does this.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from aureon.execution.fake_broker import FakeBroker
from aureon.models.base import utc_now
from aureon.models.enums import OrderType
from aureon.models.market import QuoteSnapshot
from aureon.models.settings import ExecutionSettings
from aureon.models.trade import TradeRequest
from aureon.storage.trade_request_repository import TradeRequestRepository

MAGIC = 770177
SYMBOL = "XAUUSD"
USER = "user-1"

pytestmark = pytest.mark.emulator


def _emulator_host() -> str | None:
    return os.environ.get("FIRESTORE_EMULATOR_HOST")


@pytest.fixture(scope="session", autouse=True)
def require_emulator() -> None:
    if not _emulator_host():
        pytest.skip(
            "FIRESTORE_EMULATOR_HOST is not set. These tests prove exactly-once "
            "execution against real Firestore transaction semantics and cannot be "
            "meaningfully faked. Start the emulator with `make emulator`, then run "
            "`make test-emulator`.",
            allow_module_level=True,
        )


@pytest.fixture
def firestore_client():
    """A Firestore client bound to the emulator, in a per-test namespace.

    Each test gets its own project id, which gives it an isolated database. Cheaper and
    far more reliable than deleting collections between tests -- a leftover document
    from a previous test could make a later one pass for the wrong reason.
    """
    from google.cloud import firestore

    project = f"aureon-test-{uuid.uuid4().hex[:12]}"
    client = firestore.Client(project=project)
    yield client


@pytest.fixture
def repository(firestore_client) -> TradeRequestRepository:
    return TradeRequestRepository(firestore_client)


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def settings() -> ExecutionSettings:
    """Trading enabled, generous limits, so a test blocks only on what it is testing."""
    return ExecutionSettings(
        trading_enabled=True,
        max_lot=10.0,
        max_spread_points=200.0,
        max_deviation_points=100,
        max_open_positions=100,
        max_daily_trades=1000,
        confirmation_ttl_seconds=300.0,
        quote_ttl_seconds=300.0,
        executor_lease_seconds=60.0,
    )


def make_request(
    *,
    request_id: str | None = None,
    order_type: OrderType = OrderType.MARKET_BUY,
    volume: float = 0.10,
    price: float | None = None,
    bid: float = 2400.00,
    ask: float = 2400.30,
) -> TradeRequest:
    return TradeRequest(
        request_id=request_id or f"req-{uuid.uuid4().hex[:12]}",
        symbol=SYMBOL,
        order_type=order_type,
        volume=volume,
        price=price,
        requested_by=USER,
        quote=QuoteSnapshot(
            symbol=SYMBOL, bid=bid, ask=ask, point=0.01, captured_at=utc_now()
        ),
        deviation_points=20,
    )


@pytest.fixture
def confirmed(repository: TradeRequestRepository):
    """Create and confirm a request, returning it ready to be claimed."""

    def build(**kwargs) -> TradeRequest:
        request = make_request(**kwargs)
        repository.create(request)
        return repository.confirm(
            request.request_id,
            USER,
            request.quote,
            confirmation_ttl_seconds=300.0,
        )

    return build


@pytest.fixture
def make_worker(repository: TradeRequestRepository, settings: ExecutionSettings):
    """Build an ExecutionWorker. Call twice with the same broker to simulate a restart.

    The broker -- and therefore its ledger -- is deliberately passed in rather than
    created here, so a "restart" keeps the ledger and a test can assert exactly-once
    across two process lifetimes.
    """
    from aureon.execution.execution_worker import ExecutionWorker

    def build(broker: FakeBroker, *, executor_id: str | None = None, **kwargs):
        return ExecutionWorker(
            repository,
            broker,
            magic=MAGIC,
            settings_provider=lambda: settings,
            executor_id=executor_id,
            lease_seconds=kwargs.pop("lease_seconds", 60.0),
            **kwargs,
        )

    return build


@pytest.fixture
def make_reconciler(repository: TradeRequestRepository):
    from aureon.execution.reconciliation_service import ReconciliationService

    def build(broker: FakeBroker, *, grace_seconds: float = 0.0):
        return ReconciliationService(
            repository, broker, magic=MAGIC, grace_seconds=grace_seconds
        )

    return build


@pytest.fixture
def ledger_count(broker: FakeBroker):
    """How many orders a request actually placed at the broker."""
    from aureon.models.identity import comment_token

    def count(request_id: str) -> int:
        return len(broker.executions_for(comment_token(request_id)))

    return count


def audit_actions(firestore_client, request_id: str) -> list[str]:
    """Audit actions recorded for a request, for the §60 assertions."""
    from aureon.storage import paths

    return sorted(
        doc.to_dict()["action"]
        for doc in firestore_client.collection(paths.AUDIT_LOGS).stream()
        if doc.to_dict().get("document_id") == request_id
    )


def iter_all(firestore_client, collection: str) -> Iterator[dict]:
    for doc in firestore_client.collection(collection).stream():
        yield doc.to_dict()


# ── A Discord interaction, reduced to what the handlers actually use ───────────
#
# Shared because more than one suite drives a real handler: ``/execute`` (9A) and the
# [Execute] button under an announcement (9C) must reach the SAME confirmation, and two
# copies of the double would eventually diverge in exactly the place that matters -- a
# modal recorded by one and ignored by the other would hide a button that skipped the lot.


@dataclass
class FakeResponse:
    deferred: bool = False
    messages: list[Any] = field(default_factory=list)
    edits: list[Any] = field(default_factory=list)
    modals: list[Any] = field(default_factory=list)

    async def defer(self, **kwargs: Any) -> None:
        self.deferred = True

    def is_done(self) -> bool:
        return self.deferred

    async def send_message(self, **kwargs: Any) -> None:
        self.messages.append(kwargs)

    async def edit_message(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)

    async def send_modal(self, modal: Any) -> None:
        self.modals.append(modal)


@dataclass
class FakeFollowup:
    sends: list[Any] = field(default_factory=list)

    async def send(self, **kwargs: Any) -> None:
        self.sends.append(kwargs)


@dataclass
class FakeUser:
    id: str


class FakeInteraction:
    def __init__(self, user_id: str = USER) -> None:
        self.user = FakeUser(user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    # What the assertions read: the embeds this interaction was shown.
    @property
    def embeds(self) -> list[Any]:
        return [
            call["embed"]
            for call in [*self.followup.sends, *self.response.messages, *self.response.edits]
            if "embed" in call
        ]

    @property
    def views(self) -> list[Any]:
        return [call["view"] for call in self.followup.sends if call.get("view")]

    @property
    def modals(self) -> list[Any]:
        return list(self.response.modals)


def embed_text(embed: Any) -> str:
    parts = [str(embed.title or ""), str(embed.description or "")]
    parts += [f"{f.name} {f.value}" for f in embed.fields]
    return "\n".join(parts)
