"""Canonical V1 entry-model training.

This is additive to the legacy +6 trainer. It consumes only canonical
AUREON_FEATURES_V1/AUREON_CLEAN_MOVE_V1 examples and trains interpretable
logistic and boosted-stump candidates without changing the Champion.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable

from aureon.ml.boosted_stumps import BoostedStumpModel, fit_boosted_stumps
from aureon.ml.logistic import LogisticModel, binary_metrics, fit_logistic
from aureon.ml.v1_features import V1FeatureEncoder, raw_v1_features
from aureon.models.base import to_utc, utc_now
from aureon.models.learning_v1 import (
    CanonicalTrainingExample,
    FEATURE_SCHEMA_V1,
    LABEL_SCHEMA_V1,
    MODEL_SCHEMA_V1,
    ModelLifecycleStatus,
)
from aureon.models.ml import ModelRegistryEntry, TargetMetrics

V1_TARGETS = ("clean_10", "reach_5", "reach_10", "reach_20", "reach_30", "reach_40")


def target_value(example: CanonicalTrainingExample, target: str) -> bool:
    outcome = example.outcome
    mapping = {
        "clean_10": outcome.clean_10,
        "reach_5": outcome.reached_5,
        "reach_10": outcome.reached_10,
        "reach_20": outcome.reached_20,
        "reach_30": outcome.reached_30,
        "reach_40": outcome.reached_40,
    }
    if target not in mapping:
        raise KeyError(target)
    return bool(mapping[target])


def _constant_probability(labels: list[int]) -> float:
    return (sum(labels) + 1.0) / (len(labels) + 2.0)


def _metrics(
    labels: list[int],
    probabilities: list[float],
    examples: list[CanonicalTrainingExample],
) -> TargetMetrics:
    data = binary_metrics(labels, probabilities)
    positives = [
        example
        for example, label in zip(examples, labels, strict=True)
        if label == 1
    ]
    data["average_mae"] = (
        sum(example.outcome.max_adverse_move for example in positives) / len(positives)
        if positives
        else None
    )
    data["average_mfe"] = (
        sum(example.outcome.max_favourable_move for example in positives) / len(positives)
        if positives
        else None
    )
    return TargetMetrics.model_validate(data)


def _segmented_clean_metrics(
    examples: list[CanonicalTrainingExample],
    probabilities: list[float],
) -> dict[str, Any]:
    groups: dict[str, dict[str, list[int | float]]] = defaultdict(
        lambda: {"labels": [], "probabilities": []}
    )
    for example, probability in zip(examples, probabilities, strict=True):
        label = 1 if example.outcome.clean_10 else 0
        features = example.features
        dimensions = {
            "session": features.session or "unknown",
            "volatility_regime": features.volatility_regime or "unknown",
            "market_regime": features.market_regime or "unknown",
            "direction": features.direction.value,
            "timeframe": features.timeframe.value,
        }
        for dimension, value in dimensions.items():
            key = f"{dimension}:{value}"
            groups[key]["labels"].append(label)
            groups[key]["probabilities"].append(float(probability))
    return {
        key: binary_metrics(
            [int(v) for v in payload["labels"]],
            [float(v) for v in payload["probabilities"]],
        )
        for key, payload in groups.items()
    }


@dataclass(frozen=True)
class V1Bundle:
    algorithm: str
    encoder: V1FeatureEncoder
    targets: dict[str, dict[str, Any]]
    metrics: dict[str, TargetMetrics]
    validation_metrics: dict[str, Any]

    def artifact(self) -> dict[str, Any]:
        return {
            "model_schema_version": MODEL_SCHEMA_V1,
            "feature_schema_version": FEATURE_SCHEMA_V1,
            "label_schema_version": LABEL_SCHEMA_V1,
            "algorithm": self.algorithm,
            "encoder": self.encoder.to_dict(),
            "feature_names": list(self.encoder.feature_names),
            "targets": self.targets,
            "target_order": list(V1_TARGETS),
        }


def _fit_target(
    *,
    algorithm: str,
    train_vectors: list[list[float]],
    train_labels: list[int],
) -> dict[str, Any]:
    if not train_labels:
        raise ValueError("target has no labels")
    if not any(train_labels) or all(train_labels):
        return {
            "kind": "constant",
            "probability": _constant_probability(train_labels),
        }
    if algorithm == "logistic_regression_v1":
        model = fit_logistic(train_vectors, train_labels)
        return {"kind": "logistic", "model": model.to_dict()}
    if algorithm == "boosted_stumps_v1":
        model = fit_boosted_stumps(train_vectors, train_labels)
        return {"kind": "boosted_stumps", "model": model.to_dict()}
    raise ValueError(f"unsupported V1 algorithm {algorithm!r}")


def _predict_target(payload: dict[str, Any], vector: list[float]) -> float:
    kind = payload.get("kind")
    if kind == "constant":
        return float(payload["probability"])
    if kind == "logistic":
        return LogisticModel.from_dict(payload["model"]).probability(vector)
    if kind == "boosted_stumps":
        return BoostedStumpModel.from_dict(payload["model"]).probability(vector)
    raise ValueError(f"unsupported target model kind {kind!r}")


def fit_v1_bundle(
    examples: list[CanonicalTrainingExample],
    *,
    algorithm: str,
    train_fraction: float = 0.8,
    min_samples: int = 30,
) -> V1Bundle:
    usable = [
        example
        for example in examples
        if example.feature_schema == FEATURE_SCHEMA_V1
        and example.label_schema == LABEL_SCHEMA_V1
    ]
    usable.sort(key=lambda one: (one.features.timestamp, one.setup_id))
    if len(usable) < min_samples:
        raise ValueError(f"need at least {min_samples} canonical examples; have {len(usable)}")

    split = max(20, min(len(usable) - 5, int(len(usable) * train_fraction)))
    train = usable[:split]
    validation = usable[split:]
    raw_train = [raw_v1_features(example.features) for example in train]
    encoder = V1FeatureEncoder.fit(raw_train)
    train_vectors = [encoder.transform(*raw) for raw in raw_train]
    validation_vectors = [
        encoder.transform(*raw_v1_features(example.features))
        for example in validation
    ]

    targets: dict[str, dict[str, Any]] = {}
    metrics: dict[str, TargetMetrics] = {}
    clean_validation_probabilities: list[float] = []

    for target in V1_TARGETS:
        train_labels = [1 if target_value(example, target) else 0 for example in train]
        payload = _fit_target(
            algorithm=algorithm,
            train_vectors=train_vectors,
            train_labels=train_labels,
        )
        targets[target] = payload
        validation_labels = [
            1 if target_value(example, target) else 0 for example in validation
        ]
        probabilities = [
            _predict_target(payload, vector)
            for vector in validation_vectors
        ]
        metrics[target] = _metrics(validation_labels, probabilities, validation)
        if target == "clean_10":
            clean_validation_probabilities = probabilities

    validation_metrics = {
        "chronological": {
            "train_samples": len(train),
            "validation_samples": len(validation),
            "train_from": train[0].features.timestamp.isoformat(),
            "train_through": train[-1].features.timestamp.isoformat(),
            "validation_from": validation[0].features.timestamp.isoformat(),
            "validation_through": validation[-1].features.timestamp.isoformat(),
        },
        "clean_10_by_segment": _segmented_clean_metrics(
            validation,
            clean_validation_probabilities,
        ),
    }
    return V1Bundle(
        algorithm=algorithm,
        encoder=encoder,
        targets=targets,
        metrics=metrics,
        validation_metrics=validation_metrics,
    )


def refit_v1_artifact(
    examples: list[CanonicalTrainingExample],
    *,
    algorithm: str,
) -> dict[str, Any]:
    """Refit deployment artifact on all past examples after validation is frozen."""
    ordered = sorted(
        examples, key=lambda one: (one.features.timestamp, one.setup_id)
    )
    raw = [raw_v1_features(example.features) for example in ordered]
    encoder = V1FeatureEncoder.fit(raw)
    vectors = [encoder.transform(*row) for row in raw]
    targets: dict[str, dict[str, Any]] = {}
    for target in V1_TARGETS:
        labels = [1 if target_value(example, target) else 0 for example in ordered]
        targets[target] = _fit_target(
            algorithm=algorithm, train_vectors=vectors, train_labels=labels
        )
    return {
        "model_schema_version": MODEL_SCHEMA_V1,
        "feature_schema_version": FEATURE_SCHEMA_V1,
        "label_schema_version": LABEL_SCHEMA_V1,
        "algorithm": algorithm,
        "encoder": encoder.to_dict(),
        "feature_names": list(encoder.feature_names),
        "targets": targets,
        "target_order": list(V1_TARGETS),
        "refit_on_all_past_examples": True,
    }

class V1ModelTrainer:
    """Train new candidates; never changes Champion/Shadow state."""

    def __init__(
        self,
        *,
        training_memory: Any,
        models: Any,
        now: Callable[[], Any] = utc_now,
    ) -> None:
        self.training_memory = training_memory
        self.models = models
        self._now = now

    def train_candidates(
        self,
        symbol: str,
        *,
        start_market_date: str = "0001-01-01",
        end_market_date: str = "9999-12-31",
        min_samples: int = 30,
    ) -> tuple[ModelRegistryEntry, ModelRegistryEntry]:
        symbol = symbol.upper()
        examples = self.training_memory.canonical_between(
            symbol,
            start_market_date,
            end_market_date,
        )
        if not examples:
            raise ValueError(f"{symbol}: no canonical V1 training examples")

        parent = self.models.champion(symbol)
        parent_id = parent.model_id if parent is not None else None
        trained_from = min(example.market_date for example in examples)
        trained_through = max(example.market_date for example in examples)
        created_at = to_utc(self._now())

        entries: list[ModelRegistryEntry] = []
        for algorithm in ("logistic_regression_v1", "boosted_stumps_v1"):
            bundle = fit_v1_bundle(
                examples,
                algorithm=algorithm,
                min_samples=min_samples,
            )
            artifact = refit_v1_artifact(examples, algorithm=algorithm)
            digest = hashlib.sha256(
                (
                    f"{symbol}|{algorithm}|{FEATURE_SCHEMA_V1}|{LABEL_SCHEMA_V1}|"
                    f"{trained_from}|{trained_through}|{len(examples)}|"
                    + json.dumps(artifact, sort_keys=True, separators=(",", ":"))
                ).encode("utf-8")
            ).hexdigest()[:20]
            model_id = f"{symbol.lower()}_entry_{algorithm.split('_')[0]}_{digest}"
            entry = ModelRegistryEntry(
                model_id=model_id,
                symbol=symbol,
                algorithm=algorithm,
                status=ModelLifecycleStatus.CANDIDATE.value,
                feature_schema_version=FEATURE_SCHEMA_V1,
                label_schema_version=LABEL_SCHEMA_V1,
                model_schema_version=MODEL_SCHEMA_V1,
                parent_model_id=parent_id,
                hyperparameters={
                    "train_fraction": 0.8,
                    "min_samples": min_samples,
                },
                trained_from=trained_from,
                trained_through=trained_through,
                training_samples=len(examples),
                target_metrics=bundle.metrics,
                validation_metrics=bundle.validation_metrics,
                artifact=artifact,
                created_at=created_at,
            )
            self.models.write_model(entry)
            entries.append(entry)

        return entries[0], entries[1]


def predict_v1_artifact(
    model: ModelRegistryEntry,
    features: Any,
) -> dict[str, float]:
    if model.feature_schema_version != FEATURE_SCHEMA_V1:
        raise ValueError("feature schema mismatch")
    if model.label_schema_version != LABEL_SCHEMA_V1:
        raise ValueError("label schema mismatch")
    if model.model_schema_version != MODEL_SCHEMA_V1:
        raise ValueError("model schema mismatch")

    artifact = model.artifact
    encoder = V1FeatureEncoder.from_dict(artifact["encoder"])
    vector = encoder.transform(*raw_v1_features(features))
    probabilities = {
        target: _predict_target(payload, vector)
        for target, payload in (artifact.get("targets") or {}).items()
    }

    # Reach probabilities must be monotonic by construction at the decision boundary.
    previous = 1.0
    for target in ("reach_5", "reach_10", "reach_20", "reach_30", "reach_40"):
        if target not in probabilities:
            continue
        probabilities[target] = min(previous, probabilities[target])
        previous = probabilities[target]
    return probabilities


# Shared with chronological walk-forward validation; kept as explicit aliases so the
# backtester and trainer use exactly the same target-model semantics.
fit_v1_target = _fit_target
predict_v1_target = _predict_target


def score_v1_model(
    model: ModelRegistryEntry,
    examples: list[CanonicalTrainingExample],
    *,
    decision_threshold: float = 0.55,
) -> dict[str, Any]:
    """Score a frozen registry model on an already-resolved chronological range.

    No fitting occurs here. This is the direct Champion/Challenger holdout path used
    by main_backtest.py.
    """
    if not 0.0 <= decision_threshold <= 1.0:
        raise ValueError("decision_threshold must be between 0 and 1")
    usable = [
        example
        for example in examples
        if example.feature_schema == FEATURE_SCHEMA_V1
        and example.label_schema == LABEL_SCHEMA_V1
        and example.symbol.upper() == model.symbol.upper()
    ]
    usable.sort(key=lambda one: (one.features.timestamp, one.setup_id))
    if not usable:
        return {
            "status": "no_examples",
            "model_id": model.model_id,
            "samples": 0,
            "metrics": {},
            "scored_rows": [],
        }

    probabilities_by_target: dict[str, list[float]] = {
        target: [] for target in V1_TARGETS
    }
    labels_by_target: dict[str, list[int]] = {
        target: [] for target in V1_TARGETS
    }
    scored_rows: list[dict[str, Any]] = []

    for example in usable:
        probabilities = predict_v1_artifact(model, example.features)
        row = {
            "setup_id": example.setup_id,
            "at": example.features.timestamp.isoformat(),
            "direction": example.features.direction.value,
            "probabilities": probabilities,
            "actual": {
                target: target_value(example, target)
                for target in V1_TARGETS
            },
            "mae_before_10": example.outcome.mae_before_10,
            "max_favourable_move": example.outcome.max_favourable_move,
            "max_adverse_move": example.outcome.max_adverse_move,
        }
        clean = probabilities.get("clean_10")
        row["selected_clean"] = (
            clean is not None and clean >= decision_threshold
        )
        scored_rows.append(row)
        for target in V1_TARGETS:
            probability = probabilities.get(target)
            if probability is None:
                continue
            probabilities_by_target[target].append(float(probability))
            labels_by_target[target].append(
                1 if target_value(example, target) else 0
            )

    metrics: dict[str, Any] = {}
    for target in V1_TARGETS:
        labels = labels_by_target[target]
        probs = probabilities_by_target[target]
        if not labels:
            continue
        target_examples = [
            example
            for example in usable
            if target_value(example, target)
        ]
        data = binary_metrics(labels, probs)
        data["average_mae"] = (
            sum(example.outcome.max_adverse_move for example in target_examples)
            / len(target_examples)
            if target_examples
            else None
        )
        data["average_mfe"] = (
            sum(example.outcome.max_favourable_move for example in target_examples)
            / len(target_examples)
            if target_examples
            else None
        )
        metrics[target] = data

    selected = [row for row in scored_rows if row["selected_clean"]]
    selected_clean = sum(
        bool(row["actual"]["clean_10"]) for row in selected
    )
    return {
        "status": "tested_registry_model",
        "historical_reference_only": True,
        "model_id": model.model_id,
        "model_status": model.status,
        "algorithm": model.algorithm,
        "feature_schema": model.feature_schema_version,
        "label_schema": model.label_schema_version,
        "samples": len(usable),
        "decision_threshold": decision_threshold,
        "metrics": metrics,
        "selected_clean_setups": len(selected),
        "selected_clean_wins": selected_clean,
        "selected_clean_precision": (
            selected_clean / len(selected) if selected else None
        ),
        "scored_rows": scored_rows,
    }
