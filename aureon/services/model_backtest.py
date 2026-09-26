"""Chronological walk-forward backtesting for Aureon's movement model."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from aureon.ml.features import FeatureEncoder, raw_training_features
from aureon.ml.logistic import binary_metrics, fit_logistic
from aureon.models.base import to_utc, utc_now
from aureon.models.ml import BacktestFold, ModelBacktest, TargetMetrics
from aureon.models.training import TrainingExample
from aureon.services.model_training import (
    ALGORITHM,
    FEATURE_SCHEMA,
    LABEL_SCHEMA,
    TARGETS,
    target_value,
)


class WalkForwardBacktester:
    """Expanding-window backtest. Test dates are always later than train dates."""

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
        min_train_days: int = 10,
        test_days: int = 2,
        min_train_samples: int = 30,
        min_class_samples: int = 5,
    ) -> ModelBacktest:
        symbol = symbol.upper()
        started = to_utc(self._now())
        backtest_id = self._id(symbol, started)

        examples = [
            example
            for example in self.training_memory.examples_between(
                symbol,
                "0001-01-01",
                "9999-12-31",
            )
            if example.feature_schema_version == FEATURE_SCHEMA
            and example.label_schema_version == LABEL_SCHEMA
            and example.six_dollar_reached is not None
        ]
        examples.sort(key=lambda example: (example.market_date, example.setup_id))
        dates = sorted({example.market_date for example in examples})

        if len(dates) < min_train_days + test_days:
            failed = ModelBacktest(
                backtest_id=backtest_id,
                model_id=model_id,
                symbol=symbol,
                status="insufficient_data",
                feature_schema_version=FEATURE_SCHEMA,
                label_schema_version=LABEL_SCHEMA,
                started_at=started,
                completed_at=to_utc(self._now()),
                start_market_date=dates[0] if dates else None,
                end_market_date=dates[-1] if dates else None,
                failure_message=(
                    f"need at least {min_train_days + test_days} market dates; "
                    f"have {len(dates)}"
                ),
            )
            self.models.write_backtest(failed)
            return failed

        folds: list[BacktestFold] = []
        aggregate_labels: dict[str, list[int]] = defaultdict(list)
        aggregate_probabilities: dict[str, list[float]] = defaultdict(list)

        fold_number = 0
        test_start_index = min_train_days
        while test_start_index < len(dates):
            test_end_index = min(test_start_index + test_days, len(dates))
            train_dates = dates[:test_start_index]
            test_dates = dates[test_start_index:test_end_index]
            if not test_dates:
                break

            train = [example for example in examples if example.market_date in train_dates]
            test = [example for example in examples if example.market_date in test_dates]
            fold_number += 1
            target_metrics: dict[str, TargetMetrics] = {}

            if len(train) >= min_train_samples:
                train_raw = [raw_training_features(example) for example in train]
                encoder = FeatureEncoder.fit(train_raw)
                train_vectors = [encoder.transform(*raw) for raw in train_raw]
                test_vectors = [
                    encoder.transform(*raw_training_features(example))
                    for example in test
                ]

                for target in TARGETS:
                    train_indexes = [
                        index
                        for index, example in enumerate(train)
                        if target_value(example, target) is not None
                    ]
                    train_labels = [
                        1 if bool(target_value(train[index], target)) else 0
                        for index in train_indexes
                    ]
                    test_indexes = [
                        index
                        for index, example in enumerate(test)
                        if target_value(example, target) is not None
                    ]
                    test_labels = [
                        1 if bool(target_value(test[index], target)) else 0
                        for index in test_indexes
                    ]

                    if (
                        len(train_labels) >= min_train_samples
                        and sum(train_labels) >= min_class_samples
                        and len(train_labels) - sum(train_labels) >= min_class_samples
                        and test_labels
                    ):
                        model = fit_logistic(
                            [train_vectors[index] for index in train_indexes],
                            train_labels,
                        )
                        probabilities = [
                            model.probability(test_vectors[index])
                            for index in test_indexes
                        ]
                        metric_data = binary_metrics(test_labels, probabilities)
                        target_metrics[target] = TargetMetrics.model_validate(metric_data)
                        aggregate_labels[target].extend(test_labels)
                        aggregate_probabilities[target].extend(probabilities)

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
            test_start_index = test_end_index

        aggregate = {
            target: TargetMetrics.model_validate(
                binary_metrics(
                    aggregate_labels[target],
                    aggregate_probabilities[target],
                )
            )
            for target in TARGETS
            if aggregate_labels[target]
        }
        completed = to_utc(self._now())
        result = ModelBacktest(
            backtest_id=backtest_id,
            model_id=model_id,
            symbol=symbol,
            status="complete" if aggregate.get("six") is not None else "insufficient_data",
            feature_schema_version=FEATURE_SCHEMA,
            label_schema_version=LABEL_SCHEMA,
            started_at=started,
            completed_at=completed,
            start_market_date=dates[0],
            end_market_date=dates[-1],
            folds=tuple(folds),
            aggregate_metrics=aggregate,
            out_of_sample_predictions=(
                aggregate["six"].samples if "six" in aggregate else 0
            ),
            failure_message=(
                None
                if aggregate.get("six") is not None
                else "no walk-forward fold could train and score the $6 target"
            ),
        )
        self.models.write_backtest(result)
        return result

    @staticmethod
    def _id(symbol: str, at: Any) -> str:
        digest = hashlib.sha256(
            f"walk_forward|{symbol}|{at.isoformat()}".encode("utf-8")
        ).hexdigest()[:20]
        return f"backtest_{symbol.lower()}_{digest}"



class V1WalkForwardBacktester:
    """Expanding-window V1 backtest over canonical frozen examples.

    Training dates are always strictly earlier than test dates. The encoder is fit only
    on each fold's training slice, so categorical vocabularies and normalization cannot
    leak from the future.
    """

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
        min_train_days: int = 20,
        test_days: int = 5,
        min_train_samples: int = 30,
    ) -> ModelBacktest:
        from aureon.ml.v1_features import V1FeatureEncoder, raw_v1_features
        from aureon.models.learning_v1 import FEATURE_SCHEMA_V1, LABEL_SCHEMA_V1
        from aureon.services.v1_model_training import (
            V1_TARGETS,
            fit_v1_target,
            predict_v1_target,
            target_value as v1_target_value,
        )

        symbol = symbol.upper()
        started = to_utc(self._now())
        backtest_id = self._id(symbol, started) + "_v1"

        model = self.models.get_model(model_id) if model_id else None
        algorithm = model.algorithm if model is not None else "logistic_regression_v1"
        if algorithm not in {"logistic_regression_v1", "boosted_stumps_v1"}:
            raise ValueError(f"V1 walk-forward does not support {algorithm!r}")

        examples = [
            example
            for example in self.training_memory.canonical_between(
                symbol,
                "0001-01-01",
                "9999-12-31",
            )
            if example.feature_schema == FEATURE_SCHEMA_V1
            and example.label_schema == LABEL_SCHEMA_V1
        ]
        examples.sort(key=lambda one: (one.market_date, one.features.timestamp, one.setup_id))
        dates = sorted({example.market_date for example in examples})

        if len(dates) < min_train_days + test_days:
            failed = ModelBacktest(
                backtest_id=backtest_id,
                model_id=model_id,
                symbol=symbol,
                status="insufficient_data",
                algorithm=algorithm,
                feature_schema_version=FEATURE_SCHEMA_V1,
                label_schema_version=LABEL_SCHEMA_V1,
                started_at=started,
                completed_at=to_utc(self._now()),
                start_market_date=dates[0] if dates else None,
                end_market_date=dates[-1] if dates else None,
                failure_message=(
                    f"need at least {min_train_days + test_days} market dates; "
                    f"have {len(dates)}"
                ),
            )
            self.models.write_backtest(failed)
            return failed

        folds: list[BacktestFold] = []
        aggregate_labels: dict[str, list[int]] = defaultdict(list)
        aggregate_probabilities: dict[str, list[float]] = defaultdict(list)
        fold_number = 0
        test_start_index = min_train_days

        while test_start_index < len(dates):
            test_end_index = min(test_start_index + test_days, len(dates))
            train_dates = dates[:test_start_index]
            test_dates = dates[test_start_index:test_end_index]
            if not test_dates:
                break

            train = [one for one in examples if one.market_date in train_dates]
            test = [one for one in examples if one.market_date in test_dates]
            fold_number += 1
            metrics: dict[str, TargetMetrics] = {}

            if len(train) >= min_train_samples and test:
                raw_train = [raw_v1_features(one.features) for one in train]
                encoder = V1FeatureEncoder.fit(raw_train)
                train_vectors = [encoder.transform(*raw) for raw in raw_train]
                test_vectors = [
                    encoder.transform(*raw_v1_features(one.features))
                    for one in test
                ]
                for target in V1_TARGETS:
                    train_labels = [
                        1 if v1_target_value(one, target) else 0 for one in train
                    ]
                    payload = fit_v1_target(
                        algorithm=algorithm,
                        train_vectors=train_vectors,
                        train_labels=train_labels,
                    )
                    test_labels = [
                        1 if v1_target_value(one, target) else 0 for one in test
                    ]
                    probabilities = [
                        predict_v1_target(payload, vector)
                        for vector in test_vectors
                    ]
                    metric_data = binary_metrics(test_labels, probabilities)
                    positives = [
                        one
                        for one, label in zip(test, test_labels, strict=True)
                        if label == 1
                    ]
                    metric_data["average_mae"] = (
                        sum(one.outcome.max_adverse_move for one in positives)
                        / len(positives)
                        if positives
                        else None
                    )
                    metric_data["average_mfe"] = (
                        sum(one.outcome.max_favourable_move for one in positives)
                        / len(positives)
                        if positives
                        else None
                    )
                    metrics[target] = TargetMetrics.model_validate(metric_data)
                    aggregate_labels[target].extend(test_labels)
                    aggregate_probabilities[target].extend(probabilities)

            folds.append(
                BacktestFold(
                    fold=fold_number,
                    train_from=train_dates[0],
                    train_through=train_dates[-1],
                    test_from=test_dates[0],
                    test_through=test_dates[-1],
                    train_samples=len(train),
                    test_samples=len(test),
                    target_metrics=metrics,
                )
            )
            test_start_index = test_end_index

        aggregate = {
            target: TargetMetrics.model_validate(
                binary_metrics(
                    aggregate_labels[target],
                    aggregate_probabilities[target],
                )
            )
            for target in V1_TARGETS
            if aggregate_labels[target]
        }
        result = ModelBacktest(
            backtest_id=backtest_id,
            model_id=model_id,
            symbol=symbol,
            status="complete" if aggregate.get("clean_10") is not None else "insufficient_data",
            algorithm=algorithm,
            feature_schema_version=FEATURE_SCHEMA_V1,
            label_schema_version=LABEL_SCHEMA_V1,
            started_at=started,
            completed_at=to_utc(self._now()),
            start_market_date=dates[0],
            end_market_date=dates[-1],
            folds=tuple(folds),
            aggregate_metrics=aggregate,
            out_of_sample_predictions=(
                aggregate["clean_10"].samples if "clean_10" in aggregate else 0
            ),
            failure_message=(
                None
                if aggregate.get("clean_10") is not None
                else "no V1 fold could score clean_10"
            ),
        )
        self.models.write_backtest(result)
        return result
