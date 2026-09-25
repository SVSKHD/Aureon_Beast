"""Persistence for EOD training memory.

Training rows are derived from immutable setup evidence plus later outcomes. Re-running an EOD
build addresses the same deterministic primary keys, so the operation is idempotent.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from aureon.models.training import DailyTrainingStatus, TrainingExample
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


class TrainingMemoryRepository(PostgresRepository):
    examples = tables.TrainingExample.__table__
    statuses = tables.DailyTrainingStatus.__table__

    def write_example(self, example: TrainingExample) -> TrainingExample:
        self._upsert(self._example_row(example), table=self.examples)
        return example

    def write_status(self, status: DailyTrainingStatus) -> DailyTrainingStatus:
        self._upsert(self._status_row(status), table=self.statuses)
        return status

    def status_for(self, symbol: str, market_date: str) -> DailyTrainingStatus | None:
        statement = (
            select(self.statuses)
            .where(self.statuses.c.symbol == symbol.upper())
            .where(self.statuses.c.market_date == market_date)
            .limit(1)
        )
        rows = self._rows(statement)
        if not rows:
            return None
        return DailyTrainingStatus.model_validate(self._status_dict(rows[0]))

    def latest_status(self, symbol: str) -> DailyTrainingStatus | None:
        statement = (
            select(self.statuses)
            .where(self.statuses.c.symbol == symbol.upper())
            .order_by(self.statuses.c.market_date.desc())
            .limit(1)
        )
        rows = self._rows(statement)
        if not rows:
            return None
        return DailyTrainingStatus.model_validate(self._status_dict(rows[0]))

    def examples_for(self, symbol: str, market_date: str) -> list[TrainingExample]:
        statement = (
            select(self.examples)
            .where(self.examples.c.symbol == symbol.upper())
            .where(self.examples.c.market_date == market_date)
            .order_by(self.examples.c.timeframe, self.examples.c.setup_id)
        )
        return [
            TrainingExample.model_validate(self._example_dict(row))
            for row in self._rows(statement)
        ]

    @staticmethod
    def _example_row(example: TrainingExample) -> dict[str, Any]:
        payload = example.model_dump(mode="json")
        return {
            **payload,
            "timeframe": example.timeframe.value,
            "family": example.family.value,
            "direction_context": example.direction_context.value,
        }

    @staticmethod
    def _example_dict(row: Any) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _status_row(status: DailyTrainingStatus) -> dict[str, Any]:
        payload = status.model_dump(mode="json")
        payload["by_timeframe"] = {"items": payload["by_timeframe"]}
        return payload

    @staticmethod
    def _status_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["by_timeframe"] = (data.get("by_timeframe") or {}).get("items", [])
        return data
