#!/usr/bin/env python3
"""Run Aureon's chronological walk-forward movement-model backtest."""

from __future__ import annotations

import argparse
import logging
import sys

from aureon.config import AureonConfig
from aureon.services.model_backtest import WalkForwardBacktester
from aureon.storage.runtime import build_storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--model-id")
    parser.add_argument("--min-train-days", type=int, default=10)
    parser.add_argument("--test-days", type=int, default=2)
    parser.add_argument("--min-train-samples", type=int, default=30)
    parser.add_argument("--min-class-samples", type=int, default=5)
    parser.add_argument("--v1", action="store_true", help="run canonical clean_10 V1 walk-forward")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = AureonConfig.from_env()
    symbol = args.symbol.upper()
    if symbol not in config.symbols:
        parser.error(f"{symbol} is not configured: {', '.join(config.symbols)}")

    storage = build_storage(
        account_scope=config.account_scope,
        state_heartbeat_seconds=config.state_heartbeat_seconds,
    )
    model_id = args.model_id
    if model_id is None:
        latest = storage.models.latest_model(symbol)
        model_id = latest.model_id if latest is not None else None

    if args.v1:
        from aureon.services.model_backtest import V1WalkForwardBacktester

        result = V1WalkForwardBacktester(
            training_memory=storage.training_memory,
            models=storage.models,
        ).run(
            symbol,
            model_id=model_id,
            min_train_days=args.min_train_days,
            test_days=args.test_days,
            min_train_samples=args.min_train_samples,
        )
    else:
        result = WalkForwardBacktester(
            training_memory=storage.training_memory,
            models=storage.models,
        ).run(
            symbol,
            model_id=model_id,
            min_train_days=args.min_train_days,
            test_days=args.test_days,
            min_train_samples=args.min_train_samples,
            min_class_samples=args.min_class_samples,
        )

    print(
        f"backtest {result.backtest_id} [{result.symbol}] status={result.status} "
        f"folds={len(result.folds)} oos={result.out_of_sample_predictions}"
    )
    if result.failure_message:
        print(f"  {result.failure_message}")
    for target, metrics in result.aggregate_metrics.items():
        print(
            f"  {target:<7} n={metrics.samples} auc={_fmt(metrics.roc_auc)} "
            f"brier={_fmt(metrics.brier)} precision={_fmt(metrics.precision)} "
            f"recall={_fmt(metrics.recall)}"
        )
    return 0 if result.status == "complete" else 2


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


if __name__ == "__main__":
    sys.exit(main())
