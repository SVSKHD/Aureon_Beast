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

    def status_for(
        self,
        symbol: str,
        market_date: str,
        *,
        feature_schema_version: str | None = None,
        label_schema_version: str | None = None,
    ) -> DailyTrainingStatus | None:
        statement = (
            select(self.statuses)
            .where(self.statuses.c.symbol == symbol.upper())
            .where(self.statuses.c.market_date == market_date)
        )
        if feature_schema_version is not None:
            statement = statement.where(
                self.statuses.c.feature_schema_version == feature_schema_version
            )
        if label_schema_version is not None:
            statement = statement.where(
                self.statuses.c.label_schema_version == label_schema_version
            )
        statement = statement.order_by(self.statuses.c.generated_at.desc()).limit(1)
        rows = self._rows(statement)
        if not rows:
            return None
        return DailyTrainingStatus.model_validate(self._status_dict(rows[0]))

    def latest_status(self, symbol: str) -> DailyTrainingStatus | None:
        statement = (
            select(self.statuses)
            .where(self.statuses.c.symbol == symbol.upper())
            .order_by(
                self.statuses.c.market_date.desc(),
                self.statuses.c.generated_at.desc(),
            )
            .limit(1)
        )
        rows = self._rows(statement)
        if not rows:
            return None
        return DailyTrainingStatus.model_validate(self._status_dict(rows[0]))

    def examples_between(
        self,
        symbol: str,
        start_market_date: str,
        end_market_date: str,
    ) -> list[TrainingExample]:
        """Examples in the half-open broker-date range [start, end)."""
        statement = (
            select(self.examples)
            .where(self.examples.c.symbol == symbol.upper())
            .where(self.examples.c.market_date >= start_market_date)
            .where(self.examples.c.market_date < end_market_date)
            .order_by(
                self.examples.c.market_date,
                self.examples.c.timeframe,
                self.examples.c.setup_id,
            )
        )
        return [
            TrainingExample.model_validate(self._example_dict(row))
            for row in self._rows(statement)
        ]

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
            "example_id": example.example_id,
            "schema_version": example.schema_version,
            "market_date": example.market_date,
            "symbol": example.symbol,
            "timeframe": example.timeframe.value,
            "setup_id": example.setup_id,
            "family": example.family.value,
            "direction_context": example.direction_context.value,
            "setup_version": example.setup_version,
            "feature_schema_version": example.feature_schema_version,
            "label_schema_version": example.label_schema_version,
            "context": payload["context"],
            "agent_read": payload["agent_read"],
            "features": payload["features"],
            "outcome": payload["outcome"],
            "setup_created_at": example.setup_created_at,
            "feature_frozen_at": example.feature_frozen_at,
            "outcome_resolved_at": example.outcome_resolved_at,
            "entry_price": example.entry_price,
            "direction": example.direction,
            "six_dollar_status": example.six_dollar_status,
            "six_dollar_reached": example.six_dollar_reached,
            "six_dollar_reference_price": example.six_dollar_reference_price,
            "six_dollar_threshold_price": example.six_dollar_threshold_price,
            "six_dollar_reached_at": example.six_dollar_reached_at,
            "time_to_six_seconds": example.time_to_six_seconds,
            "twenty_dollar_reached": example.twenty_dollar_reached,
            "forty_dollar_reached": example.forty_dollar_reached,
            "time_to_twenty_seconds": example.time_to_twenty_seconds,
            "time_to_forty_seconds": example.time_to_forty_seconds,
            "max_favourable_move_price": example.max_favourable_move_price,
            "extension_after_six_price": example.extension_after_six_price,
            "mfe_points": example.mfe_points,
            "mae_points": example.mae_points,
            "mae_before_six_price": example.mae_before_six_price,
            "evaluation_rule_id": example.evaluation_rule_id,
            "evaluation_complete": example.evaluation_complete,
            "generated_at": example.generated_at,
        }

    @staticmethod
    def _example_dict(row: Any) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _status_row(status: DailyTrainingStatus) -> dict[str, Any]:
        payload = status.model_dump(mode="json")
        return {
            "status_id": status.status_id,
            "schema_version": status.schema_version,
            "market_date": status.market_date,
            "symbol": status.symbol,
            "feature_schema_version": status.feature_schema_version,
            "label_schema_version": status.label_schema_version,
            "examples_written": status.examples_written,
            "reached_six": status.reached_six,
            "not_reached_six": status.not_reached_six,
            "unavailable_six": status.unavailable_six,
            "complete_evaluations": status.complete_evaluations,
            "mae_before_six_available": status.mae_before_six_available,
            "median_mae_before_six_price": status.median_mae_before_six_price,
            "max_mae_before_six_price": status.max_mae_before_six_price,
            "by_timeframe": {"items": payload["by_timeframe"]},
            "generated_at": status.generated_at,
        }

    @staticmethod
    def _status_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["by_timeframe"] = (data.get("by_timeframe") or {}).get("items", [])
        return data
