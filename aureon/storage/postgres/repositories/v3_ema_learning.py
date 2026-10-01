"""Storage for Aureon V3 EMA training examples, predictions and holdout days."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from aureon.models.ema_journey_v3 import EMA_FEATURE_SCHEMA_V3, EMA_LABEL_SCHEMA_V3
from aureon.services.v3_ema_learning import (
    CanonicalEMAExampleV3,
    EMAHoldoutDayV3,
)
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


class V3EMALearningRepository(PostgresRepository):
    examples = tables.V3EMAExample.__table__
    predictions = tables.V3EMAPrediction.__table__
    holdouts = tables.V3EMAHoldoutDay.__table__
    table = examples

    def write_example(self, example: CanonicalEMAExampleV3) -> CanonicalEMAExampleV3:
        self._upsert(
            {
                "example_id": example.example_id,
                "schema_version": example.schema_version,
                "journey_id": example.journey_id,
                "detection_id": example.detection_id,
                "symbol": example.symbol,
                "timeframe": example.timeframe.value,
                "direction": example.direction.value,
                "market_date": example.market_date,
                "anchor_type": example.anchor_type,
                "feature_schema": example.feature_schema,
                "label_schema": example.label_schema,
                "payload": example.model_dump(mode="json"),
                "generated_at": example.generated_at,
            },
            table=self.examples,
        )
        return example

    def example_for_detection(self, detection_id: str) -> CanonicalEMAExampleV3 | None:
        statement = (
            select(self.examples)
            .where(self.examples.c.detection_id == detection_id)
            .where(self.examples.c.feature_schema == EMA_FEATURE_SCHEMA_V3)
            .where(self.examples.c.label_schema == EMA_LABEL_SCHEMA_V3)
            .limit(1)
        )
        rows = self._rows(statement)
        return None if not rows else CanonicalEMAExampleV3.model_validate(dict(rows[0])["payload"])

    def examples_between(
        self,
        symbol: str,
        start_market_date: str,
        end_market_date: str,
    ) -> list[CanonicalEMAExampleV3]:
        statement = (
            select(self.examples)
            .where(self.examples.c.symbol == symbol.upper())
            .where(self.examples.c.market_date >= start_market_date)
            .where(self.examples.c.market_date <= end_market_date)
            .order_by(self.examples.c.generated_at)
        )
        return [
            CanonicalEMAExampleV3.model_validate(dict(row)["payload"])
            for row in self._rows(statement)
        ]

    def write_prediction(
        self,
        *,
        prediction_id: str,
        model_id: str,
        journey_id: str,
        detection_id: str,
        symbol: str,
        predicted_at: Any,
        payload: dict[str, Any],
    ) -> None:
        self._upsert(
            {
                "prediction_id": prediction_id,
                "schema_version": 1,
                "model_id": model_id,
                "journey_id": journey_id,
                "detection_id": detection_id,
                "symbol": symbol.upper(),
                "predicted_at": predicted_at,
                "payload": payload,
                "reconciled_at": None,
                "actual_outcome": None,
                "outcome_class": None,
            },
            table=self.predictions,
        )

    def prediction_for_detection(
        self,
        detection_id: str,
        *,
        model_id: str | None = None,
    ) -> dict[str, Any] | None:
        statement = select(self.predictions).where(
            self.predictions.c.detection_id == detection_id
        )
        if model_id is not None:
            statement = statement.where(self.predictions.c.model_id == model_id)
        statement = statement.order_by(self.predictions.c.predicted_at.desc()).limit(1)
        rows = self._rows(statement)
        return None if not rows else dict(rows[0])

    def unreconciled_predictions(self, detection_id: str) -> list[dict[str, Any]]:
        statement = (
            select(self.predictions)
            .where(self.predictions.c.detection_id == detection_id)
            .where(self.predictions.c.reconciled_at.is_(None))
            .order_by(self.predictions.c.predicted_at)
        )
        return [dict(row) for row in self._rows(statement)]

    def reconcile_prediction(
        self,
        prediction_id: str,
        *,
        outcome: dict[str, Any],
        at: Any,
    ) -> None:
        clean = bool(outcome.get("clean_10"))
        with self._db.transaction() as connection:
            row = connection.execute(
                select(self.predictions).where(
                    self.predictions.c.prediction_id == prediction_id
                )
            ).mappings().first()
            if row is None or row.get("reconciled_at") is not None:
                return
            payload = dict(row.get("payload") or {})
            p10 = payload.get("probability_clean_10")
            predicted_positive = p10 is not None and float(p10) >= 0.5
            outcome_class = (
                "true_positive" if predicted_positive and clean
                else "false_positive" if predicted_positive and not clean
                else "false_negative" if not predicted_positive and clean
                else "true_negative"
            )
            connection.execute(
                update(self.predictions)
                .where(self.predictions.c.prediction_id == prediction_id)
                .values(
                    actual_outcome=outcome,
                    reconciled_at=at,
                    outcome_class=outcome_class,
                )
            )

    def predictions_for_model(
        self,
        model_id: str,
        *,
        reconciled_only: bool = False,
    ) -> list[dict[str, Any]]:
        statement = select(self.predictions).where(
            self.predictions.c.model_id == model_id
        )
        if reconciled_only:
            statement = statement.where(self.predictions.c.reconciled_at.is_not(None))
        statement = statement.order_by(self.predictions.c.predicted_at)
        return [dict(row) for row in self._rows(statement)]

    def predictions_for_market_date(
        self,
        symbol: str,
        market_date: str,
        *,
        model_id: str | None = None,
        reconciled_only: bool = True,
    ) -> list[dict[str, Any]]:
        statement = (
            select(self.predictions)
            .where(self.predictions.c.symbol == symbol.upper())
        )
        if model_id is not None:
            statement = statement.where(self.predictions.c.model_id == model_id)
        if reconciled_only:
            statement = statement.where(self.predictions.c.reconciled_at.is_not(None))
        rows = [dict(row) for row in self._rows(statement)]
        return [
            row
            for row in rows
            if str(row["predicted_at"])[:10] == market_date
        ]

    def false_positive_detection_ids(self, symbol: str) -> set[str]:
        statement = (
            select(self.predictions.c.detection_id)
            .where(self.predictions.c.symbol == symbol.upper())
            .where(self.predictions.c.outcome_class == "false_positive")
        )
        return {
            str(row[0] if not hasattr(row, "keys") else dict(row)["detection_id"])
            for row in self._rows(statement)
        }

    def write_holdout(self, holdout: EMAHoldoutDayV3) -> EMAHoldoutDayV3:
        self._upsert(
            {
                "holdout_id": holdout.holdout_id,
                "schema_version": holdout.schema_version,
                "symbol": holdout.symbol.upper(),
                "market_date": holdout.market_date,
                "frozen_model_id": holdout.frozen_model_id,
                "status": holdout.status,
                "created_at": holdout.created_at,
                "scored_at": holdout.scored_at,
                "metrics": holdout.metrics,
            },
            table=self.holdouts,
        )
        return holdout

    def holdouts_for(self, symbol: str) -> list[EMAHoldoutDayV3]:
        statement = (
            select(self.holdouts)
            .where(self.holdouts.c.symbol == symbol.upper())
            .order_by(self.holdouts.c.market_date)
        )
        return [EMAHoldoutDayV3.model_validate(dict(row)) for row in self._rows(statement)]

    def holdout_for(self, symbol: str, market_date: str) -> EMAHoldoutDayV3 | None:
        statement = (
            select(self.holdouts)
            .where(self.holdouts.c.symbol == symbol.upper())
            .where(self.holdouts.c.market_date == market_date)
            .limit(1)
        )
        rows = self._rows(statement)
        return None if not rows else EMAHoldoutDayV3.model_validate(dict(rows[0]))

    def update_holdout(
        self,
        holdout: EMAHoldoutDayV3,
    ) -> EMAHoldoutDayV3:
        return self.write_holdout(holdout)

    def is_held_out(self, symbol: str, market_date: str) -> bool:
        statement = (
            select(self.holdouts.c.holdout_id)
            .where(self.holdouts.c.symbol == symbol.upper())
            .where(self.holdouts.c.market_date == market_date)
            .where(self.holdouts.c.status.in_(("open", "scored")))
            .limit(1)
        )
        return bool(self._rows(statement))
