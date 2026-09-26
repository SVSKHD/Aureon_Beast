"""Persistence for trained models, backtests and shadow predictions."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from aureon.models.learning_v1 import EvolutionDecision, ModelLifecycleStatus
from aureon.models.ml import (
    ModelBacktest,
    ModelPrediction,
    ModelRegistryEntry,
    ModelTrainingRun,
    ShadowPredictionSummary,
    TargetMetrics,
)
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


class ModelRepository(PostgresRepository):
    models = tables.ModelRegistry.__table__
    runs = tables.ModelTrainingRun.__table__
    backtests = tables.ModelBacktest.__table__
    predictions = tables.ModelPrediction.__table__
    evolution = tables.ModelEvolutionLog.__table__

    def write_model(self, model: ModelRegistryEntry) -> ModelRegistryEntry:
        self._upsert(self._model_row(model), table=self.models)
        return model

    def write_training_run(self, run: ModelTrainingRun) -> ModelTrainingRun:
        self._upsert(self._run_row(run), table=self.runs)
        return run

    def write_backtest(self, backtest: ModelBacktest) -> ModelBacktest:
        self._upsert(self._backtest_row(backtest), table=self.backtests)
        return backtest

    def write_prediction(self, prediction: ModelPrediction) -> ModelPrediction:
        self._upsert(self._prediction_row(prediction), table=self.predictions)
        return prediction

    def get_model(self, model_id: str) -> ModelRegistryEntry | None:
        row = self._row(model_id, table=self.models)
        return None if row is None else ModelRegistryEntry.model_validate(self._model_dict(row))

    def latest_model(self, symbol: str) -> ModelRegistryEntry | None:
        statement = (
            select(self.models)
            .where(self.models.c.symbol == symbol.upper())
            .order_by(self.models.c.created_at.desc())
            .limit(1)
        )
        rows = self._rows(statement)
        return None if not rows else ModelRegistryEntry.model_validate(self._model_dict(rows[0]))

    def champion(self, symbol: str) -> ModelRegistryEntry | None:
        statement = (
            select(self.models)
            .where(self.models.c.symbol == symbol.upper())
            .where(self.models.c.status == ModelLifecycleStatus.CHAMPION.value)
            .order_by(self.models.c.activated_at.desc(), self.models.c.created_at.desc())
            .limit(1)
        )
        rows = self._rows(statement)
        return None if not rows else ModelRegistryEntry.model_validate(self._model_dict(rows[0]))

    def challengers(self, symbol: str) -> list[ModelRegistryEntry]:
        statement = (
            select(self.models)
            .where(self.models.c.symbol == symbol.upper())
            .where(
                self.models.c.status.in_(
                    [
                        ModelLifecycleStatus.CANDIDATE.value,
                        ModelLifecycleStatus.CHALLENGER.value,
                        ModelLifecycleStatus.SHADOW.value,
                    ]
                )
            )
            .order_by(self.models.c.created_at.desc())
        )
        return [
            ModelRegistryEntry.model_validate(self._model_dict(row))
            for row in self._rows(statement)
        ]

    def active_shadow(self, symbol: str) -> ModelRegistryEntry | None:
        statement = (
            select(self.models)
            .where(self.models.c.symbol == symbol.upper())
            .where(self.models.c.status == "shadow")
            .order_by(self.models.c.activated_at.desc(), self.models.c.created_at.desc())
            .limit(1)
        )
        rows = self._rows(statement)
        return None if not rows else ModelRegistryEntry.model_validate(self._model_dict(rows[0]))

    def activate_shadow(self, model_id: str, *, at: Any) -> ModelRegistryEntry:
        with self._db.transaction() as connection:
            row = self._row(model_id, table=self.models, connection=connection)
            if row is None:
                raise LookupError(f"no model {model_id}")
            symbol = str(row["symbol"])
            connection.execute(
                update(self.models)
                .where(self.models.c.symbol == symbol)
                .where(self.models.c.status == "shadow")
                .values(status="retired")
            )
            connection.execute(
                update(self.models)
                .where(self.models.c.model_id == model_id)
                .values(status="shadow", activated_at=at)
            )
        refreshed = self.get_model(model_id)
        if refreshed is None:
            raise LookupError(f"model {model_id} disappeared after activation")
        return refreshed

    def set_status(
        self,
        model_id: str,
        status: ModelLifecycleStatus | str,
        *,
        at: Any | None = None,
        promotion_reason: str | None = None,
    ) -> ModelRegistryEntry:
        value = status.value if isinstance(status, ModelLifecycleStatus) else str(status)
        allowed = {one.value for one in ModelLifecycleStatus}
        if value not in allowed:
            raise ValueError(f"unsupported model lifecycle status {value!r}")
        with self._db.transaction() as connection:
            row = self._row(model_id, table=self.models, connection=connection)
            if row is None:
                raise LookupError(f"no model {model_id}")
            if value == ModelLifecycleStatus.CHAMPION.value:
                raise ValueError("use promote_champion() so Champion replacement is atomic")
            values: dict[str, Any] = {"status": value}
            if at is not None and value == ModelLifecycleStatus.SHADOW.value:
                values["activated_at"] = at
            if promotion_reason is not None:
                values["promotion_reason"] = promotion_reason
            connection.execute(
                update(self.models)
                .where(self.models.c.model_id == model_id)
                .values(**values)
            )
        refreshed = self.get_model(model_id)
        if refreshed is None:
            raise LookupError(f"model {model_id} disappeared after status update")
        return refreshed

    def promote_champion(
        self,
        model_id: str,
        *,
        at: Any,
        reason: str,
    ) -> ModelRegistryEntry:
        """Atomically promote a SHADOW model and retire the prior Champion.

        A candidate/challenger cannot skip shadow evaluation. This is the hard guard
        against silently overwriting production intelligence.
        """
        with self._db.transaction() as connection:
            row = self._row(model_id, table=self.models, connection=connection)
            if row is None:
                raise LookupError(f"no model {model_id}")
            if str(row["status"]) != ModelLifecycleStatus.SHADOW.value:
                raise ValueError(
                    f"model {model_id} is {row['status']}; only shadow may become champion"
                )
            symbol = str(row["symbol"])
            connection.execute(
                update(self.models)
                .where(self.models.c.symbol == symbol)
                .where(self.models.c.status == ModelLifecycleStatus.CHAMPION.value)
                .values(status=ModelLifecycleStatus.RETIRED.value)
            )
            connection.execute(
                update(self.models)
                .where(self.models.c.model_id == model_id)
                .values(
                    status=ModelLifecycleStatus.CHAMPION.value,
                    activated_at=at,
                    promotion_reason=reason,
                )
            )
        refreshed = self.get_model(model_id)
        if refreshed is None:
            raise LookupError(f"model {model_id} disappeared after promotion")
        return refreshed

    def write_evolution_decision(self, decision: EvolutionDecision) -> EvolutionDecision:
        payload = decision.model_dump(mode="json")
        self._upsert(
            {
                "decision_id": decision.decision_id,
                "schema_version": decision.schema_version,
                "symbol": decision.symbol,
                "model_id": decision.model_id,
                "champion_model_id": decision.champion_model_id,
                "action": decision.action,
                "reason": decision.reason,
                "metrics": payload["metrics"],
                "created_at": decision.created_at,
            },
            table=self.evolution,
        )
        return decision

    def evolution_for(self, model_id: str) -> list[EvolutionDecision]:
        statement = (
            select(self.evolution)
            .where(self.evolution.c.model_id == model_id)
            .order_by(self.evolution.c.created_at)
        )
        return [
            EvolutionDecision.model_validate(dict(row))
            for row in self._rows(statement)
        ]

    def latest_training_run(self, symbol: str) -> ModelTrainingRun | None:
        statement = (
            select(self.runs)
            .where(self.runs.c.symbol == symbol.upper())
            .order_by(self.runs.c.started_at.desc())
            .limit(1)
        )
        rows = self._rows(statement)
        return None if not rows else ModelTrainingRun.model_validate(self._run_dict(rows[0]))

    def latest_backtest(self, symbol: str) -> ModelBacktest | None:
        statement = (
            select(self.backtests)
            .where(self.backtests.c.symbol == symbol.upper())
            .order_by(self.backtests.c.started_at.desc())
            .limit(1)
        )
        rows = self._rows(statement)
        return None if not rows else ModelBacktest.model_validate(self._backtest_dict(rows[0]))

    def latest_backtest_for_model(self, model_id: str) -> ModelBacktest | None:
        statement = (
            select(self.backtests)
            .where(self.backtests.c.model_id == model_id)
            .order_by(self.backtests.c.started_at.desc())
            .limit(1)
        )
        rows = self._rows(statement)
        return None if not rows else ModelBacktest.model_validate(self._backtest_dict(rows[0]))

    def prediction_for(self, model_id: str, setup_id: str) -> ModelPrediction | None:
        statement = (
            select(self.predictions)
            .where(self.predictions.c.model_id == model_id)
            .where(self.predictions.c.setup_id == setup_id)
            .limit(1)
        )
        rows = self._rows(statement)
        return None if not rows else ModelPrediction.model_validate(self._prediction_dict(rows[0]))

    def predictions_for_model(
        self,
        model_id: str,
        *,
        reconciled_only: bool = False,
        limit: int | None = None,
    ) -> list[ModelPrediction]:
        statement = select(self.predictions).where(
            self.predictions.c.model_id == model_id
        )
        if reconciled_only:
            statement = statement.where(self.predictions.c.reconciled_at.is_not(None))
        statement = statement.order_by(self.predictions.c.predicted_at)
        if limit is not None:
            statement = statement.limit(limit)
        return [
            ModelPrediction.model_validate(self._prediction_dict(row))
            for row in self._rows(statement)
        ]

    def update_shadow_metrics(
        self,
        model_id: str,
        metrics: dict[str, Any],
    ) -> ModelRegistryEntry:
        with self._db.transaction() as connection:
            row = self._row(model_id, table=self.models, connection=connection)
            if row is None:
                raise LookupError(f"no model {model_id}")
            connection.execute(
                update(self.models)
                .where(self.models.c.model_id == model_id)
                .values(shadow_metrics=metrics)
            )
        refreshed = self.get_model(model_id)
        if refreshed is None:
            raise LookupError(f"model {model_id} disappeared after metrics update")
        return refreshed

    def predictions_for_setup(
        self,
        setup_id: str,
        *,
        unreconciled_only: bool = False,
    ) -> list[ModelPrediction]:
        statement = select(self.predictions).where(
            self.predictions.c.setup_id == setup_id
        )
        if unreconciled_only:
            statement = statement.where(self.predictions.c.reconciled_at.is_(None))
        statement = statement.order_by(self.predictions.c.predicted_at)
        return [
            ModelPrediction.model_validate(self._prediction_dict(row))
            for row in self._rows(statement)
        ]

    def reconcile_prediction(
        self,
        model_id: str,
        setup_id: str,
        *,
        outcomes: dict[str, bool | float | None],
        at: Any,
    ) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                update(self.predictions)
                .where(self.predictions.c.model_id == model_id)
                .where(self.predictions.c.setup_id == setup_id)
                .values(actual_outcomes=outcomes, reconciled_at=at)
            )

    def shadow_summary(self, symbol: str, *, limit: int = 500) -> ShadowPredictionSummary:
        model = self.active_shadow(symbol)
        if model is None:
            return ShadowPredictionSummary()
        statement = (
            select(self.predictions)
            .where(self.predictions.c.model_id == model.model_id)
            .order_by(self.predictions.c.predicted_at.desc())
            .limit(limit)
        )
        rows = [self._prediction_dict(row) for row in self._rows(statement)]
        reconciled = [row for row in rows if row.get("actual_outcomes") is not None]

        def brier(target: str) -> float | None:
            pairs: list[tuple[float, float]] = []
            for row in reconciled:
                actual = (row.get("actual_outcomes") or {}).get(target)
                probability = (row.get("probabilities") or {}).get(target)
                if actual is None or probability is None:
                    continue
                pairs.append((float(probability), 1.0 if bool(actual) else 0.0))
            if not pairs:
                return None
            return sum((probability - actual) ** 2 for probability, actual in pairs) / len(pairs)

        return ShadowPredictionSummary(
            predictions=len(rows),
            reconciled=len(reconciled),
            six_brier=brier("six"),
            twenty_brier=brier("twenty"),
            forty_brier=brier("forty"),
            clean_10_brier=brier("clean_10"),
        )

    @staticmethod
    def _metrics_json(metrics: dict[str, TargetMetrics]) -> dict[str, Any]:
        return {
            name: value.model_dump(mode="json")
            for name, value in metrics.items()
        }

    @classmethod
    def _model_row(cls, model: ModelRegistryEntry) -> dict[str, Any]:
        return {
            "model_id": model.model_id,
            "schema_version": model.schema_version,
            "symbol": model.symbol,
            "algorithm": model.algorithm,
            "status": model.status,
            "feature_schema_version": model.feature_schema_version,
            "label_schema_version": model.label_schema_version,
            "model_schema_version": model.model_schema_version,
            "parent_model_id": model.parent_model_id,
            "hyperparameters": model.hyperparameters,
            "trained_from": model.trained_from,
            "trained_through": model.trained_through,
            "training_samples": model.training_samples,
            "target_metrics": cls._metrics_json(model.target_metrics),
            "validation_metrics": model.validation_metrics,
            "shadow_metrics": model.shadow_metrics,
            "artifact": model.artifact,
            "promotion_reason": model.promotion_reason,
            "created_at": model.created_at,
            "activated_at": model.activated_at,
        }

    @classmethod
    def _run_row(cls, run: ModelTrainingRun) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "schema_version": run.schema_version,
            "model_id": run.model_id,
            "symbol": run.symbol,
            "status": run.status,
            "algorithm": run.algorithm,
            "feature_schema_version": run.feature_schema_version,
            "label_schema_version": run.label_schema_version,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "sample_count": run.sample_count,
            "trained_from": run.trained_from,
            "trained_through": run.trained_through,
            "target_metrics": cls._metrics_json(run.target_metrics),
            "failure_message": run.failure_message,
        }

    @classmethod
    def _backtest_row(cls, backtest: ModelBacktest) -> dict[str, Any]:
        payload = backtest.model_dump(mode="json")
        return {
            "backtest_id": backtest.backtest_id,
            "schema_version": backtest.schema_version,
            "model_id": backtest.model_id,
            "symbol": backtest.symbol,
            "status": backtest.status,
            "algorithm": backtest.algorithm,
            "feature_schema_version": backtest.feature_schema_version,
            "label_schema_version": backtest.label_schema_version,
            "started_at": backtest.started_at,
            "completed_at": backtest.completed_at,
            "start_market_date": backtest.start_market_date,
            "end_market_date": backtest.end_market_date,
            "folds": {"items": payload["folds"]},
            "aggregate_metrics": cls._metrics_json(backtest.aggregate_metrics),
            "out_of_sample_predictions": backtest.out_of_sample_predictions,
            "failure_message": backtest.failure_message,
        }

    @staticmethod
    def _prediction_row(prediction: ModelPrediction) -> dict[str, Any]:
        return {
            "prediction_id": prediction.prediction_id,
            "schema_version": prediction.schema_version,
            "model_id": prediction.model_id,
            "setup_id": prediction.setup_id,
            "event_id": prediction.event_id,
            "symbol": prediction.symbol,
            "timeframe": prediction.timeframe.value,
            "predicted_at": prediction.predicted_at,
            "feature_schema_version": prediction.feature_schema_version,
            "label_schema_version": prediction.label_schema_version,
            "probabilities": prediction.probabilities,
            "feature_snapshot": prediction.feature_snapshot,
            "actual_outcomes": prediction.actual_outcomes,
            "reconciled_at": prediction.reconciled_at,
        }

    @staticmethod
    def _model_dict(row: Any) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _run_dict(row: Any) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _backtest_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["folds"] = (data.get("folds") or {}).get("items", [])
        return data

    @staticmethod
    def _prediction_dict(row: Any) -> dict[str, Any]:
        return dict(row)
