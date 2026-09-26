"""Chronological walk-forward validation for Aureon V1 models.

No random shuffling is permitted. Every fold enforces max(train_date) < min(test_date).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from aureon.ml.features import raw_training_features
from aureon.models.base import to_utc, utc_now
from aureon.models.ml import BacktestFold, ModelBacktest, TargetMetrics
from aureon.models.training import TrainingExample
from aureon.services.learning_contract import enforce_target_probability_order
from aureon.services.model_training import (
    ALGORITHM,
    FEATURE_SCHEMA,
    LABEL_SCHEMA,
    TARGETS,
    fit_bundle,
    prediction_metrics,
    target_value,
)


def _augment_excursions(
    metrics: dict[str, Any],
    examples: list[TrainingExample],
) -> dict[str, Any]:
    maes = [
        float(one.outcome.max_adverse_move)
        for one in examples
        if one.outcome is not None
    ]
    mfes = [
        float(one.outcome.max_favourable_move)
        for one in examples
        if one.outcome is not None
    ]
    payload = dict(metrics)
    payload["average_mae"] = sum(maes) / len(maes) if maes else None
    payload["average_mfe"] = sum(mfes) / len(mfes) if mfes else None
    return payload


def _clean_breakdowns(
    examples: list[TrainingExample],
    probabilities: list[float],
) -> dict[str, Any]:
    dimensions = {
        "session": lambda e: str((e.features or {}).get("session") or "unknown"),
        "volatility_regime": lambda e: str(
            (e.features or {}).get("volatility_regime") or "unknown"
        ),
        "market_regime": lambda e: str(
            (e.features or {}).get("market_regime") or "unknown"
        ),
        "direction": lambda e: str(e.direction or "unknown"),
        "timeframe": lambda e: e.timeframe.value,
    }
    result: dict[str, Any] = {}
    for dimension, getter in dimensions.items():
        groups: dict[str, list[tuple[TrainingExample, float]]] = defaultdict(list)
        for example, probability in zip(examples, probabilities, strict=True):
            groups[getter(example)].append((example, probability))
        result[dimension] = {}
        for value, pairs in sorted(groups.items()):
            labels = [
                1 if bool(pair[0].outcome and pair[0].outcome.clean_10) else 0
                for pair in pairs
            ]
            probs = [pair[1] for pair in pairs]
            result[dimension][value] = _augment_excursions(
                prediction_metrics(labels, probs),
                [pair[0] for pair in pairs],
            )
    return result


class WalkForwardBacktester:
    """Expanding-window walk-forward test over canonical TrainingMemory."""

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

    def run(
        self,
        symbol: str,
        *,
        model_id: str | None = None,
        algorithm: str | None = None,
        min_train_days: int = 10,
        test_days: int = 1,
        min_train_samples: int = 30,
        min_class_samples: int = 5,
    ) -> ModelBacktest:
        symbol = symbol.upper()
        started = to_utc(self._now())
        backtest_id = self._id(symbol, started)

        selected_model = self.models.get_model(model_id) if model_id else None
        if model_id and selected_model is None:
            raise LookupError(f"no model {model_id}")
        if selected_model is not None:
            if (
                selected_model.feature_schema_version != FEATURE_SCHEMA
                or selected_model.label_schema_version != LABEL_SCHEMA
            ):
                raise ValueError("model schema is incompatible with V1 walk-forward data")
            algorithm = selected_model.algorithm
        algorithm = algorithm or ALGORITHM

        examples = [
            one
            for one in self.training_memory.examples_between(
                symbol, "0001-01-01", "9999-12-31"
            )
            if one.feature_schema_version == FEATURE_SCHEMA
            and one.label_schema_version == LABEL_SCHEMA
            and one.outcome is not None
        ]
        dates = sorted({one.market_date for one in examples})
        if len(dates) < min_train_days + test_days:
            result = ModelBacktest(
                backtest_id=backtest_id,
                model_id=model_id,
                symbol=symbol,
                algorithm=algorithm,
                status="insufficient_data",
                feature_schema_version=FEATURE_SCHEMA,
                label_schema_version=LABEL_SCHEMA,
                started_at=started,
                completed_at=to_utc(self._now()),
                failure_message=(
                    f"need at least {min_train_days + test_days} market dates; "
                    f"have {len(dates)}"
                ),
            )
            self.models.write_backtest(result)
            return result

        aggregate_labels: dict[str, list[int]] = defaultdict(list)
        aggregate_probabilities: dict[str, list[float]] = defaultdict(list)
        clean_examples: list[TrainingExample] = []
        clean_probabilities: list[float] = []
        folds: list[BacktestFold] = []

        test_start = min_train_days
        fold_number = 1
        while test_start < len(dates):
            test_end = min(len(dates), test_start + test_days)
            train_dates = dates[:test_start]
            test_dates = dates[test_start:test_end]
            if not test_dates:
                break
            if train_dates[-1] >= test_dates[0]:
                raise AssertionError("walk-forward leakage: train date overlaps test date")

            train = [one for one in examples if one.market_date in train_dates]
            test = [one for one in examples if one.market_date in test_dates]
            target_metrics: dict[str, TargetMetrics] = {}

            try:
                bundle = fit_bundle(
                    train,
                    algorithm=algorithm,
                    min_samples=min_train_samples,
                    min_class_samples=min_class_samples,
                )
            except ValueError:
                bundle = None

            if bundle is not None and test:
                raw_test = [raw_training_features(one) for one in test]
                test_vectors = [bundle.encoder.transform(*raw) for raw in raw_test]
                row_probs: list[dict[str, float]] = []
                for vector in test_vectors:
                    probabilities = {
                        target: float(model.probability(vector))
                        for target, model in bundle.models.items()
                    }
                    row_probs.append(enforce_target_probability_order(probabilities))

                for target in TARGETS:
                    indexes = [
                        i for i, one in enumerate(test)
                        if target in bundle.models and target_value(one, target) is not None
                    ]
                    labels = [
                        1 if bool(target_value(test[i], target)) else 0
                        for i in indexes
                    ]
                    probabilities = [row_probs[i][target] for i in indexes]
                    if not labels:
                        continue
                    payload = _augment_excursions(
                        prediction_metrics(labels, probabilities),
                        [test[i] for i in indexes],
                    )
                    target_metrics[target] = TargetMetrics.model_validate(payload)
                    aggregate_labels[target].extend(labels)
                    aggregate_probabilities[target].extend(probabilities)

                if "clean_10" in bundle.models:
                    clean_examples.extend(test)
                    clean_probabilities.extend(
                        [row.get("clean_10", 0.0) for row in row_probs]
                    )

            folds.append(
                BacktestFold(
                    fold=fold_number,
                    train_from=train_dates[0],
                    train_through=train_dates[-1],
                    test_from=test_dates[0],
                    test_through=test_dates[-1],
                    train_samples=len(train),
                    test_samples=len(test),
                    target_metrics=target_metrics,
                )
            )
            fold_number += 1
            test_start = test_end

        aggregate: dict[str, TargetMetrics] = {}
        for target in TARGETS:
            labels = aggregate_labels.get(target, [])
            probs = aggregate_probabilities.get(target, [])
            if not labels:
                continue
            metric_examples = (
                clean_examples[: len(labels)]
                if target == "clean_10"
                else []
            )
            payload = prediction_metrics(labels, probs)
            if metric_examples:
                payload = _augment_excursions(payload, metric_examples)
            aggregate[target] = TargetMetrics.model_validate(payload)

        breakdowns = (
            _clean_breakdowns(clean_examples, clean_probabilities)
            if clean_examples and len(clean_examples) == len(clean_probabilities)
            else {}
        )
        completed = to_utc(self._now())
        result = ModelBacktest(
            backtest_id=backtest_id,
            model_id=model_id,
            symbol=symbol,
            algorithm=algorithm,
            status="complete" if aggregate.get("clean_10") is not None else "insufficient_data",
            feature_schema_version=FEATURE_SCHEMA,
            label_schema_version=LABEL_SCHEMA,
            started_at=started,
            completed_at=completed,
            start_market_date=dates[0],
            end_market_date=dates[-1],
            folds=tuple(folds),
            aggregate_metrics=aggregate,
            breakdown_metrics=breakdowns,
            out_of_sample_predictions=(
                aggregate["clean_10"].samples if "clean_10" in aggregate else 0
            ),
            failure_message=(
                None
                if aggregate.get("clean_10") is not None
                else "no chronological fold could train and score clean_10"
            ),
        )
        self.models.write_backtest(result)
        return result

    @staticmethod
    def _id(symbol: str, at: Any) -> str:
        digest = hashlib.sha256(
            f"walk_forward_v1|{symbol}|{at.isoformat()}".encode("utf-8")
        ).hexdigest()[:20]
        return f"backtest_{symbol.lower()}_{digest}"
