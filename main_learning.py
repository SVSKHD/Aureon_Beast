#!/usr/bin/env python3
"""Run Aureon's local-first continuous V1 learning sidecar.

This process has no broker execution interface. It periodically:
1. evaluates the active shadow model;
2. trains new candidates only when enough new canonical examples exist;
3. runs chronological walk-forward validation;
4. lets EvolutionAgent move one qualified model into shadow;
5. creates one immutable local recovery snapshot per month and mirrors it to an
   optional Google Drive Desktop folder asynchronously.

Trading does not wait for this process to finish a training or backup cycle.
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
from pathlib import Path

from aureon.config import AureonConfig
from aureon.services.backup_service import BackupService
from aureon.services.learning_cycle import LearningCyclePolicy, LearningCycleService
from aureon.services.shutdown import install_handlers
from aureon.storage.local_database import local_db_path
from aureon.storage.runtime import build_storage

log = logging.getLogger("aureon.learning")


class LearningRunner:
    def __init__(self, service: LearningCycleService, *, interval_seconds: float) -> None:
        self.service = service
        self.interval_seconds = max(60.0, float(interval_seconds))
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            results = self.service.run_once()
            for result in results:
                log.info(
                    "learning_cycle symbol=%s examples=%s action=%s model=%s detail=%s",
                    result.symbol,
                    result.canonical_examples,
                    result.action,
                    result.model_id or "—",
                    result.detail,
                )
            self._stop.wait(self.interval_seconds)

    def stop(self) -> None:
        self._stop.set()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is invalid; using %s", name, raw, default)
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is invalid; using %s", name, raw, default)
        return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="run exactly one local learning/governance/backup cycle and exit",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = AureonConfig.from_env()
    storage = build_storage(
        account_scope=config.account_scope,
        state_heartbeat_seconds=config.state_heartbeat_seconds,
    )

    backtest_dir = Path("data/backtests")
    assets: list[Path] = [local_db_path()]
    if backtest_dir.exists():
        assets.extend(sorted(path for path in backtest_dir.glob("*.json") if path.is_file()))

    safe_config = {
        "account_scope": config.account_scope,
        "market_tz": config.market_tz,
        "symbols": list(config.symbols),
        "timeframes": [timeframe.value for timeframe in config.timeframes],
        "ema_fast": config.ema_fast,
        "ema_slow": config.ema_slow,
        "ema_rsi_long_below": config.ema_rsi_long_below,
        "ema_rsi_short_above": config.ema_rsi_short_above,
        "evaluation_rule_id": config.evaluation_rule_id,
        "learning_contract": {
            "feature_schema": "AUREON_FEATURES_V1",
            "label_schema": "AUREON_CLEAN_MOVE_V1",
            "clean_target": 10.0,
            "clean_max_mae": 7.0,
            "target_ladder": [5, 10, 20, 30, 40],
        },
    }

    service = LearningCycleService(
        training_memory=storage.training_memory,
        models=storage.models,
        symbols=config.symbols,
        policy=LearningCyclePolicy(
            min_samples=_int_env("AUREON_V1_MIN_SAMPLES", 30),
            min_new_examples=_int_env("AUREON_V1_MIN_NEW_EXAMPLES", 10),
            min_train_days=_int_env("AUREON_V1_MIN_TRAIN_DAYS", 20),
            test_days=_int_env("AUREON_V1_TEST_DAYS", 5),
            min_train_samples=_int_env("AUREON_V1_MIN_TRAIN_SAMPLES", 30),
        ),
        backup=BackupService(
            backup_root=os.getenv("AUREON_BACKUP_ROOT", "backups"),
            drive_root=os.getenv("AUREON_GOOGLE_DRIVE_BACKUP_ROOT") or None,
        ),
        backup_assets=tuple(assets),
        backup_config=safe_config,
    )
    if args.once:
        results = service.run_once()
        for result in results:
            print(
                f"{result.symbol}: action={result.action} "
                f"examples={result.canonical_examples} "
                f"model={result.model_id or '—'} detail={result.detail}"
            )
        return 0 if all(result.action != "error" for result in results) else 2

    runner = LearningRunner(
        service,
        interval_seconds=_float_env("AUREON_V1_LEARNING_INTERVAL_SECONDS", 3600.0),
    )
    install_handlers(runner.stop, service="learning")
    log.info(
        "V1 learning sidecar started interval=%ss symbols=%s",
        runner.interval_seconds,
        ",".join(config.symbols),
    )
    runner.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
