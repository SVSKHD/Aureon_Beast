"""Research models for Aureon V4 pre-cross and remaining-move intelligence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aureon.ml.logistic import LogisticModel, fit_logistic
from aureon.ml.v1_features import V1FeatureEncoder
from aureon.models.ema_journey_v4 import (
    V4MovementFeatures,
    V4PreCrossExample,
    V4RemainingMovementExample,
)
from aureon.services.v3_ema_model import raw_v3_features

_PRE_TARGETS = ("cross_within_1", "cross_within_3", "cross_within_6", "cross_within_12")
_REMAINING_TARGETS = ("reached_3", "reached_5", "reached_10", "reached_20", "reached_30", "reached_40")


def raw_v4_features(features: V4MovementFeatures) -> tuple[dict[str, float], dict[str, str]]:
    numeric, categorical = raw_v3_features(features.base)
    numeric = {
        **numeric,
        "movement_consumed": float(features.movement_consumed),
        "pre_cross_movement": float(features.pre_cross_movement),
        "pre_cross_bars": float(features.pre_cross_bars),
        "pre_cross_seconds": float(features.pre_cross_seconds),
    }
    categorical = {
        **categorical,
        "v4_anchor_type": features.anchor_type,
        "v4_cross_order": features.cross_order,
    }
    return numeric, categorical


def _constant(labels: list[int]) -> dict[str, Any]:
    return {"kind": "constant", "probability": (sum(labels) + 1.0) / (len(labels) + 2.0)}


def _fit_binary(vectors: list[list[float]], labels: list[int]) -> dict[str, Any]:
    if not labels:
        raise ValueError("cannot fit an empty V4 target")
    if all(v == labels[0] for v in labels):
        return _constant(labels)
    return {"kind": "logistic", "model": fit_logistic(vectors, labels).to_dict()}


def _predict_binary(payload: dict[str, Any], vector: list[float]) -> float:
    if payload["kind"] == "constant":
        return float(payload["probability"])
    return LogisticModel.from_dict(payload["model"]).probability(vector)


def _fit_linear(vectors: list[list[float]], values: list[float]) -> dict[str, Any]:
    if not vectors or len(vectors) != len(values):
        raise ValueError("linear target requires aligned rows")
    width = len(vectors[0])
    weights = [0.0] * width
    bias = sum(values) / len(values)
    lr, l2 = 0.01, 0.001
    for _ in range(300):
        grad_w = [0.0] * width
        grad_b = 0.0
        for vector, value in zip(vectors, values, strict=True):
            prediction = bias + sum(w * x for w, x in zip(weights, vector, strict=True))
            error = prediction - value
            grad_b += error
            for index, value_x in enumerate(vector):
                grad_w[index] += error * value_x
        count = float(len(values))
        bias -= lr * grad_b / count
        for index in range(width):
            weights[index] -= lr * (grad_w[index] / count + l2 * weights[index])
    return {"weights": weights, "bias": bias}


def _predict_linear(payload: dict[str, Any], vector: list[float]) -> float:
    return max(
        0.0,
        float(payload["bias"])
        + sum(float(w) * x for w, x in zip(payload["weights"], vector, strict=True)),
    )


@dataclass(frozen=True)
class V4PreCrossBundle:
    encoder: V1FeatureEncoder
    targets: dict[str, dict[str, Any]]
    samples: int


@dataclass(frozen=True)
class V4RemainingMoveBundle:
    encoder: V1FeatureEncoder
    targets: dict[str, dict[str, Any]]
    mfe_model: dict[str, Any]
    mae_model: dict[str, Any]
    samples: int


def fit_pre_cross_model(
    examples: list[V4PreCrossExample],
    *,
    min_samples: int = 20,
) -> V4PreCrossBundle:
    if len(examples) < min_samples:
        raise ValueError(f"need at least {min_samples} pre-cross examples; have {len(examples)}")
    raw = [raw_v4_features(row.features) for row in examples]
    encoder = V1FeatureEncoder.fit(raw)
    vectors = [encoder.transform(*row) for row in raw]
    targets = {
        name: _fit_binary(
            vectors,
            [1 if bool(getattr(row.labels, name)) else 0 for row in examples],
        )
        for name in _PRE_TARGETS
    }
    return V4PreCrossBundle(encoder=encoder, targets=targets, samples=len(examples))


def predict_pre_cross(
    bundle: V4PreCrossBundle,
    features: V4MovementFeatures,
) -> dict[str, float]:
    vector = bundle.encoder.transform(*raw_v4_features(features))
    probabilities = {
        name: _predict_binary(payload, vector)
        for name, payload in bundle.targets.items()
    }
    # P(cross within N) must be non-decreasing as N grows.
    previous = 0.0
    for name in _PRE_TARGETS:
        probabilities[name] = max(previous, probabilities[name])
        previous = probabilities[name]
    return probabilities


def fit_remaining_move_model(
    examples: list[V4RemainingMovementExample],
    *,
    min_samples: int = 20,
) -> V4RemainingMoveBundle:
    if len(examples) < min_samples:
        raise ValueError(f"need at least {min_samples} remaining-move examples; have {len(examples)}")
    raw = [raw_v4_features(row.features) for row in examples]
    encoder = V1FeatureEncoder.fit(raw)
    vectors = [encoder.transform(*row) for row in raw]
    targets = {
        name: _fit_binary(
            vectors,
            [1 if bool(getattr(row.labels, name)) else 0 for row in examples],
        )
        for name in _REMAINING_TARGETS
    }
    return V4RemainingMoveBundle(
        encoder=encoder,
        targets=targets,
        mfe_model=_fit_linear(vectors, [row.labels.remaining_mfe for row in examples]),
        mae_model=_fit_linear(vectors, [row.labels.remaining_mae for row in examples]),
        samples=len(examples),
    )


def predict_remaining_move(
    bundle: V4RemainingMoveBundle,
    features: V4MovementFeatures,
) -> dict[str, float]:
    vector = bundle.encoder.transform(*raw_v4_features(features))
    probabilities = {
        name: _predict_binary(payload, vector)
        for name, payload in bundle.targets.items()
    }
    # Larger targets cannot be more probable than smaller targets.
    previous = 1.0
    for name in _REMAINING_TARGETS:
        probabilities[name] = min(previous, probabilities[name])
        previous = probabilities[name]
    return {
        **probabilities,
        "expected_remaining_mfe": _predict_linear(bundle.mfe_model, vector),
        "expected_remaining_mae": _predict_linear(bundle.mae_model, vector),
    }
