"""Champion and Shadow inference over the same frozen V1 feature snapshot.

This module has no broker, trade-request, risk-mutation or execution dependency.
Malformed/missing/incompatible ML fails closed by returning no prediction.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from aureon.ml.features import FeatureEncoder, raw_setup_features
from aureon.models.base import to_utc, utc_now
from aureon.models.ml import ModelPrediction
from aureon.services.learning_contract import (
    FEATURE_SCHEMA_V1,
    LABEL_SCHEMA_V1,
    MODEL_SCHEMA_V1,
    FeatureBuilder,
    enforce_target_probability_order,
)
from aureon.services.model_training import probability_from_payload

log = logging.getLogger(__name__)


class PredictionService:
    """Read-only model intelligence. Never creates or executes trade requests."""

    def __init__(self, models: Any, *, now: Any = utc_now) -> None:
        self.models = models
        self._now = now

    def predict_champion(self, setup: Any, event: Any) -> ModelPrediction | None:
        return self._predict(
            self.models.active_champion(setup.symbol),
            setup,
            event,
            mode="champion",
        )

    def predict_shadow(self, setup: Any, event: Any) -> ModelPrediction | None:
        return self._predict(
            self.models.active_shadow(setup.symbol),
            setup,
            event,
            mode="shadow",
        )

    def _predict(
        self,
        model: Any | None,
        setup: Any,
        event: Any,
        *,
        mode: str,
    ) -> ModelPrediction | None:
        if model is None:
            return None
        if (
            model.feature_schema_version != FEATURE_SCHEMA_V1
            or model.label_schema_version != LABEL_SCHEMA_V1
            or model.model_schema_version != MODEL_SCHEMA_V1
        ):
            log.warning(
                "%s model %s contract mismatch; prediction skipped",
                mode,
                model.model_id,
            )
            return None

        existing = self.models.prediction_for(model.model_id, setup.setup_id)
        if existing is not None:
            return existing

        try:
            encoder = FeatureEncoder.from_dict(model.artifact["encoder"])
            numeric, categorical = raw_setup_features(setup, event)
            vector = encoder.transform(numeric, categorical)
            probabilities = {
                target: probability_from_payload(payload, vector)
                for target, payload in (model.artifact.get("targets") or {}).items()
            }
            probabilities = enforce_target_probability_order(probabilities)
            if "clean_10" not in probabilities:
                raise ValueError("model artifact has no clean_10 output")
        except Exception as exc:  # fail closed: deterministic pipeline continues without ML
            log.warning(
                "%s model %s prediction failed; ML unavailable for setup %s: %s",
                mode,
                model.model_id,
                setup.setup_id,
                exc,
            )
            return None

        recommended_target = 0
        for target in (5, 10, 20, 30, 40):
            probability = probabilities.get(f"reach_{target}")
            if probability is not None and probability >= 0.50:
                recommended_target = target

        direction = str(getattr(getattr(setup, "direction_context", None), "value", "unknown"))
        intelligence = {
            "direction": direction,
            "p_clean_10": probabilities.get("clean_10"),
            "p_reach_5": probabilities.get("reach_5"),
            "p_reach_10": probabilities.get("reach_10"),
            "p_reach_20": probabilities.get("reach_20"),
            "p_reach_30": probabilities.get("reach_30"),
            "p_reach_40": probabilities.get("reach_40"),
            "confidence": probabilities.get("clean_10"),
            "recommended_target": recommended_target,
            "execution_authority": False,
        }
        prediction_id = hashlib.sha256(
            f"{model.model_id}|{setup.setup_id}|{event.event_id}|{mode}".encode("utf-8")
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
            feature_snapshot=FeatureBuilder.freeze_setup(setup, event),
            mode=mode,
            decision_intelligence=intelligence,
        )
        self.models.write_prediction(prediction)
        log.info(
            "%s prediction setup=%s model=%s p_clean10=%.3f target=%s",
            mode,
            setup.setup_id,
            model.model_id,
            probabilities["clean_10"],
            recommended_target,
        )
        return prediction

    def reconcile_examples(self, examples: list[Any]) -> int:
        """Attach canonical outcomes to Champion/Shadow predictions after resolution."""
        count = 0
        moment = to_utc(self._now())
        for example in examples:
            if example.outcome is None:
                continue
            outcome = example.outcome
            outcomes = {
                "clean_10": outcome.clean_10,
                "reach_5": outcome.reached_5,
                "reach_10": outcome.reached_10,
                "reach_20": outcome.reached_20,
                "reach_30": outcome.reached_30,
                "reach_40": outcome.reached_40,
                "mae_before_10": outcome.mae_before_10,
                "max_favourable_move": outcome.max_favourable_move,
                "max_adverse_move": outcome.max_adverse_move,
            }
            for prediction in self.models.predictions_for_setup(
                example.setup_id,
                unreconciled_only=True,
            ):
                if (
                    prediction.feature_schema_version != FEATURE_SCHEMA_V1
                    or prediction.label_schema_version != LABEL_SCHEMA_V1
                ):
                    continue
                self.models.reconcile_prediction(
                    prediction.model_id,
                    example.setup_id,
                    outcomes=outcomes,
                    at=moment,
                )
                count += 1
        return count


class ShadowModelService:
    """Compatibility facade preserving existing observer/review wiring."""

    def __init__(self, models: Any, *, now: Any = utc_now) -> None:
        self.predictions = PredictionService(models, now=now)
        self.models = models

    def predict_setup(self, setup: Any, event: Any) -> ModelPrediction | None:
        return self.predictions.predict_shadow(setup, event)

    def predict_champion(self, setup: Any, event: Any) -> ModelPrediction | None:
        return self.predictions.predict_champion(setup, event)

    def reconcile_examples(self, examples: list[Any]) -> int:
        return self.predictions.reconcile_examples(examples)

    def reconcile_day(self, training_memory: Any, symbol: str, market_date: str) -> int:
        """Legacy-compatible entrypoint; reconciles only canonical V1 examples."""
        examples = [
            one
            for one in training_memory.examples_for(symbol, market_date)
            if one.feature_schema_version == FEATURE_SCHEMA_V1
            and one.label_schema_version == LABEL_SCHEMA_V1
            and one.outcome is not None
        ]
        return self.predictions.reconcile_examples(examples)
