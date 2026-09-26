"""Tiny dependency-free gradient-boosted decision-stump classifier."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


def _sigmoid(value: float) -> float:
    value = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


@dataclass(frozen=True)
class Stump:
    feature: int
    threshold: float
    left: float
    right: float

    def value(self, vector: list[float]) -> float:
        return self.left if vector[self.feature] <= self.threshold else self.right


@dataclass(frozen=True)
class BoostedStumpModel:
    bias: float
    learning_rate: float
    stumps: tuple[Stump, ...]

    def probability(self, vector: Iterable[float]) -> float:
        values = list(vector)
        score = self.bias
        for stump in self.stumps:
            score += self.learning_rate * stump.value(values)
        return _sigmoid(score)

    def to_dict(self) -> dict:
        return {
            "bias": self.bias,
            "learning_rate": self.learning_rate,
            "stumps": [
                {
                    "feature": stump.feature,
                    "threshold": stump.threshold,
                    "left": stump.left,
                    "right": stump.right,
                }
                for stump in self.stumps
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "BoostedStumpModel":
        return cls(
            bias=float(payload["bias"]),
            learning_rate=float(payload["learning_rate"]),
            stumps=tuple(
                Stump(
                    feature=int(item["feature"]),
                    threshold=float(item["threshold"]),
                    left=float(item["left"]),
                    right=float(item["right"]),
                )
                for item in payload.get("stumps", [])
            ),
        )


def fit_boosted_stumps(
    vectors: list[list[float]],
    labels: list[int],
    *,
    rounds: int = 40,
    learning_rate: float = 0.12,
    min_leaf: int = 3,
) -> BoostedStumpModel:
    if not vectors or len(vectors) != len(labels):
        raise ValueError("vectors and labels must be non-empty and aligned")
    width = len(vectors[0])
    if any(len(vector) != width for vector in vectors):
        raise ValueError("all vectors must have the same width")

    positives = sum(labels)
    negatives = len(labels) - positives
    bias = math.log((positives + 1.0) / (negatives + 1.0))
    scores = [bias] * len(labels)
    stumps: list[Stump] = []

    for _ in range(rounds):
        residuals = [
            float(label) - _sigmoid(score)
            for label, score in zip(labels, scores, strict=True)
        ]
        best: tuple[float, Stump] | None = None
        for feature in range(width):
            values = sorted({row[feature] for row in vectors})
            if len(values) < 2:
                continue
            thresholds = [
                (values[i] + values[i + 1]) / 2.0
                for i in range(len(values) - 1)
            ]
            if len(thresholds) > 24:
                step = max(1, len(thresholds) // 24)
                thresholds = thresholds[::step][:24]
            for threshold in thresholds:
                left_idx = [i for i, row in enumerate(vectors) if row[feature] <= threshold]
                right_idx = [i for i, row in enumerate(vectors) if row[feature] > threshold]
                if len(left_idx) < min_leaf or len(right_idx) < min_leaf:
                    continue
                left_value = sum(residuals[i] for i in left_idx) / len(left_idx)
                right_value = sum(residuals[i] for i in right_idx) / len(right_idx)
                error = sum(
                    (
                        residuals[i]
                        - (left_value if vectors[i][feature] <= threshold else right_value)
                    ) ** 2
                    for i in range(len(vectors))
                )
                stump = Stump(feature, threshold, left_value, right_value)
                if best is None or error < best[0]:
                    best = (error, stump)
        if best is None:
            break
        stump = best[1]
        stumps.append(stump)
        for i, vector in enumerate(vectors):
            scores[i] += learning_rate * stump.value(vector)

    return BoostedStumpModel(
        bias=bias,
        learning_rate=learning_rate,
        stumps=tuple(stumps),
    )
