"""Schema-checked Champion/Shadow inference boundary for Aureon V1.

Predictions are intelligence only. This service cannot create trade requests or call a broker.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.learning_v1 import (
    EntryIntelligence,
    FEATURE_SCHEMA_V1,
    LABEL_SCHEMA_V1,
    MODEL_SCHEMA_V1,
)
from aureon.models.ml import ModelPrediction, ModelRegistryEntry
from aureon.services.learning_contract import FeatureBuilder
from aureon.services.v1_model_training import predict_v1_artifact

log = logging.getLogger(__name__)


class PredictionService:
    """One fail-closed inference path shared by Champion and Shadow models."""

    def __init__(
        self,
        models: Any,
        *,
        now: Any = utc_now,
        clean_threshold: float = 0.55,
        target_threshold: float = 0.50,
    ) -> None:
        self.models = models
        self._now = now
        self.clean_threshold = clean_threshold
        self.target_threshold = target_threshold

    def predict_champion(
        self,
        setup: Any,
        event: Any,
        *,
        features: Any | None = None,
    ) -> EntryIntelligence | None:
        model = self.models.champion(setup.symbol)
        if model is None:
            log.warning("%s has no Champion; ML intelligence unavailable", setup.symbol)
            return None
        return self._predict(model, setup, event, role="champion", features=features)

    def predict_shadow(
        self,
        setup: Any,
        event: Any,
        *,
        features: Any | None = None,
    ) -> EntryIntelligence | None:
        model = self.models.active_shadow(setup.symbol)
        if model is None:
            return None
        return self._predict(model, setup, event, role="shadow", features=features)

    def _predict(
        self,
        model: ModelRegistryEntry,
        setup: Any,
        event: Any,
        *,
        role: str,
        features: Any | None = None,
    ) -> EntryIntelligence | None:
        if (
            model.feature_schema_version != FEATURE_SCHEMA_V1
            or model.label_schema_version != LABEL_SCHEMA_V1
            or model.model_schema_version != MODEL_SCHEMA_V1
        ):
            log.warning(
                "%s model %s contract mismatch feature=%s label=%s model=%s; skipped",
                role,
                model.model_id,
                model.feature_schema_version,
                model.label_schema_version,
                model.model_schema_version,
            )
            return None

        try:
            frozen = features or FeatureBuilder.from_setup_event(setup, event)
            probabilities = predict_v1_artifact(model, frozen)
        except Exception:
            log.exception("%s prediction failed closed for model %s", role, model.model_id)
            return None

        existing = self.models.prediction_for(model.model_id, setup.setup_id)
        predicted_at = to_utc(event.market_time.utc)
        if existing is None:
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
                predicted_at=predicted_at,
                feature_schema_version=model.feature_schema_version,
                label_schema_version=model.label_schema_version,
                probabilities=probabilities,
                feature_snapshot=frozen.model_dump(mode="json"),
            )
            self.models.write_prediction(prediction)

        clean = probabilities.get("clean_10")
        recommended_target = 0
        for target in (5, 10, 20, 30, 40):
            probability = probabilities.get(f"reach_{target}")
            if probability is not None and probability >= self.target_threshold:
                recommended_target = target

        if clean is None:
            decision = "ML_UNAVAILABLE"
            reason = "Champion did not emit P(clean_10)"
        elif clean >= self.clean_threshold:
            decision = "ML_SUPPORT"
            reason = (
                f"P(clean_10)={clean:.3f} >= {self.clean_threshold:.3f}; "
                "risk and deterministic gates still decide execution"
            )
        else:
            decision = "ML_NO_SUPPORT"
            reason = f"P(clean_10)={clean:.3f} < {self.clean_threshold:.3f}"

        return EntryIntelligence(
            model_id=model.model_id,
            setup_id=setup.setup_id,
            symbol=setup.symbol,
            timeframe=setup.timeframe,
            direction=frozen.direction,
            predicted_at=predicted_at,
            probabilities=probabilities,
            confidence=clean,
            recommended_target=recommended_target,
            decision=decision,
            reason=reason,
        )
