"""Aureon V4 uncertainty, OOD and weekly learning loop (TODO 071-080).

This module deliberately stops at Candidate creation. Promotion remains an explicit
governance action handled by the lifecycle work in TODO 081+.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from aureon.models.base import to_utc
from aureon.models.ema_journey_v4 import V4RemainingMovementExample
from aureon.services.v4_ema_model import fit_remaining_move_model, predict_remaining_move
from aureon.services.v4_specialist_similarity import (
    V4SimilarJourney,
    retrieve_similar_journeys,
    summarize_similar_outcomes,
)


@dataclass(frozen=True)
class V4PredictionReliability:
    uncertainty: float
    similarity_confidence: float
    confidence: float
    out_of_distribution: bool
    reasons: tuple[str, ...]
    similar_samples: int


def historical_similarity_confidence(
    matches: list[V4SimilarJourney],
    *,
    min_samples: int = 5,
) -> float:
    """TODO 072: confidence from both neighbourhood size and closeness."""
    if not matches:
        return 0.0
    support = min(1.0, len(matches) / max(1, min_samples))
    closeness = mean(match.similarity for match in matches)
    return max(0.0, min(1.0, support * closeness))


def prediction_uncertainty(
    probabilities: dict[str, float],
    *,
    similarity_confidence: float,
) -> float:
    """TODO 071: uncertainty rises near 50/50 and when history is dissimilar."""
    binary = [
        float(value)
        for key, value in probabilities.items()
        if key.startswith("reached_") and 0.0 <= float(value) <= 1.0
    ]
    ambiguity = (
        mean(1.0 - abs(2.0 * probability - 1.0) for probability in binary)
        if binary else 1.0
    )
    history_gap = 1.0 - max(0.0, min(1.0, similarity_confidence))
    return max(0.0, min(1.0, 0.65 * ambiguity + 0.35 * history_gap))


def assess_prediction_reliability(
    query: V4RemainingMovementExample,
    history: list[V4RemainingMovementExample],
    probabilities: dict[str, float],
    *,
    similarity_limit: int = 10,
    min_similar_samples: int = 5,
    min_similarity_confidence: float = 0.20,
    max_uncertainty: float = 0.70,
) -> V4PredictionReliability:
    """TODO 071-073: uncertainty + similarity confidence + explicit OOD reasons."""
    matches = retrieve_similar_journeys(query, history, limit=similarity_limit)
    similarity = historical_similarity_confidence(
        matches,
        min_samples=min_similar_samples,
    )
    uncertainty = prediction_uncertainty(
        probabilities,
        similarity_confidence=similarity,
    )
    reasons: list[str] = []
    if len(matches) < min_similar_samples:
        reasons.append("insufficient_similar_history")
    if similarity < min_similarity_confidence:
        reasons.append("low_historical_similarity")
    if uncertainty > max_uncertainty:
        reasons.append("high_prediction_uncertainty")

    # A regime absent from all retrieved same-symbol history is a stronger OOD signal.
    regime = (
        query.features.base.session,
        query.features.base.session_phase,
        query.features.base.htf_alignment,
        query.features.base.trend_direction,
    )
    historical_regimes = {
        (
            match.outcome.features.base.session,
            match.outcome.features.base.session_phase,
            match.outcome.features.base.htf_alignment,
            match.outcome.features.base.trend_direction,
        )
        for match in matches
    }
    if matches and regime not in historical_regimes:
        reasons.append("unseen_local_regime")

    out_of_distribution = bool(reasons)
    confidence = 0.0 if out_of_distribution else max(
        0.0,
        min(1.0, (1.0 - uncertainty) * similarity),
    )
    return V4PredictionReliability(
        uncertainty=uncertainty,
        similarity_confidence=similarity,
        confidence=confidence,
        out_of_distribution=out_of_distribution,
        reasons=tuple(reasons),
        similar_samples=len(matches),
    )


@dataclass
class V4OutcomeMemory:
    """TODO 074/077: append-only resolved outcome memory, failures included."""

    _rows: dict[str, V4RemainingMovementExample] = field(default_factory=dict)

    @staticmethod
    def key(row: V4RemainingMovementExample) -> str:
        return f"{row.journey_id}:{row.detection_id}"

    def save(self, row: V4RemainingMovementExample) -> bool:
        key = self.key(row)
        if key in self._rows:
            return False
        self._rows[key] = row
        return True

    def all(self) -> list[V4RemainingMovementExample]:
        return list(self._rows.values())

    def since_market_date(self, market_date: str) -> list[V4RemainingMovementExample]:
        return [row for row in self._rows.values() if row.market_date >= market_date]

    def failure_count(self) -> int:
        return sum(not row.labels.reached_3 for row in self._rows.values())


@dataclass(frozen=True)
class V4WeeklyTrainingPolicy:
    cadence_days: int = 7
    minimum_new_examples: int = 20
    emergency_drift_retraining: bool = True


@dataclass(frozen=True)
class V4WeeklyTrainingDecision:
    due: bool
    emergency: bool
    reasons: tuple[str, ...]


def weekly_training_due(
    *,
    last_trained_at: datetime | None,
    now: datetime,
    new_examples: int,
    drift_detected: bool,
    policy: V4WeeklyTrainingPolicy | None = None,
) -> V4WeeklyTrainingDecision:
    """TODO 075/078: weekly cadence with an emergency drift escape hatch."""
    policy = policy or V4WeeklyTrainingPolicy()
    reasons: list[str] = []
    emergency = bool(policy.emergency_drift_retraining and drift_detected)
    if last_trained_at is None:
        reasons.append("no_previous_v4_training")
    elif to_utc(now) - to_utc(last_trained_at) >= timedelta(days=policy.cadence_days):
        reasons.append("weekly_cadence")
    if new_examples >= policy.minimum_new_examples:
        reasons.append("enough_new_examples")
    if emergency:
        reasons.append("emergency_drift")
    return V4WeeklyTrainingDecision(bool(reasons), emergency, tuple(reasons))


@dataclass(frozen=True)
class V4CandidateVersion:
    model_id: str
    version: str
    created_at: datetime
    trained_from: str
    trained_through: str
    samples: int
    failures: int
    artifact: Any
    status: str = "candidate"
    auto_promote: bool = False


def train_weekly_candidate(
    examples: list[V4RemainingMovementExample],
    *,
    created_at: datetime,
    min_samples: int = 20,
) -> V4CandidateVersion:
    """TODO 075-079: versioned weekly Candidate; never auto-promotes."""
    if len(examples) < min_samples:
        raise ValueError(f"need at least {min_samples} V4 examples; have {len(examples)}")
    ordered = sorted(examples, key=lambda row: (row.market_date, row.journey_id, row.detection_id))
    bundle = fit_remaining_move_model(ordered, min_samples=min_samples)
    trained_from = ordered[0].market_date
    trained_through = ordered[-1].market_date
    fingerprint = hashlib.sha256(
        "|".join(f"{row.journey_id}:{row.detection_id}" for row in ordered).encode()
    ).hexdigest()[:12]
    stamp = to_utc(created_at).strftime("%Y%m%dT%H%M%SZ")
    version = f"v4-weekly-{stamp}-{fingerprint}"
    return V4CandidateVersion(
        model_id=f"v4_candidate_{fingerprint}_{stamp}",
        version=version,
        created_at=to_utc(created_at),
        trained_from=trained_from,
        trained_through=trained_through,
        samples=len(ordered),
        failures=sum(not row.labels.reached_3 for row in ordered),
        artifact=bundle,
    )


def weekly_learning_report(
    candidate: V4CandidateVersion,
    examples: list[V4RemainingMovementExample],
    *,
    similarity_limit: int = 10,
) -> dict[str, Any]:
    """TODO 080: auditable weekly report without making a promotion decision."""
    if not examples:
        raise ValueError("weekly report requires examples")
    target_names = ("reached_3", "reached_5", "reached_10", "reached_20", "reached_30", "reached_40")
    rates = {
        target: sum(bool(getattr(row.labels, target)) for row in examples) / len(examples)
        for target in target_names
    }
    predictions = [
        predict_remaining_move(candidate.artifact, row.features)
        for row in examples
    ]
    mean_probabilities = {
        target: mean(pred[target] for pred in predictions)
        for target in target_names
    }

    # Similarity coverage is descriptive; it does not authorize promotion.
    similarity_confidences: list[float] = []
    for row in examples:
        matches = retrieve_similar_journeys(row, examples, limit=similarity_limit)
        similarity_confidences.append(historical_similarity_confidence(matches))

    return {
        "model_id": candidate.model_id,
        "version": candidate.version,
        "status": candidate.status,
        "auto_promote": candidate.auto_promote,
        "trained_from": candidate.trained_from,
        "trained_through": candidate.trained_through,
        "samples": candidate.samples,
        "failed_outcomes_retained": candidate.failures,
        "observed_target_rates": rates,
        "mean_predicted_probabilities": mean_probabilities,
        "mean_similarity_confidence": mean(similarity_confidences),
        "promotion": "requires_explicit_governance",
    }
