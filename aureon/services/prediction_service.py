"""Schema-checked Champion/Shadow inference boundary for Aureon V1.

Predictions are intelligence only. This service cannot create trade requests or call a
broker. It returns a stable ``EntryIntelligence`` and records the prediction BEFORE any
outcome is known, so the later score is an honest score of what was actually said.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.learning_v1 import (
    FEATURE_SCHEMA_V1,
    LABEL_SCHEMA_V1,
    MODEL_SCHEMA_V1,
    CoverageStatus,
    EntryDecision,
    EntryIntelligence,
)
from aureon.models.ml import ModelPrediction, ModelRegistryEntry
from aureon.services.learning_contract import FeatureBuilder
from aureon.services.training_coverage import assess_coverage
from aureon.services.v1_model_training import predict_v1_artifact

log = logging.getLogger(__name__)

#: Reads ``validation_metrics["training_coverage"]`` off the model that predicts.
COVERAGE_KEY = "training_coverage"


class PredictionService:
    """One fail-closed inference path shared by Champion and Shadow models.

    ``position_provider(symbol) -> bool`` answers "does Aureon already own an active
    position on this symbol?". When it does, the Champion still analyses the setup and
    records it, but the decision is HOLD_EXISTING_POSITION and a would-have-entered setup
    is flagged ``addon_opportunity`` for research. V1 never pyramids.
    """

    def __init__(
        self,
        models: Any,
        *,
        now: Any = utc_now,
        clean_threshold: float = 0.55,
        wait_threshold: float = 0.45,
        target_threshold: float = 0.50,
        position_provider: Callable[[str], bool] | None = None,
    ) -> None:
        if not 0 <= wait_threshold <= clean_threshold <= 1:
            raise ValueError("thresholds must satisfy 0 <= wait <= clean <= 1")
        self.models = models
        self._now = now
        self.clean_threshold = clean_threshold
        self.wait_threshold = wait_threshold
        self.target_threshold = target_threshold
        self.position_provider = position_provider

    def predict_champion(
        self,
        setup: Any,
        event: Any,
        *,
        features: Any | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> EntryIntelligence | None:
        model = self.models.champion(setup.symbol)
        if model is None:
            log.warning("%s has no Champion; ML intelligence unavailable", setup.symbol)
            return None
        return self._predict(
            model,
            setup,
            event,
            role="champion",
            features=features,
            extra_context=extra_context,
        )

    def predict_shadow(
        self,
        setup: Any,
        event: Any,
        *,
        features: Any | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> EntryIntelligence | None:
        model = self.models.active_shadow(setup.symbol)
        if model is None:
            return None
        return self._predict(
            model,
            setup,
            event,
            role="shadow",
            features=features,
            extra_context=extra_context,
        )

    def _predict(
        self,
        model: ModelRegistryEntry,
        setup: Any,
        event: Any,
        *,
        role: str,
        features: Any | None = None,
        extra_context: dict[str, Any] | None = None,
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
            frozen = features or FeatureBuilder.from_setup_event(
                setup,
                event,
                extra_context=extra_context,
            )
            probabilities = predict_v1_artifact(model, frozen)
        except Exception:
            log.exception("%s prediction failed closed for model %s", role, model.model_id)
            return None

        coverage = assess_coverage(
            (model.validation_metrics or {}).get(COVERAGE_KEY), frozen
        )
        intelligence = self._intelligence(
            model, setup, event, frozen, probabilities, coverage=coverage
        )

        existing = self.models.prediction_for(model.model_id, setup.setup_id)
        if existing is None:
            prediction_id = hashlib.sha256(
                f"{model.model_id}|{setup.setup_id}|{event.event_id}".encode()
            ).hexdigest()
            prediction = ModelPrediction(
                prediction_id=prediction_id,
                model_id=model.model_id,
                setup_id=setup.setup_id,
                event_id=event.event_id,
                symbol=setup.symbol,
                timeframe=setup.timeframe,
                predicted_at=intelligence.predicted_at,
                feature_schema_version=model.feature_schema_version,
                label_schema_version=model.label_schema_version,
                probabilities=probabilities,
                feature_snapshot=frozen.model_dump(mode="json"),
                decision=intelligence.decision.value,
            )
            self.models.write_prediction(prediction)
        return intelligence

    def _intelligence(
        self,
        model: ModelRegistryEntry,
        setup: Any,
        event: Any,
        frozen: Any,
        probabilities: dict[str, float],
        *,
        coverage: Any,
    ) -> EntryIntelligence:
        clean = probabilities.get("clean_10")
        recommended_target = 0
        for target in (5, 10, 20, 30, 40):
            probability = probabilities.get(f"reach_{target}")
            if probability is not None and probability >= self.target_threshold:
                recommended_target = target

        would_enter = clean is not None and clean >= self.clean_threshold
        if clean is None:
            decision = EntryDecision.WAIT
            reason = "Champion did not emit P(clean_10); no entry intelligence"
        elif coverage.status is CoverageStatus.OUT_OF_DISTRIBUTION:
            decision = EntryDecision.WAIT
            reason = (
                f"P(clean_10)={clean:.3f} but context is out of distribution "
                f"({coverage.reason}); waiting for a represented context"
            )
        elif would_enter:
            decision = EntryDecision.ENTER
            reason = (
                f"P(clean_10)={clean:.3f} >= {self.clean_threshold:.3f}; "
                "ExecutionGuard and deterministic risk still decide execution"
            )
            if coverage.status is CoverageStatus.LOW_TRAINING_COVERAGE:
                reason += f" (low training coverage: {coverage.reason})"
        elif clean >= self.wait_threshold:
            decision = EntryDecision.WAIT
            reason = (
                f"P(clean_10)={clean:.3f} between wait {self.wait_threshold:.3f} and "
                f"enter {self.clean_threshold:.3f}"
            )
        else:
            decision = EntryDecision.REJECT
            reason = f"P(clean_10)={clean:.3f} < {self.wait_threshold:.3f}"

        addon = False
        if self.position_provider is not None:
            try:
                holding = bool(self.position_provider(setup.symbol))
            except Exception:  # noqa: BLE001 - an unreadable position state fails closed
                log.exception("position provider failed for %s; holding", setup.symbol)
                holding = True
            if holding:
                addon = decision is EntryDecision.ENTER
                decision = EntryDecision.HOLD_EXISTING_POSITION
                reason = (
                    "Aureon already owns an active position on this symbol; V1 allows one "
                    f"autonomous position (would otherwise be {'ENTER' if addon else 'no entry'})"
                )

        supporting = tuple(
            sorted(name for name, state in frozen.agents.items() if state.alignment == "aligned")
        )
        opposing = tuple(
            sorted(name for name, state in frozen.agents.items() if state.alignment == "opposed")
        )
        return EntryIntelligence(
            model_id=model.model_id,
            generation=int(getattr(model, "generation", 0) or 0),
            setup_id=setup.setup_id,
            symbol=setup.symbol,
            timeframe=setup.timeframe,
            predicted_at=to_utc(event.market_time.utc),
            direction=frozen.direction,
            confidence=clean,
            probability_clean_10=clean,
            probability_reach_5=probabilities.get("reach_5"),
            probability_reach_10=probabilities.get("reach_10"),
            probability_reach_20=probabilities.get("reach_20"),
            probability_reach_30=probabilities.get("reach_30"),
            probability_reach_40=probabilities.get("reach_40"),
            probabilities=probabilities,
            recommended_target=recommended_target,
            supporting_agents=supporting,
            opposing_agents=opposing,
            daily_bias=frozen.daily_bias_at_entry,
            daily_bias_strength=frozen.daily_bias_strength_at_entry,
            session=frozen.current_session or frozen.session,
            session_bias=frozen.session_bias_at_entry,
            session_strength=frozen.session_bias_strength_at_entry,
            market_regime=frozen.market_regime,
            volatility_regime=frozen.volatility_regime,
            htf_alignment=frozen.htf_alignment,
            training_coverage=coverage,
            decision=decision,
            reason=reason,
            addon_opportunity=addon,
            would_enter=would_enter,
        )
