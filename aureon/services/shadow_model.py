"""Live shadow inference from confirmed setups, with no execution authority."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from aureon.ml.features import FeatureEncoder, raw_setup_features
from aureon.ml.logistic import LogisticModel
from aureon.models.base import to_utc, utc_now
from aureon.models.learning_v1 import MODEL_SCHEMA_V1
from aureon.models.ml import ModelPrediction
from aureon.services.model_training import FEATURE_SCHEMA, LABEL_SCHEMA, MODEL_SCHEMA
from aureon.services.prediction_service import PredictionService

log = logging.getLogger(__name__)


class ShadowModelService:
    """Predict only. Never changes setup state, confluence, requests, or trades."""

    def __init__(self, models: Any, *, now: Any = utc_now) -> None:
        self.models = models
        self._now = now

    def predict_setup(
        self,
        setup: Any,
        event: Any,
        *,
        extra_context: dict[str, Any] | None = None,
    ) -> ModelPrediction | None:
        model = self.models.active_shadow(setup.symbol)
        if model is None:
            return None
        if model.model_schema_version == MODEL_SCHEMA_V1:
            # Reuse the canonical fail-closed prediction path. It persists the same
            # ModelPrediction contract and still has zero execution authority.
            PredictionService(self.models, now=self._now).predict_shadow(
                setup, event, extra_context=extra_context
            )
            return self.models.prediction_for(model.model_id, setup.setup_id)
        if (
            model.feature_schema_version != FEATURE_SCHEMA
            or model.label_schema_version != LABEL_SCHEMA
            or model.model_schema_version != MODEL_SCHEMA
        ):
            log.warning(
                "shadow model %s contract mismatch; prediction skipped",
                model.model_id,
            )
            return None

        existing = self.models.prediction_for(model.model_id, setup.setup_id)
        if existing is not None:
            return existing

        encoder = FeatureEncoder.from_dict(model.artifact["encoder"])
        numeric, categorical = raw_setup_features(setup, event)
        vector = encoder.transform(numeric, categorical)

        probabilities: dict[str, float] = {}
        for target, payload in (model.artifact.get("targets") or {}).items():
            probabilities[target] = LogisticModel.from_dict(payload).probability(vector)

        prediction_id = hashlib.sha256(
            f"{model.model_id}|{setup.setup_id}|{event.event_id}".encode("utf-8")
        ).hexdigest()
        prediction = ModelPrediction(
            prediction_id=prediction_id,
            model_id=model.model_id,
            setup_id=setup.setup_id,
            event_id=event.event_id,
            symbol=setup.symbol,
            timeframe=setup.timeframe,
            predicted_at=event.market_time.utc,
            feature_schema_version=model.feature_schema_version,
            label_schema_version=model.label_schema_version,
            probabilities=probabilities,
            feature_snapshot={
                "numeric": numeric,
                "categorical": categorical,
            },
        )
        self.models.write_prediction(prediction)
        log.info(
            "shadow prediction setup=%s model=%s p6=%s p20=%s p40=%s",
            setup.setup_id,
            model.model_id,
            _fmt(probabilities.get("six")),
            _fmt(probabilities.get("twenty")),
            _fmt(probabilities.get("forty")),
        )
        return prediction

    def reconcile_day(self, training_memory: Any, symbol: str, market_date: str) -> int:
        """Attach resolved outcomes to unreconciled predictions.

        Canonical V1 outcomes are not defined by EOD. The job may run at EOD, but it
        reconciles only examples whose canonical outcome has actually been resolved.
        Legacy examples remain supported unchanged.
        """
        count = 0
        moment = to_utc(self._now())

        canonical_reader = getattr(training_memory, "canonical_for", None)
        if callable(canonical_reader):
            for example in canonical_reader(symbol, market_date):
                predictions = self.models.predictions_for_setup(
                    example.setup_id,
                    unreconciled_only=True,
                )
                outcomes = example.outcome
                payload = {
                    "clean_10": outcomes.clean_10,
                    "reach_5": outcomes.reached_5,
                    "reach_10": outcomes.reached_10,
                    "reach_20": outcomes.reached_20,
                    "reach_30": outcomes.reached_30,
                    "reach_40": outcomes.reached_40,
                    "mae_before_10": outcomes.mae_before_10,
                    "max_favourable_move": outcomes.max_favourable_move,
                    "max_adverse_move": outcomes.max_adverse_move,
                }
                for prediction in predictions:
                    self.models.reconcile_prediction(
                        prediction.model_id,
                        example.setup_id,
                        outcomes=payload,
                        at=moment,
                    )
                    count += 1
        for example in training_memory.examples_for(symbol, market_date):
            predictions = self.models.predictions_for_setup(
                example.setup_id,
                unreconciled_only=True,
            )
            for prediction in predictions:
                self.models.reconcile_prediction(
                    prediction.model_id,
                    example.setup_id,
                    outcomes={
                        "six": example.six_dollar_reached,
                        "twenty": example.twenty_dollar_reached,
                        "forty": example.forty_dollar_reached,
                        "max_favourable_move_price": example.max_favourable_move_price,
                        "mae_before_six_price": example.mae_before_six_price,
                    },
                    at=moment,
                )
                count += 1
        return count


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"
