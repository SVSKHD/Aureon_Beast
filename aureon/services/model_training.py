"""Offline V1 model training from immutable canonical training examples.

Training creates a Candidate only. It never overwrites or activates the Champion.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aureon.ml.boosted_stumps import BoostedStumpModel, fit_boosted_stumps
from aureon.ml.features import FeatureEncoder, raw_training_features
from aureon.ml.logistic import LogisticModel, binary_metrics, fit_logistic
from aureon.models.base import to_utc, utc_now
from aureon.models.ml import EvolutionDecision, ModelRegistryEntry, ModelTrainingRun, TargetMetrics
from aureon.models.training import TrainingExample
from aureon.services.learning_contract import (
    FEATURE_SCHEMA_V1,
    LABEL_SCHEMA_V1,
    MODEL_SCHEMA_V1,
    enforce_target_probability_order,
)

FEATURE_SCHEMA = FEATURE_SCHEMA_V1
LABEL_SCHEMA = LABEL_SCHEMA_V1
MODEL_SCHEMA = MODEL_SCHEMA_V1
ALGORITHM = "logistic_regression_v1"
CHALLENGER_ALGORITHM = "boosted_stumps_v1"
TARGETS = ("clean_10", "reach_5", "reach_10", "reach_20", "reach_30", "reach_40")
SUPPORTED_ALGORITHMS = (ALGORITHM, CHALLENGER_ALGORITHM)


def target_value(example: TrainingExample, target: str) -> bool | None:
    outcome = example.outcome
    if outcome is None:
        return None
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


def prediction_metrics(labels: list[int], probabilities: list[float]) -> dict[str, Any]:
    if not probabilities:
        return {
            "samples": len(labels),
            "positives": sum(labels),
            "negatives": len(labels) - sum(labels),
            "accuracy": None,
            "precision": None,
            "recall": None,
            "brier": None,
            "log_loss": None,
            "roc_auc": None,
            "false_positive_rate": None,
        }
    payload = binary_metrics(labels, probabilities)
    predicted = [1 if p >= 0.5 else 0 for p in probabilities]
    negatives = sum(1 for y in labels if y == 0)
    false_positives = sum(1 for y, p in zip(labels, predicted, strict=True) if y == 0 and p == 1)
    payload["false_positive_rate"] = (
        false_positives / negatives if negatives else None
    )
    return payload


def _fit_model(vectors: list[list[float]], labels: list[int], algorithm: str) -> Any:
    if algorithm == ALGORITHM:
        return fit_logistic(vectors, labels)
    if algorithm == CHALLENGER_ALGORITHM:
        return fit_boosted_stumps(vectors, labels)
    raise ValueError(f"unsupported algorithm {algorithm!r}")


def _model_payload(model: Any, algorithm: str) -> dict[str, Any]:
    return {
        "algorithm": algorithm,
        "model": model.to_dict(),
    }


def probability_from_payload(payload: dict[str, Any], vector: list[float]) -> float:
    algorithm = str(payload.get("algorithm") or ALGORITHM)
    model_payload = payload.get("model") or payload
    if algorithm == ALGORITHM:
        return LogisticModel.from_dict(model_payload).probability(vector)
    if algorithm == CHALLENGER_ALGORITHM:
        return BoostedStumpModel.from_dict(model_payload).probability(vector)
    raise ValueError(f"unsupported model artifact algorithm {algorithm!r}")


@dataclass(frozen=True)
class TrainedBundle:
    encoder: FeatureEncoder
    models: dict[str, Any]
    metrics: dict[str, TargetMetrics]
    algorithm: str

    def to_artifact(self) -> dict[str, Any]:
        return {
            "model_schema_version": MODEL_SCHEMA,
            "algorithm": self.algorithm,
            "encoder": self.encoder.to_dict(),
            "feature_names": list(self.encoder.feature_names),
            "targets": {
                target: _model_payload(model, self.algorithm)
                for target, model in self.models.items()
            },
        }

    def predict(self, features: tuple[dict[str, float], dict[str, str]]) -> dict[str, float]:
        vector = self.encoder.transform(*features)
        probabilities = {
            target: float(model.probability(vector))
            for target, model in self.models.items()
        }
        return enforce_target_probability_order(probabilities)


def fit_bundle(
    examples: list[TrainingExample],
    *,
    algorithm: str = ALGORITHM,
    min_samples: int = 30,
    min_class_samples: int = 5,
) -> TrainedBundle:
    if algorithm not in SUPPORTED_ALGORITHMS:
        raise ValueError(f"unsupported algorithm {algorithm!r}")
    usable = [
        example
        for example in examples
        if example.feature_schema_version == FEATURE_SCHEMA
        and example.label_schema_version == LABEL_SCHEMA
        and example.outcome is not None
    ]
    if len(usable) < min_samples:
        raise ValueError(
            f"need at least {min_samples} canonical labelled examples; have {len(usable)}"
        )

    raw = [raw_training_features(example) for example in usable]
    encoder = FeatureEncoder.fit(raw)
    vectors = [encoder.transform(*features) for features in raw]

    models: dict[str, Any] = {}
    metrics: dict[str, TargetMetrics] = {}
    for target in TARGETS:
        indexes = [
            index
            for index, example in enumerate(usable)
            if target_value(example, target) is not None
        ]
        labels = [
            1 if bool(target_value(usable[index], target)) else 0
            for index in indexes
        ]
        probabilities: list[float] = []
        if (
            len(labels) >= min_samples
            and sum(labels) >= min_class_samples
            and len(labels) - sum(labels) >= min_class_samples
        ):
            target_vectors = [vectors[index] for index in indexes]
            model = _fit_model(target_vectors, labels, algorithm)
            models[target] = model
            probabilities = [float(model.probability(vector)) for vector in target_vectors]
        metrics[target] = TargetMetrics.model_validate(
            prediction_metrics(labels, probabilities)
        )

    if "clean_10" not in models:
        raise ValueError(
            "clean_10 cannot be trained yet; enough positive and negative canonical "
            "examples are required"
        )
    return TrainedBundle(
        encoder=encoder,
        models=models,
        metrics=metrics,
        algorithm=algorithm,
    )


class ModelTrainer:
    """Train and register a Candidate; lifecycle advancement is EvolutionAgent's job."""

    def __init__(
        self,
        *,
        training_memory: Any,
        models: Any,
        now: Any = utc_now,
    ) -> None:
        self.training_memory = training_memory
        self.models = models
        self._now = now

    def train(
        self,
        symbol: str,
        *,
        algorithm: str = ALGORITHM,
        min_samples: int = 30,
        min_class_samples: int = 5,
        hyperparameters: dict[str, Any] | None = None,
        start_market_date: str = "0001-01-01",
        end_market_date: str = "9999-12-31",
    ) -> ModelRegistryEntry:
        symbol = symbol.upper()
        started = to_utc(self._now())
        run_id = self._id("train", symbol, started)
        running = ModelTrainingRun(
            run_id=run_id,
            symbol=symbol,
            status="running",
            algorithm=algorithm,
            feature_schema_version=FEATURE_SCHEMA,
            label_schema_version=LABEL_SCHEMA,
            started_at=started,
        )
        self.models.write_training_run(running)

        examples = self.training_memory.examples_between(
            symbol,
            start_market_date,
            end_market_date,
        )
        usable = [
            example
            for example in examples
            if example.feature_schema_version == FEATURE_SCHEMA
            and example.label_schema_version == LABEL_SCHEMA
            and example.outcome is not None
        ]
        try:
            bundle = fit_bundle(
                usable,
                algorithm=algorithm,
                min_samples=min_samples,
                min_class_samples=min_class_samples,
            )
        except Exception as exc:
            self.models.write_training_run(
                running.model_copy(
                    update={
                        "status": "failed",
                        "completed_at": to_utc(self._now()),
                        "sample_count": len(usable),
                        "failure_message": str(exc),
                    }
                )
            )
            raise

        trained_from = min(example.market_date for example in usable)
        trained_through = max(example.market_date for example in usable)
        completed = to_utc(self._now())
        artifact = bundle.to_artifact()
        digest = hashlib.sha256(
            (
                f"{symbol}|{FEATURE_SCHEMA}|{LABEL_SCHEMA}|{algorithm}|"
                f"{trained_from}|{trained_through}|{len(usable)}|"
                + json.dumps(artifact, sort_keys=True, separators=(",", ":"))
            ).encode("utf-8")
        ).hexdigest()[:20]
        model_id = f"{symbol.lower()}_v1_{digest}"
        champion = self.models.active_champion(symbol)
        entry = ModelRegistryEntry(
            model_id=model_id,
            parent_model_id=champion.model_id if champion is not None else None,
            symbol=symbol,
            algorithm=algorithm,
            status="candidate",
            feature_schema_version=FEATURE_SCHEMA,
            label_schema_version=LABEL_SCHEMA,
            model_schema_version=MODEL_SCHEMA,
            trained_from=trained_from,
            trained_through=trained_through,
            training_samples=len(usable),
            target_metrics=bundle.metrics,
            hyperparameters=hyperparameters or {},
            artifact=artifact,
            created_at=completed,
        )
        self.models.write_model(entry)
        self.models.write_training_run(
            running.model_copy(
                update={
                    "model_id": model_id,
                    "status": "complete",
                    "completed_at": completed,
                    "sample_count": len(usable),
                    "trained_from": trained_from,
                    "trained_through": trained_through,
                    "target_metrics": bundle.metrics,
                }
            )
        )
        self.models.write_evolution_decision(
            EvolutionDecision(
                decision_id=self._id("candidate", symbol, completed) + "_" + digest[:8],
                symbol=symbol,
                model_id=model_id,
                champion_model_id=champion.model_id if champion is not None else None,
                action="candidate_created",
                reason="chronological canonical V1 training completed",
                metrics={
                    key: value.model_dump(mode="json")
                    for key, value in bundle.metrics.items()
                },
                detail={"algorithm": algorithm, "training_samples": len(usable)},
                decided_at=completed,
            )
        )
        return entry

    @staticmethod
    def _id(kind: str, symbol: str, at: datetime) -> str:
        digest = hashlib.sha256(
            f"{kind}|{symbol}|{at.isoformat()}".encode("utf-8")
        ).hexdigest()[:20]
        return f"{kind}_{symbol.lower()}_{digest}"
