#!/usr/bin/env python3
"""Activate one successfully backtested model artifact in shadow mode."""

from __future__ import annotations

import argparse
import sys

from aureon.config import AureonConfig
from aureon.models.base import utc_now
from aureon.models.learning_v1 import MODEL_SCHEMA_V1, ModelLifecycleStatus
from aureon.services.evolution_agent import EvolutionAgent
from aureon.storage.runtime import build_storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True)
    args = parser.parse_args(argv)

    config = AureonConfig.from_env()
    storage = build_storage(
        account_scope=config.account_scope,
        state_heartbeat_seconds=config.state_heartbeat_seconds,
    )
    model = storage.models.get_model(args.model_id)
    if model is None:
        parser.error(f"no registered model {args.model_id}")
    if model.model_schema_version == MODEL_SCHEMA_V1:
        evolution = EvolutionAgent(storage.models)
        current = model
        if current.status == ModelLifecycleStatus.CANDIDATE.value:
            current = evolution.qualify_candidate(current.model_id)
        if current.status == ModelLifecycleStatus.REJECTED.value:
            parser.error(
                f"model {current.model_id} was rejected by V1 validation policy"
            )
        if current.status == ModelLifecycleStatus.CHALLENGER.value:
            current = evolution.admit_shadow(current.model_id)
        if current.status != ModelLifecycleStatus.SHADOW.value:
            parser.error(
                f"V1 model {current.model_id} is {current.status}; expected shadow"
            )
        backtest = storage.models.latest_backtest_for_model(current.model_id)
        print(
            f"V1 shadow model activated: {current.model_id} [{current.symbol}] "
            f"backtest={None if backtest is None else backtest.backtest_id}"
        )
    else:
        if model.status not in {"candidate", "shadow"}:
            parser.error(
                f"legacy model {model.model_id} is {model.status}, not candidate/shadow"
            )
        backtest = storage.models.latest_backtest_for_model(model.model_id)
        if backtest is None or backtest.status != "complete":
            parser.error(
                "this exact model artifact has no completed walk-forward backtest; "
                "run scripts/backtest_model.py --model-id first"
            )
        activated = storage.models.activate_shadow(model.model_id, at=utc_now())
        print(
            f"legacy shadow model activated: {activated.model_id} [{activated.symbol}] "
            f"backtest={backtest.backtest_id}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
