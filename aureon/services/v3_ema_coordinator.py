"""Live coordination for Aureon V3 EMA learning and confidence.

This layer:
- writes canonical examples only after outcomes close;
- reconciles earlier predictions, including failures;
- freezes next-day holdouts;
- records model confidence before outcome candles arrive.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.ema_journey_v3 import (
    EMA_FEATURE_SCHEMA_V3,
    EMA_LABEL_SCHEMA_V3,
    EMA_MODEL_SCHEMA_V3,
    EMAJourneyAnchor,
    EMAMovementJourney,
)
from aureon.services.v3_ema_learning import (
    EMAHoldoutDayV3,
    canonical_example,
)
from aureon.services.v3_ema_model import predict_v3


class V3EMALearningCoordinator:
    def __init__(
        self,
        *,
        learning: Any,
        models: Any,
        now: Any = utc_now,
        min_model_samples: int = 30,
    ) -> None:
        self.learning = learning
        self.models = models
        self._now = now
        self.min_model_samples = min_model_samples

    def predict_anchor(
        self,
        journey: EMAMovementJourney,
        anchor: EMAJourneyAnchor,
    ) -> Any | None:
        """Predict once, immediately after the anchor snapshot is frozen."""
        holdout = self.learning.holdout_for(
            journey.symbol,
            journey.market_date,
        )
        model = None
        if holdout is not None and holdout.status == "open" and holdout.frozen_model_id:
            candidate = self.models.get_model(holdout.frozen_model_id)
            if (
                candidate is not None
                and candidate.feature_schema_version == EMA_FEATURE_SCHEMA_V3
                and candidate.label_schema_version == EMA_LABEL_SCHEMA_V3
                and candidate.model_schema_version == EMA_MODEL_SCHEMA_V3
            ):
                model = candidate
        if model is None:
            model = self.models.champion_for_contract(
                journey.symbol,
                feature_schema=EMA_FEATURE_SCHEMA_V3,
                label_schema=EMA_LABEL_SCHEMA_V3,
                model_schema=EMA_MODEL_SCHEMA_V3,
            )
        if model is None:
            return None
        existing = self.learning.prediction_for_detection(
            anchor.detection_id,
            model_id=model.model_id,
        )
        if existing is not None:
            return existing["payload"]

        confidence = predict_v3(
            model,
            anchor.features,
            min_samples=self.min_model_samples,
            direction=anchor.direction.value,
            min_cell_samples=20,
        )
        prediction_id = hashlib.sha256(
            f"{model.model_id}|{anchor.detection_id}".encode()
        ).hexdigest()
        self.learning.write_prediction(
            prediction_id=prediction_id,
            model_id=model.model_id,
            journey_id=journey.journey_id,
            detection_id=anchor.detection_id,
            symbol=journey.symbol,
            market_date=journey.market_date,
            predicted_at=anchor.detected_at,
            payload=confidence.model_dump(mode="json"),
        )
        return confidence

    def resolve_journey(self, journey: EMAMovementJourney) -> list[Any]:
        """Write both successes and failures as immutable canonical examples."""
        written = []
        when = to_utc(journey.ended_at or self._now())
        if str(journey.status.value) == "invalid":
            return written

        for anchor in journey.anchors:
            if not anchor.outcome.completed:
                continue
            existing = self.learning.example_for_detection(anchor.detection_id)
            if existing is None:
                example = canonical_example(journey, anchor, generated_at=when)
                self.learning.write_example(example)
            else:
                example = existing
            written.append(example)

            actual = example.outcome.model_dump(mode="json")
            for prediction in self.learning.unreconciled_predictions(
                anchor.detection_id
            ):
                self.learning.reconcile_prediction(
                    prediction["prediction_id"],
                    outcome=actual,
                    at=when,
                )
        return written

    def freeze_next_day(
        self,
        symbol: str,
        *,
        after_market_date: str,
    ) -> EMAHoldoutDayV3:
        """Create the next UTC-calendar date as a frozen unseen-day gate.

        Market-day construction remains broker-time responsibility upstream; this
        helper is deterministic for an already-normalized YYYY-MM-DD market_date.
        """
        next_day = (
            datetime.fromisoformat(after_market_date)
            .replace(tzinfo=UTC)
            + timedelta(days=1)
        )
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        next_date = next_day.date().isoformat()
        model = self.models.champion_for_contract(
            symbol,
            feature_schema=EMA_FEATURE_SCHEMA_V3,
            label_schema=EMA_LABEL_SCHEMA_V3,
            model_schema=EMA_MODEL_SCHEMA_V3,
        )
        holdout_id = hashlib.sha256(
            f"{symbol.upper()}|{next_date}|V3_EMA_HOLDOUT".encode()
        ).hexdigest()
        holdout = EMAHoldoutDayV3(
            holdout_id=holdout_id,
            symbol=symbol.upper(),
            market_date=next_date,
            frozen_model_id=model.model_id if model else None,
            status="open",
            created_at=to_utc(self._now()),
        )
        return self.learning.write_holdout(holdout)
