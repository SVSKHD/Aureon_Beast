"""Small dependency-free logistic regression used as Aureon's baseline model."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


def _sigmoid(value: float) -> float:
    value = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


@dataclass(frozen=True)
class LogisticModel:
    weights: tuple[float, ...]
    bias: float

    def probability(self, vector: Iterable[float]) -> float:
        score = self.bias + sum(w * x for w, x in zip(self.weights, vector, strict=True))
        return _sigmoid(score)

    def to_dict(self) -> dict:
        return {"weights": list(self.weights), "bias": self.bias}

    @classmethod
    def from_dict(cls, payload: dict) -> "LogisticModel":
        return cls(
            weights=tuple(float(value) for value in payload["weights"]),
            bias=float(payload["bias"]),
        )


def fit_logistic(
    vectors: list[list[float]],
    labels: list[int],
    *,
    iterations: int = 500,
    learning_rate: float = 0.05,
    l2: float = 0.001,
) -> LogisticModel:
    if not vectors or len(vectors) != len(labels):
        raise ValueError("vectors and labels must be non-empty and aligned")
    width = len(vectors[0])
    if any(len(vector) != width for vector in vectors):
        raise ValueError("all vectors must have the same width")

    weights = [0.0] * width
    positives = sum(labels)
    negatives = len(labels) - positives
    bias = math.log((positives + 1.0) / (negatives + 1.0))

    for _ in range(iterations):
        grad_w = [0.0] * width
        grad_b = 0.0
        for vector, label in zip(vectors, labels, strict=True):
            score = bias + sum(w * x for w, x in zip(weights, vector, strict=True))
            error = _sigmoid(score) - label
            grad_b += error
            for index, value in enumerate(vector):
                grad_w[index] += error * value
        size = float(len(labels))
        bias -= learning_rate * grad_b / size
        for index in range(width):
            gradient = grad_w[index] / size + l2 * weights[index]
            weights[index] -= learning_rate * gradient

    return LogisticModel(weights=tuple(weights), bias=bias)


def binary_metrics(labels: list[int], probabilities: list[float]) -> dict[str, float | int | None]:
    if not labels or len(labels) != len(probabilities):
        return {
            "samples": 0,
            "positives": 0,
            "negatives": 0,
            "accuracy": None,
            "precision": None,
            "recall": None,
            "brier": None,
            "log_loss": None,
            "roc_auc": None,
        }

    predictions = [1 if probability >= 0.5 else 0 for probability in probabilities]
    tp = sum(p == 1 and y == 1 for p, y in zip(predictions, labels, strict=True))
    tn = sum(p == 0 and y == 0 for p, y in zip(predictions, labels, strict=True))
    fp = sum(p == 1 and y == 0 for p, y in zip(predictions, labels, strict=True))
    fn = sum(p == 0 and y == 1 for p, y in zip(predictions, labels, strict=True))

    clipped = [max(1e-9, min(1.0 - 1e-9, value)) for value in probabilities]
    brier = sum((p - y) ** 2 for p, y in zip(clipped, labels, strict=True)) / len(labels)
    log_loss = -sum(
        y * math.log(p) + (1 - y) * math.log(1 - p)
        for p, y in zip(clipped, labels, strict=True)
    ) / len(labels)

    return {
        "samples": len(labels),
        "positives": sum(labels),
        "negatives": len(labels) - sum(labels),
        "accuracy": (tp + tn) / len(labels),
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "brier": brier,
        "log_loss": log_loss,
        "roc_auc": _roc_auc(labels, probabilities),
    }


def _roc_auc(labels: list[int], probabilities: list[float]) -> float | None:
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None

    ranked = sorted(zip(probabilities, labels, strict=True), key=lambda item: item[0])
    rank_sum = 0.0
    index = 0
    while index < len(ranked):
        end = index + 1
        while end < len(ranked) and ranked[end][0] == ranked[index][0]:
            end += 1
        average_rank = (index + 1 + end) / 2.0
        rank_sum += average_rank * sum(label for _, label in ranked[index:end])
        index = end

    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)
