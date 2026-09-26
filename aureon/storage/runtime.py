"""Runtime storage composition.

For now Aureon is intentionally local-only: one SQLite WAL database shared by the five
local processes. The service layer receives repositories, never a Firebase client and
never a cloud SDK. A future PostgreSQL backend can be plugged in here without changing
observer/executor/monitor/Discord/review business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aureon.storage.backend import StorageBackend, selected_backend
from aureon.storage.local_database import LocalDatabase, get_database
from aureon.storage.postgres.repositories.alerts import PriceAlertRepository
from aureon.storage.postgres.repositories.control_requests import ControlRequestRepository
from aureon.storage.postgres.repositories.detections import DetectionRepository
from aureon.storage.postgres.repositories.evaluations import EvaluationRepository
from aureon.storage.postgres.repositories.market_days import MarketDayRepository
from aureon.storage.postgres.repositories.models import ModelRepository
from aureon.storage.postgres.repositories.notifications import NotificationRepository
from aureon.storage.postgres.repositories.operations import (
    ExecutionSettingsRepository,
    HeartbeatRepository,
    NotificationSettingsRepository,
    SymbolRepository,
    SystemStateRepository,
)
from aureon.storage.postgres.repositories.research import (
    AssessmentRepository,
    OpsEventRepository,
    TradeNoteRepository,
)
from aureon.storage.postgres.repositories.reviews import ReviewReader, ReviewRepository
from aureon.storage.postgres.repositories.sessions import SessionRepository
from aureon.storage.postgres.repositories.setup_evaluations import SetupEvaluationRepository
from aureon.storage.postgres.repositories.setups import SetupRepository
from aureon.storage.postgres.repositories.trade_requests import TradeRequestRepository
from aureon.storage.postgres.repositories.training import TrainingMemoryRepository
from aureon.storage.postgres.repositories.trades import TradeRepository


class SetupReadAdapter:
    """Read-only shape Discord expects, backed by the SQL setup repository."""

    def __init__(self, repository: SetupRepository) -> None:
        self._repo = repository

    def get(self, setup_id: str) -> Any:
        return self._repo.get(setup_id)

    def get_event(self, setup_id: str, event_id: str) -> Any:
        event = self._repo.events.get(event_id)
        return event if event is not None and event.setup_id == setup_id else None

    def events(self, setup_id: str, *, limit: int | None = None) -> list[Any]:
        return self._repo.events.for_setup(setup_id, limit=limit)

    def open_setups(self, symbol: str) -> list[Any]:
        return self._repo.open_for_symbol(symbol)

    def changed_since(
        self, *, symbol: str | None = None, since: datetime
    ) -> list[Any]:
        rows = self._repo.changed_since(since)
        if symbol is None:
            return rows
        return [row for row in rows if row.symbol == symbol]


class TrainingMemoryReadAdapter:
    """Read-only training-memory surface exposed to Discord."""

    def __init__(self, repository: TrainingMemoryRepository) -> None:
        self._repo = repository

    def status_for(self, symbol: str, market_date: str) -> Any:
        return self._repo.status_for(symbol, market_date)

    def latest_status(self, symbol: str) -> Any:
        return self._repo.latest_status(symbol)

    def examples_for(self, symbol: str, market_date: str) -> list[Any]:
        return self._repo.examples_for(symbol, market_date)

    def examples_between(
        self,
        symbol: str,
        start_market_date: str,
        end_market_date: str,
    ) -> list[Any]:
        return self._repo.examples_between(
            symbol,
            start_market_date,
            end_market_date,
        )

    def canonical_between(
        self,
        symbol: str,
        start_market_date: str,
        end_market_date: str,
    ) -> list[Any]:
        return self._repo.canonical_between(
            symbol,
            start_market_date,
            end_market_date,
        )


class ModelReadAdapter:
    """Read-only model status surface for Discord."""

    def __init__(self, repository: ModelRepository) -> None:
        self._repo = repository

    def latest_model(self, symbol: str) -> Any:
        return self._repo.latest_model(symbol)

    def active_shadow(self, symbol: str) -> Any:
        return self._repo.active_shadow(symbol)

    def champion(self, symbol: str) -> Any:
        return self._repo.champion(symbol)

    def challengers(self, symbol: str) -> list[Any]:
        return self._repo.challengers(symbol)

    def latest_training_run(self, symbol: str) -> Any:
        return self._repo.latest_training_run(symbol)

    def latest_backtest(self, symbol: str) -> Any:
        return self._repo.latest_backtest(symbol)

    def shadow_summary(self, symbol: str) -> Any:
        return self._repo.shadow_summary(symbol)


class SessionReadAdapter:
    """Read-only session shape exposed to Discord."""

    def __init__(self, repository: SessionRepository) -> None:
        self._repo = repository

    def for_market_date(self, market_date: str, *, symbol: str | None = None) -> list[Any]:
        return self._repo.for_market_date(market_date, symbol=symbol)


class PeriodReadAdapter:
    """Read-only aggregate source for the review service."""

    def __init__(self, storage: StorageRuntime) -> None:
        self._s = storage

    def detections_in(self, start: datetime, end: datetime) -> list[Any]:
        return self._s.detections.in_period(start, end)

    def evaluations_for(self, detections: list[Any], rule_id: str) -> dict[str, Any]:
        return self._s.evaluations.get_many([d.detection_id for d in detections], rule_id)

    def trades_opened_in(self, start: datetime, end: datetime) -> list[Any]:
        return self._s.trades.opened_in_period(start, end)

    def sessions_in(self, start: datetime, end: datetime) -> list[Any]:
        return self._s.sessions.in_period(start, end)

    def assessments_in(self, start: datetime, end: datetime) -> list[Any]:
        return self._s.assessments.in_period(start, end)

    def setups_in(self, start: datetime, end: datetime) -> list[Any]:
        return self._s.setups.in_period(start, end)

    def setup_evaluations_for(self, setups: list[Any], rule_id: str) -> dict[str, Any]:
        return self._s.setup_evaluations.get_many(setups, rule_id)

    def notes_for(self, trades: list[Any]) -> dict[str, list[Any]]:
        return self._s.notes.for_trades([t.trade_id for t in trades])


@dataclass
class StorageRuntime:
    database: LocalDatabase
    detections: DetectionRepository
    evaluations: EvaluationRepository
    sessions: SessionRepository
    symbols: SymbolRepository
    market_days: MarketDayRepository
    setups: SetupRepository
    setup_evaluations: SetupEvaluationRepository
    trade_requests: TradeRequestRepository
    trades: TradeRepository
    controls: ControlRequestRepository
    settings: ExecutionSettingsRepository
    notification_settings: NotificationSettingsRepository
    notifications: NotificationRepository
    alerts: PriceAlertRepository
    assessments: AssessmentRepository
    notes: TradeNoteRepository
    reviews: ReviewRepository
    review_reader: ReviewReader
    ops: OpsEventRepository
    heartbeats: HeartbeatRepository
    system_state: SystemStateRepository
    training_memory: TrainingMemoryRepository
    models: ModelRepository

    @property
    def setup_reader(self) -> SetupReadAdapter:
        return SetupReadAdapter(self.setups)

    @property
    def session_reader(self) -> SessionReadAdapter:
        return SessionReadAdapter(self.sessions)

    @property
    def training_reader(self) -> TrainingMemoryReadAdapter:
        return TrainingMemoryReadAdapter(self.training_memory)

    @property
    def model_reader(self) -> ModelReadAdapter:
        return ModelReadAdapter(self.models)

    @property
    def period_reader(self) -> PeriodReadAdapter:
        return PeriodReadAdapter(self)


def build_storage(
    *,
    account_scope: str = "primary",
    state_heartbeat_seconds: float = 5.0,
) -> StorageRuntime:
    backend = selected_backend()
    if backend is StorageBackend.POSTGRES:
        raise RuntimeError(
            "PostgreSQL is reserved for next week's storage decision and is not enabled "
            "in the current local-only runtime. Set AUREON_STORAGE_BACKEND=sqlite."
        )
    db = get_database()
    return StorageRuntime(
        database=db,
        detections=DetectionRepository(db),
        evaluations=EvaluationRepository(db),
        sessions=SessionRepository(db),
        symbols=SymbolRepository(db),
        market_days=MarketDayRepository(db),
        setups=SetupRepository(db),
        setup_evaluations=SetupEvaluationRepository(db),
        trade_requests=TradeRequestRepository(db),
        trades=TradeRepository(db, account_scope=account_scope),
        controls=ControlRequestRepository(db),
        settings=ExecutionSettingsRepository(db),
        notification_settings=NotificationSettingsRepository(db),
        notifications=NotificationRepository(db),
        alerts=PriceAlertRepository(db),
        assessments=AssessmentRepository(db),
        notes=TradeNoteRepository(db),
        reviews=ReviewRepository(db),
        review_reader=ReviewReader(db),
        ops=OpsEventRepository(db),
        heartbeats=HeartbeatRepository(db, min_interval_seconds=state_heartbeat_seconds),
        system_state=SystemStateRepository(db, min_interval_seconds=state_heartbeat_seconds),
        training_memory=TrainingMemoryRepository(db),
        models=ModelRepository(db),
    )


__all__ = [
    "PeriodReadAdapter",
    "SessionReadAdapter",
    "TrainingMemoryReadAdapter",
    "ModelReadAdapter",
    "SetupReadAdapter",
    "StorageRuntime",
    "build_storage",
]
