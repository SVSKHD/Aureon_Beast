"""Aureon V4 Gold specialist, similarity and evidence intelligence (TODO 061-070).

All helpers are research-only. They consume frozen V4 facts and resolved outcomes;
none of them creates execution requests or bypasses Champion governance.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import mean
from typing import Any

from aureon.ml.logistic import LogisticModel, fit_logistic
from aureon.ml.v1_features import V1FeatureEncoder
from aureon.models.ema_journey_v4 import (
    V4MovementFeatures,
    V4ReentryObservation,
    V4RemainingMovementExample,
)
from aureon.services.v4_ema_model import (
    V4RemainingMoveBundle,
    fit_remaining_move_model,
    predict_remaining_move,
    raw_v4_features,
)


@dataclass(frozen=True)
class V4PullbackSpecialist:
    encoder: V1FeatureEncoder
    continuation_target: dict[str, Any]
    samples: int


@dataclass(frozen=True)
class V4ExhaustionSpecialist:
    encoder: V1FeatureEncoder
    exhaustion_target: dict[str, Any]
    samples: int


@dataclass(frozen=True)
class V4MAESpecialist:
    bundle: V4RemainingMoveBundle


@dataclass(frozen=True)
class V4RegimeRoute:
    key: str
    samples: int
    specialist: V4RemainingMoveBundle | None
    fallback: bool


@dataclass(frozen=True)
class V4JourneyEmbedding:
    journey_id: str
    symbol: str
    direction: str
    numeric: dict[str, float]
    categorical: dict[str, str]


@dataclass(frozen=True)
class V4SimilarJourney:
    journey_id: str
    distance: float
    similarity: float
    outcome: V4RemainingMovementExample


def _fit_binary(vectors: list[list[float]], labels: list[int]) -> dict[str, Any]:
    if not labels:
        raise ValueError("cannot fit empty specialist target")
    if all(label == labels[0] for label in labels):
        return {
            "kind": "constant",
            "probability": (sum(labels) + 1.0) / (len(labels) + 2.0),
        }
    return {"kind": "logistic", "model": fit_logistic(vectors, labels).to_dict()}


def _predict_binary(payload: dict[str, Any], vector: list[float]) -> float:
    if payload["kind"] == "constant":
        return float(payload["probability"])
    return LogisticModel.from_dict(payload["model"]).probability(vector)


def fit_remaining_move_specialist(
    examples: list[V4RemainingMovementExample],
    *,
    min_samples: int = 20,
) -> V4RemainingMoveBundle:
    """TODO 061: dedicated remaining-move specialist.

    V4 already had the underlying remaining-move learner; this makes it an explicit
    specialist contract for routing alongside continuation/pullback/exhaustion.
    """
    return fit_remaining_move_model(examples, min_samples=min_samples)


def _pullback_raw(row: V4ReentryObservation) -> tuple[dict[str, float], dict[str, str]]:
    volume = row.volume
    return (
        {
            "pullback_depth": float(row.pullback_depth),
            "pullback_fraction": float(row.pullback_fraction),
            "atr": float(row.atr or 0.0),
            "structure_intact": float(row.structure_intact),
            "ema_aligned": float(row.ema_aligned),
            "retest_count": float(len(row.retests)),
            "pullback_volume_mean": float(volume.pullback_volume_mean or 0.0),
            "continuation_volume_mean": float(volume.continuation_volume_mean or 0.0),
            "pullback_volume_contracting": float(volume.pullback_volume_contracting),
            "continuation_volume_expanding": float(volume.continuation_volume_expanding),
        },
        {
            "direction": row.direction.value,
            "pullback_class": row.pullback_class.value,
        },
    )


def fit_pullback_specialist(
    rows: list[V4ReentryObservation],
    *,
    min_samples: int = 20,
) -> V4PullbackSpecialist:
    """TODO 062: estimate continuation after an observed pullback/re-entry."""
    resolved = [row for row in rows if row.outcome.completed]
    if len(resolved) < min_samples:
        raise ValueError(f"need at least {min_samples} resolved pullbacks; have {len(resolved)}")
    raw = [_pullback_raw(row) for row in resolved]
    encoder = V1FeatureEncoder.fit(raw)
    vectors = [encoder.transform(*item) for item in raw]
    labels = [1 if row.outcome.reached_3 and not row.outcome.failed else 0 for row in resolved]
    return V4PullbackSpecialist(encoder, _fit_binary(vectors, labels), len(resolved))


def predict_pullback_continuation(
    specialist: V4PullbackSpecialist,
    row: V4ReentryObservation,
) -> float:
    return _predict_binary(
        specialist.continuation_target,
        specialist.encoder.transform(*_pullback_raw(row)),
    )


def fit_exhaustion_specialist(
    examples: list[V4RemainingMovementExample],
    *,
    min_samples: int = 20,
) -> V4ExhaustionSpecialist:
    """TODO 063: probability that less than +3 remains after the anchor."""
    if len(examples) < min_samples:
        raise ValueError(f"need at least {min_samples} exhaustion examples; have {len(examples)}")
    raw = [raw_v4_features(row.features) for row in examples]
    encoder = V1FeatureEncoder.fit(raw)
    vectors = [encoder.transform(*item) for item in raw]
    labels = [1 if not row.labels.reached_3 else 0 for row in examples]
    return V4ExhaustionSpecialist(encoder, _fit_binary(vectors, labels), len(examples))


def predict_exhaustion(
    specialist: V4ExhaustionSpecialist,
    features: V4MovementFeatures,
) -> float:
    return _predict_binary(
        specialist.exhaustion_target,
        specialist.encoder.transform(*raw_v4_features(features)),
    )


def fit_mae_specialist(
    examples: list[V4RemainingMovementExample],
    *,
    min_samples: int = 20,
) -> V4MAESpecialist:
    """TODO 064: expected adverse excursion from the observable anchor."""
    return V4MAESpecialist(fit_remaining_move_model(examples, min_samples=min_samples))


def predict_mae(specialist: V4MAESpecialist, features: V4MovementFeatures) -> float:
    return float(predict_remaining_move(specialist.bundle, features)["expected_remaining_mae"])


def regime_key(features: V4MovementFeatures) -> str:
    """Stable routing cell using only facts frozen at the anchor."""
    base = features.base
    return "|".join(
        (
            base.session or "unknown",
            base.session_phase or "unknown",
            base.htf_alignment or "unknown",
            base.trend_direction or "unknown",
        )
    )


def build_regime_router(
    examples: list[V4RemainingMovementExample],
    *,
    min_samples: int = 20,
) -> dict[str, V4RegimeRoute]:
    """TODO 065: train specialists only for regimes with enough resolved evidence."""
    grouped: dict[str, list[V4RemainingMovementExample]] = {}
    for row in examples:
        grouped.setdefault(regime_key(row.features), []).append(row)
    routes: dict[str, V4RegimeRoute] = {}
    for key, rows in grouped.items():
        enough = len(rows) >= min_samples
        routes[key] = V4RegimeRoute(
            key=key,
            samples=len(rows),
            specialist=fit_remaining_move_model(rows, min_samples=min_samples) if enough else None,
            fallback=not enough,
        )
    return routes


def route_regime(
    routes: dict[str, V4RegimeRoute],
    features: V4MovementFeatures,
) -> V4RegimeRoute | None:
    return routes.get(regime_key(features))


def journey_embedding(row: V4RemainingMovementExample) -> V4JourneyEmbedding:
    """TODO 066: deterministic embedding made only from frozen observable features."""
    numeric, categorical = raw_v4_features(row.features)
    return V4JourneyEmbedding(
        journey_id=row.journey_id,
        symbol=row.symbol,
        direction=row.direction.value,
        numeric=numeric,
        categorical=categorical,
    )


def _distance(
    query: V4JourneyEmbedding,
    candidate: V4JourneyEmbedding,
    scales: dict[str, float],
) -> float:
    keys = set(query.numeric) | set(candidate.numeric)
    numeric_sq = sum(
        ((query.numeric.get(key, 0.0) - candidate.numeric.get(key, 0.0)) / scales.get(key, 1.0)) ** 2
        for key in keys
    )
    categorical_keys = set(query.categorical) | set(candidate.categorical)
    categorical_penalty = sum(
        1.0
        for key in categorical_keys
        if query.categorical.get(key) != candidate.categorical.get(key)
    )
    if query.symbol != candidate.symbol:
        categorical_penalty += 2.0
    if query.direction != candidate.direction:
        categorical_penalty += 2.0
    return sqrt(numeric_sq + categorical_penalty)


def retrieve_similar_journeys(
    query: V4RemainingMovementExample,
    history: list[V4RemainingMovementExample],
    *,
    limit: int = 10,
    same_symbol: bool = True,
) -> list[V4SimilarJourney]:
    """TODO 067: nearest resolved historical journeys, excluding the query itself."""
    if limit <= 0:
        return []
    candidates = [
        row for row in history
        if row.journey_id != query.journey_id and (not same_symbol or row.symbol == query.symbol)
    ]
    if not candidates:
        return []
    embeddings = [journey_embedding(row) for row in candidates]
    q = journey_embedding(query)
    numeric_keys = set(q.numeric)
    for emb in embeddings:
        numeric_keys.update(emb.numeric)
    scales: dict[str, float] = {}
    for key in numeric_keys:
        values = [q.numeric.get(key, 0.0), *[emb.numeric.get(key, 0.0) for emb in embeddings]]
        spread = max(values) - min(values)
        scales[key] = spread if spread > 1e-9 else 1.0

    ranked = sorted(
        (
            V4SimilarJourney(
                journey_id=row.journey_id,
                distance=(distance := _distance(q, emb, scales)),
                similarity=1.0 / (1.0 + distance),
                outcome=row,
            )
            for row, emb in zip(candidates, embeddings, strict=True)
        ),
        key=lambda item: (item.distance, item.journey_id),
    )
    return ranked[:limit]


def summarize_similar_outcomes(
    matches: list[V4SimilarJourney],
) -> dict[str, float | int]:
    """TODO 068: compact outcome evidence from retrieved historical journeys."""
    if not matches:
        return {"samples": 0}
    rows = [match.outcome for match in matches]
    n = len(rows)
    return {
        "samples": n,
        "mean_similarity": mean(match.similarity for match in matches),
        "reach_3_rate": sum(row.labels.reached_3 for row in rows) / n,
        "reach_5_rate": sum(row.labels.reached_5 for row in rows) / n,
        "reach_10_rate": sum(row.labels.reached_10 for row in rows) / n,
        "mean_remaining_mfe": mean(row.labels.remaining_mfe for row in rows),
        "mean_remaining_mae": mean(row.labels.remaining_mae for row in rows),
    }


def explain_model_evidence(
    query: V4RemainingMovementExample,
    matches: list[V4SimilarJourney],
) -> dict[str, list[str]]:
    """TODO 069-070: factual positive/negative evidence, not causal claims."""
    positives: list[str] = []
    negatives: list[str] = []
    summary = summarize_similar_outcomes(matches)
    n = int(summary.get("samples", 0))
    if n:
        hit5 = float(summary["reach_5_rate"])
        hit10 = float(summary["reach_10_rate"])
        if hit5 >= 0.5:
            positives.append(f"similar journeys reached +5 in {hit5:.0%} of {n} resolved cases")
        else:
            negatives.append(f"similar journeys reached +5 in only {hit5:.0%} of {n} resolved cases")
        if hit10 >= 0.5:
            positives.append(f"similar journeys reached +10 in {hit10:.0%} of {n} resolved cases")
        else:
            negatives.append(f"similar journeys reached +10 in only {hit10:.0%} of {n} resolved cases")

    base = query.features.base
    if base.htf_alignment == "aligned":
        positives.append("M15/H1 context is aligned at this anchor")
    elif base.htf_alignment and base.htf_alignment not in {"unknown", "mixed"}:
        negatives.append(f"HTF alignment is {base.htf_alignment} at this anchor")

    if query.features.movement_consumed >= 10.0:
        negatives.append("substantial movement was already consumed before this anchor")
    elif query.features.movement_consumed <= 3.0:
        positives.append("limited movement was consumed before this anchor")

    return {"positive": positives, "negative": negatives}
