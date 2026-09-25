"""Live shadow inference from confirmed setups, with no execution authority."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from aureon.ml.features import FeatureEncoder, raw_setup_features
from aureon.ml.logistic import LogisticModel
from aureon.models.base import to_utc, utc_now
from aureon.models.ml import ModelPrediction
from aureon.services.model_training import FEATURE_SCHEMA, LABEL_SCHEMA, MODEL_SCHEMA

log = logging.getLogger(__name__)


class ShadowModelService:
    """Predict only. Never changes setup state, confluence, requests, or trades."""

    def __init__(self, models: Any, *, now: Any = utc_now) -> None:
        self.models = models
        self._now = now

    def predict_setup(self, setup: Any, event: Any) -> ModelPrediction | None:
        model = self.models.active_shadow(setup.symbol)
        if model is None:
            return None
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
        """Attach completed EOD outcomes to any shadow predictions from that day's setups."""
        model = self.models.active_shadow(symbol)
        if model is None:
            return 0
        count = 0
        moment = to_utc(self._now())
        for example in training_memory.examples_for(symbol, market_date):
            prediction = self.models.prediction_for(model.model_id, example.setup_id)
            if prediction is None or prediction.actual_outcomes is not None:
                continue
            self.models.reconcile_prediction(
                model.model_id,
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
