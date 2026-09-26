#!/usr/bin/env python3
"""Train Aureon's offline movement model and optionally activate it in shadow mode."""

from __future__ import annotations

import argparse
import logging
import sys

from aureon.config import AureonConfig
from aureon.services.model_training import ModelTrainer
from aureon.storage.runtime import build_storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument("--min-class-samples", type=int, default=5)
    parser.add_argument("--v1", action="store_true", help="train canonical clean_10 V1 logistic + boosted candidates")
    parser.add_argument("--from", dest="start", default="0001-01-01")
    parser.add_argument("--to", dest="end", default="9999-12-31")
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
    if args.v1:
        from aureon.services.v1_model_training import V1ModelTrainer

        candidates = V1ModelTrainer(
            training_memory=storage.training_memory,
            models=storage.models,
        ).train_candidates(
            symbol,
            start_market_date=args.start,
            end_market_date=args.end,
            min_samples=args.min_samples,
        )
        for model in candidates:
            print(
                f"candidate {model.model_id} [{model.algorithm}] "
                f"samples={model.training_samples} "
                f"period={model.trained_from}..{model.trained_through}"
            )
            clean = model.target_metrics.get("clean_10")
            if clean is not None:
                print(
                    f"  clean_10 n={clean.samples} auc={_fmt(clean.roc_auc)} "
                    f"brier={_fmt(clean.brier)} precision={_fmt(clean.precision)} "
                    f"recall={_fmt(clean.recall)} fpr={_fmt(clean.false_positive_rate)}"
                )
        print(
            "  candidates only; run V1 walk-forward for each model_id, then let "
            "EvolutionAgent qualify -> shadow -> Champion."
        )
    else:
        trainer = ModelTrainer(
            training_memory=storage.training_memory,
            models=storage.models,
        )
        model = trainer.train(
            symbol,
            min_samples=args.min_samples,
            min_class_samples=args.min_class_samples,
        )

        print(
            f"model {model.model_id} [{model.symbol}] status={model.status} "
            f"samples={model.training_samples} "
            f"period={model.trained_from}..{model.trained_through}"
        )
        for target, metrics in model.target_metrics.items():
            print(
                f"  {target:<7} n={metrics.samples} pos={metrics.positives} "
                f"auc={_fmt(metrics.roc_auc)} brier={_fmt(metrics.brier)} "
                f"precision={_fmt(metrics.precision)} recall={_fmt(metrics.recall)}"
            )
        print(
            "  legacy candidate only; backtest this exact model_id, then activate "
            "with scripts/activate_shadow_model.py"
        )
    return 0


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


if __name__ == "__main__":
    sys.exit(main())
