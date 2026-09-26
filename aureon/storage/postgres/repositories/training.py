"""Persistence for EOD training memory.

Training rows are derived from immutable setup evidence plus later outcomes. Re-running an EOD
build addresses the same deterministic primary keys, so the operation is idempotent.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select

from aureon.models.learning_v1 import CanonicalTrainingExample, PendingLearningSetup
from aureon.models.training import DailyTrainingStatus, TrainingExample
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


class TrainingMemoryRepository(PostgresRepository):
    examples = tables.TrainingExample.__table__
    statuses = tables.DailyTrainingStatus.__table__
    canonical = tables.CanonicalTrainingExample.__table__
    pending = tables.PendingLearningSetup.__table__

    def write_example(self, example: TrainingExample) -> TrainingExample:
        self._upsert(self._example_row(example), table=self.examples)
        return example

    def write_status(self, status: DailyTrainingStatus) -> DailyTrainingStatus:
        self._upsert(self._status_row(status), table=self.statuses)
        return status

    def write_pending(self, pending: PendingLearningSetup) -> PendingLearningSetup:
        payload = pending.model_dump(mode="json")
        self._upsert(
            {
                "setup_id": pending.setup_id,
                "schema_version": pending.schema_version,
                "market_date": pending.market_date,
                "symbol": pending.symbol,
                "timeframe": pending.timeframe.value,
                "feature_schema": pending.feature_schema,
                "label_schema": pending.label_schema,
                "features": payload["features"],
                "horizon_bars": pending.horizon_bars,
                "clean_target": pending.clean_target,
                "clean_max_mae": pending.clean_max_mae,
                "bars_seen": pending.bars_seen,
                "state": pending.state,
                "started_at": pending.started_at,
                "updated_at": pending.updated_at,
            },
            table=self.pending,
        )
        return pending

    def pending_for_stream(
        self, symbol: str, timeframe: str
    ) -> list[PendingLearningSetup]:
        statement = (
            select(self.pending)
            .where(self.pending.c.symbol == symbol.upper())
            .where(self.pending.c.timeframe == timeframe)
            .order_by(self.pending.c.started_at)
        )
        return [
            PendingLearningSetup.model_validate(dict(row))
            for row in self._rows(statement)
        ]

    def pending_for_setup(self, setup_id: str) -> PendingLearningSetup | None:
        row = self._row(setup_id, table=self.pending)
        return None if row is None else PendingLearningSetup.model_validate(dict(row))

    def delete_pending(self, setup_id: str) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                delete(self.pending).where(self.pending.c.setup_id == setup_id)
            )

    def write_canonical(
        self, example: CanonicalTrainingExample
    ) -> CanonicalTrainingExample:
        self._upsert(self._canonical_row(example), table=self.canonical)
        return example

    def canonical_between(
        self,
        symbol: str,
        start_market_date: str,
        end_market_date: str,
    ) -> list[CanonicalTrainingExample]:
        statement = (
            select(self.canonical)
            .where(self.canonical.c.symbol == symbol.upper())
            .where(self.canonical.c.market_date >= start_market_date)
            .where(self.canonical.c.market_date < end_market_date)
            .order_by(
                self.canonical.c.market_date,
                self.canonical.c.timeframe,
                self.canonical.c.setup_id,
            )
        )
        return [
            CanonicalTrainingExample.model_validate(dict(row))
            for row in self._rows(statement)
        ]

    def canonical_for(
        self, symbol: str, market_date: str
    ) -> list[CanonicalTrainingExample]:
        return [
            example
            for example in self.canonical_between(
                symbol,
                market_date,
                _next_iso_date(market_date),
            )
        ]

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
    def _canonical_row(example: CanonicalTrainingExample) -> dict[str, Any]:
        payload = example.model_dump(mode="json")
        return {
            "example_id": example.example_id,
            "schema_version": example.schema_version,
            "market_date": example.market_date,
            "setup_id": example.setup_id,
            "symbol": example.symbol,
            "timeframe": example.timeframe.value,
            "feature_schema": example.feature_schema,
            "label_schema": example.label_schema,
            "features": payload["features"],
            "outcome": payload["outcome"],
            "generated_at": example.generated_at,
        }

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


def _next_iso_date(value: str) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(value) + timedelta(days=1)).isoformat()
