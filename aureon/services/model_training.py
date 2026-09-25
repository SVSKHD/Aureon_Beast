"""Offline model training from immutable EOD training examples."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aureon.ml.features import FeatureEncoder, raw_training_features
from aureon.ml.logistic import LogisticModel, binary_metrics, fit_logistic
from aureon.models.base import to_utc, utc_now
from aureon.models.ml import ModelRegistryEntry, ModelTrainingRun, TargetMetrics
from aureon.models.training import TrainingExample

FEATURE_SCHEMA = "EOD_SETUP_FEATURES_V1"
LABEL_SCHEMA = "FAVOURABLE_MOVE_LADDER_V2"
MODEL_SCHEMA = "AUREON_MOVE_MODEL_V1"
ALGORITHM = "logistic_regression_v1"
TARGETS = ("six", "twenty", "forty")


def target_value(example: TrainingExample, target: str) -> bool | None:
    if target == "six":
        return example.six_dollar_reached
    if target == "twenty":
        return example.twenty_dollar_reached
    if target == "forty":
        return example.forty_dollar_reached
    raise KeyError(target)


@dataclass(frozen=True)
class TrainedBundle:
    encoder: FeatureEncoder
    models: dict[str, LogisticModel]
    metrics: dict[str, TargetMetrics]

    def to_artifact(self) -> dict[str, Any]:
        return {
            "model_schema_version": MODEL_SCHEMA,
            "algorithm": ALGORITHM,
            "encoder": self.encoder.to_dict(),
            "feature_names": list(self.encoder.feature_names),
            "targets": {
                target: model.to_dict()
                for target, model in self.models.items()
            },
        }


def fit_bundle(
    examples: list[TrainingExample],
    *,
    min_samples: int = 30,
    min_class_samples: int = 5,
) -> TrainedBundle:
    usable = [
        example
        for example in examples
        if example.feature_schema_version == FEATURE_SCHEMA
        and example.label_schema_version == LABEL_SCHEMA
        and example.six_dollar_reached is not None
    ]
    if len(usable) < min_samples:
        raise ValueError(
            f"need at least {min_samples} labelled examples; have {len(usable)}"
        )

    raw = [raw_training_features(example) for example in usable]
    encoder = FeatureEncoder.fit(raw)
    vectors = [encoder.transform(*features) for features in raw]

    models: dict[str, LogisticModel] = {}
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
            model = fit_logistic(target_vectors, labels)
            models[target] = model
            probabilities = [model.probability(vector) for vector in target_vectors]
        metric_data = binary_metrics(labels, probabilities) if probabilities else {
            "samples": len(labels),
            "positives": sum(labels),
            "negatives": len(labels) - sum(labels),
            "accuracy": None,
            "precision": None,
            "recall": None,
            "brier": None,
            "log_loss": None,
            "roc_auc": None,
        }
        metrics[target] = TargetMetrics.model_validate(metric_data)

    if "six" not in models:
        raise ValueError(
            "the $6 classifier cannot be trained yet; both positive and negative "
            "examples are required"
        )
    return TrainedBundle(encoder=encoder, models=models, metrics=metrics)


class ModelTrainer:
    """Train and register a candidate model; activation is explicit."""

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
        min_samples: int = 30,
        min_class_samples: int = 5,
    ) -> ModelRegistryEntry:
        symbol = symbol.upper()
        started = to_utc(self._now())
        run_id = self._id("train", symbol, started)
        running = ModelTrainingRun(
            run_id=run_id,
            symbol=symbol,
            status="running",
            feature_schema_version=FEATURE_SCHEMA,
            label_schema_version=LABEL_SCHEMA,
            started_at=started,
        )
        self.models.write_training_run(running)

        examples = self.training_memory.examples_between(
            symbol,
            "0001-01-01",
            "9999-12-31",
        )
        usable = [
            example
            for example in examples
            if example.feature_schema_version == FEATURE_SCHEMA
            and example.label_schema_version == LABEL_SCHEMA
            and example.six_dollar_reached is not None
        ]
        try:
            bundle = fit_bundle(
                usable,
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
                f"{symbol}|{FEATURE_SCHEMA}|{LABEL_SCHEMA}|"
                f"{trained_from}|{trained_through}|{len(usable)}|"
                + json.dumps(artifact, sort_keys=True, separators=(",", ":"))
            ).encode("utf-8")
        ).hexdigest()[:20]
        model_id = f"{symbol.lower()}_move_{digest}"
        entry = ModelRegistryEntry(
            model_id=model_id,
            symbol=symbol,
            status="candidate",
            feature_schema_version=FEATURE_SCHEMA,
            label_schema_version=LABEL_SCHEMA,
            model_schema_version=MODEL_SCHEMA,
            trained_from=trained_from,
            trained_through=trained_through,
            training_samples=len(usable),
            target_metrics=bundle.metrics,
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
        return entry

    @staticmethod
    def _id(kind: str, symbol: str, at: datetime) -> str:
        digest = hashlib.sha256(
            f"{kind}|{symbol}|{at.isoformat()}".encode("utf-8")
        ).hexdigest()[:20]
        return f"{kind}_{symbol.lower()}_{digest}"
