"""Persistence for trained models, backtests and shadow predictions."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from aureon.models.ml import (
    EvolutionDecision,
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
    evolution = tables.ModelEvolutionDecision.__table__

    def write_model(self, model: ModelRegistryEntry) -> ModelRegistryEntry:
        existing = self.get_model(model.model_id)
        if existing is not None and existing.status == "champion":
            # Production intelligence is immutable through ordinary writes. Promotion,
            # retirement and metric updates must use explicit lifecycle methods.
            if existing.model_dump(mode="json") != model.model_dump(mode="json"):
                raise RuntimeError(
                    f"refusing to overwrite Champion {model.model_id}; "
                    "use explicit lifecycle methods"
                )
            return existing
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

    def active_champion(self, symbol: str) -> ModelRegistryEntry | None:
        statement = (
            select(self.models)
            .where(self.models.c.symbol == symbol.upper())
            .where(self.models.c.status == "champion")
            .order_by(self.models.c.activated_at.desc(), self.models.c.created_at.desc())
            .limit(1)
        )
        rows = self._rows(statement)
        return None if not rows else ModelRegistryEntry.model_validate(self._model_dict(rows[0]))

    def models_by_status(self, symbol: str, status: str) -> list[ModelRegistryEntry]:
        statement = (
            select(self.models)
            .where(self.models.c.symbol == symbol.upper())
            .where(self.models.c.status == status)
            .order_by(self.models.c.created_at.desc())
        )
        return [
            ModelRegistryEntry.model_validate(self._model_dict(row))
            for row in self._rows(statement)
        ]

    def set_status(
        self,
        model_id: str,
        status: str,
        *,
        at: Any | None = None,
        promotion_reason: str | None = None,
    ) -> ModelRegistryEntry:
        allowed = {"candidate", "challenger", "shadow", "champion", "retired", "rejected"}
        if status not in allowed:
            raise ValueError(f"unsupported model status {status!r}")
        current = self.get_model(model_id)
        if current is None:
            raise LookupError(f"no model {model_id}")
        if current.status == "champion" and status not in {"champion", "retired"}:
            raise RuntimeError("Champion can only remain Champion or be explicitly retired")
        values: dict[str, Any] = {"status": status}
        if status in {"shadow", "champion"} and at is not None:
            values["activated_at"] = at
        if status == "retired" and at is not None:
            values["retired_at"] = at
        if promotion_reason is not None:
            values["promotion_reason"] = promotion_reason
        with self._db.transaction() as connection:
            connection.execute(
                update(self.models)
                .where(self.models.c.model_id == model_id)
                .values(**values)
            )
        refreshed = self.get_model(model_id)
        if refreshed is None:
            raise LookupError(f"model {model_id} disappeared after status transition")
        return refreshed

    def promote_champion(
        self,
        model_id: str,
        *,
        at: Any,
        reason: str,
        allow_bootstrap_candidate: bool = False,
    ) -> ModelRegistryEntry:
        """Atomically replace the Champion; ordinary writes cannot do this."""
        with self._db.transaction() as connection:
            row = self._row(model_id, table=self.models, connection=connection)
            if row is None:
                raise LookupError(f"no model {model_id}")
            status = str(row["status"])
            allowed = {"shadow", "challenger"}
            if allow_bootstrap_candidate:
                allowed.add("candidate")
            if status not in allowed:
                raise RuntimeError(
                    f"{model_id} is {status}; only {sorted(allowed)} may be promoted"
                )
            symbol = str(row["symbol"])
            connection.execute(
                update(self.models)
                .where(self.models.c.symbol == symbol)
                .where(self.models.c.status == "champion")
                .where(self.models.c.model_id != model_id)
                .values(status="retired", retired_at=at)
            )
            connection.execute(
                update(self.models)
                .where(self.models.c.model_id == model_id)
                .values(
                    status="champion",
                    activated_at=at,
                    retired_at=None,
                    promotion_reason=reason,
                )
            )
        refreshed = self.get_model(model_id)
        if refreshed is None:
            raise LookupError(f"model {model_id} disappeared after promotion")
        return refreshed

    def write_evolution_decision(self, decision: EvolutionDecision) -> EvolutionDecision:
        self._upsert(
            {
                "decision_id": decision.decision_id,
                "schema_version": decision.schema_version,
                "symbol": decision.symbol,
                "model_id": decision.model_id,
                "champion_model_id": decision.champion_model_id,
                "action": decision.action,
                "reason": decision.reason,
                "metrics": decision.metrics,
                "detail": decision.detail,
                "decided_at": decision.decided_at,
            },
            table=self.evolution,
        )
        return decision

    def evolution_for_symbol(self, symbol: str, *, limit: int = 200) -> list[EvolutionDecision]:
        statement = (
            select(self.evolution)
            .where(self.evolution.c.symbol == symbol.upper())
            .order_by(self.evolution.c.decided_at.desc())
            .limit(limit)
        )
        return [
            EvolutionDecision.model_validate(dict(row))
            for row in self._rows(statement)
        ]

    def activate_shadow(self, model_id: str, *, at: Any) -> ModelRegistryEntry:
        with self._db.transaction() as connection:
            row = self._row(model_id, table=self.models, connection=connection)
            if row is None:
                raise LookupError(f"no model {model_id}")
            symbol = str(row["symbol"])
            status = str(row["status"])
            if status not in {"candidate", "challenger", "shadow"}:
                raise RuntimeError(
                    f"{model_id} is {status}; only candidate/challenger may enter shadow"
                )
            connection.execute(
                update(self.models)
                .where(self.models.c.symbol == symbol)
                .where(self.models.c.status == "shadow")
                .where(self.models.c.model_id != model_id)
                .values(status="retired", retired_at=at)
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

    def update_model_metrics(
        self,
        model_id: str,
        *,
        validation_metrics: dict[str, Any] | None = None,
        shadow_metrics: dict[str, Any] | None = None,
    ) -> ModelRegistryEntry:
        current = self.get_model(model_id)
        if current is None:
            raise LookupError(f"no model {model_id}")
        values: dict[str, Any] = {}
        if validation_metrics is not None:
            values["validation_metrics"] = validation_metrics
        if shadow_metrics is not None:
            values["shadow_metrics"] = shadow_metrics
        if values:
            with self._db.transaction() as connection:
                connection.execute(
                    update(self.models)
                    .where(self.models.c.model_id == model_id)
                    .values(**values)
                )
        refreshed = self.get_model(model_id)
        if refreshed is None:
            raise LookupError(f"model {model_id} disappeared while updating metrics")
        return refreshed

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

        target_names = (
            "clean_10", "reach_5", "reach_10", "reach_20", "reach_30", "reach_40"
        )
        return ShadowPredictionSummary(
            predictions=len(rows),
            reconciled=len(reconciled),
            clean_10_brier=brier("clean_10"),
            target_brier={name: brier(name) for name in target_names},
            six_brier=brier("six"),
            twenty_brier=brier("twenty"),
            forty_brier=brier("forty"),
        )

    def prediction_summary_for_model(
        self,
        model_id: str,
        *,
        limit: int = 500,
    ) -> ShadowPredictionSummary:
        statement = (
            select(self.predictions)
            .where(self.predictions.c.model_id == model_id)
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

        targets = ("clean_10", "reach_5", "reach_10", "reach_20", "reach_30", "reach_40")
        return ShadowPredictionSummary(
            predictions=len(rows),
            reconciled=len(reconciled),
            clean_10_brier=brier("clean_10"),
            target_brier={target: brier(target) for target in targets},
            six_brier=brier("six"),
            twenty_brier=brier("twenty"),
            forty_brier=brier("forty"),
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
            "created_at": model.created_at,
            "activated_at": model.activated_at,
            "retired_at": model.retired_at,
            "promotion_reason": model.promotion_reason,
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
            "breakdown_metrics": backtest.breakdown_metrics,
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
            "mode": prediction.mode,
            "decision_intelligence": prediction.decision_intelligence,
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
