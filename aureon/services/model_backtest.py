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
