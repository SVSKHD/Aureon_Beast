#!/usr/bin/env python3
"""Freeze the V1 baseline: Champion, config, thresholds, exit policy, exams, walk-forward.

    python scripts/freeze_v1_baseline.py --symbol XAUUSD --tag v1.0.0 \\
        --include data/backtests/foundation.json --include data/backtests/exit_policies.json

Writes baselines/<tag>/ with a SHA-256 manifest (verify with scripts/v1_status.py's backup
section or aureon.services.backup_service.verify_manifest). Baselines are immutable: a tag
that already has a manifest is refused. Tag the commit afterwards:

    git tag -a <tag> -m "Aureon V1 baseline <tag>"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aureon.config import AureonConfig  # noqa: E402
from aureon.management.exit_manager import ExitPolicy  # noqa: E402
from aureon.services.backup_service import verify_manifest  # noqa: E402
from aureon.services.session_evidence import git_commit, git_dirty  # noqa: E402
from aureon.services.v1_baseline import freeze_v1_baseline  # noqa: E402
from aureon.services.v1_release import new_ledger, release_context  # noqa: E402
from aureon.storage.runtime import build_storage  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--tag", required=True, help="e.g. v1.0.0")
    parser.add_argument("--root", default=os.getenv("AUREON_BASELINE_ROOT", "baselines"))
    parser.add_argument("--evidence", type=Path, help="reviewed release evidence ledger")
    parser.add_argument("--prepare-evidence", type=Path, help="write pending ledger; do not freeze")
    parser.add_argument("--include", action="append", default=[], help="result file to copy")
    parser.add_argument("--clean-threshold", type=float, default=0.55)
    parser.add_argument("--wait-threshold", type=float, default=0.45)
    args = parser.parse_args(argv)

    commit = git_commit(cwd=REPO_ROOT)
    if not commit or git_dirty(cwd=REPO_ROOT) is not False:
        parser.error("release evidence requires a clean, committed checkout")
    config = AureonConfig.from_env()
    storage = build_storage(
        account_scope=config.account_scope,
        state_heartbeat_seconds=config.state_heartbeat_seconds,
    )
    safe_config = {
        "account_scope": config.account_scope,
        "market_tz": config.market_tz,
        "symbols": list(config.symbols),
        "timeframes": [t.value for t in config.timeframes],
        "ema_fast": config.ema_fast,
        "ema_slow": config.ema_slow,
        "ema_rsi_long_below": config.ema_rsi_long_below,
        "ema_rsi_short_above": config.ema_rsi_short_above,
        "autonomous_management_enabled": config.autonomous_management_enabled,
        "learning_contract": {
            "feature_schema": "AUREON_FEATURES_V1",
            "label_schema": "AUREON_CLEAN_MOVE_V1",
            "model_schema": "AUREON_ENTRY_MODEL_V1",
            "clean_target": 10.0,
            "clean_max_mae": 7.0,
            "target_ladder": [5, 10, 20, 30, 40],
        },
    }
    thresholds = {
        "entry": {"enter_at_clean_10": args.clean_threshold, "wait_at": args.wait_threshold},
        "coverage": {"low_below": 50, "ood_below": 10},
        "evolution": {
            "min_clean_precision": 0.55,
            "max_clean_false_positive_rate": 0.45,
            "min_shadow_samples": 20,
        },
        "one_aureon_position_per_symbol": 1,
    }
    from aureon.config.sessions import (
        SESSION_CONFIG_VERSION,
        SESSION_PRECEDENCE,
        SESSION_WINDOWS,
    )
    from aureon.config.symbol_tuning import setup_tuning, tuning_for
    from aureon.models.enums import SetupFamily
    from aureon.services.daily_market_bias import AGENT_VERSION, DailyBiasPolicy
    from main_observer import default_agents

    safe_config["evaluation_rule_id"] = config.evaluation_rule_id
    safe_config["evaluation_rules"] = dict(config.evaluation_rules)
    safe_config["mtf_m5_bars"] = config.mtf_m5_bars
    safe_config["session_config_version"] = SESSION_CONFIG_VERSION
    safe_config["session_precedence"] = [name.value for name in SESSION_PRECEDENCE]
    safe_config["sessions"] = {
        name.value: {"start": window.start.isoformat(), "end": window.end.isoformat()}
        for name, window in SESSION_WINDOWS.items()
    }
    safe_config["daily_bias"] = {"version": AGENT_VERSION, "policy": DailyBiasPolicy().as_dict()}
    thresholds["symbol_tuning"] = {symbol: asdict(tuning_for(symbol)) for symbol in config.symbols}
    thresholds["setups"] = {
        symbol: {
            family.value: setup_tuning(symbol, family.value).snapshot() for family in SetupFamily
        }
        for symbol in config.symbols
    }
    thresholds["agents"] = {
        symbol: [
            {
                "name": agent.agent_name,
                "version": agent.agent_version,
                "params": agent.params_snapshot(),
            }
            for agent in default_agents(config, symbol=symbol)
        ]
        for symbol in config.symbols
    }
    exit_policy = ExitPolicy.from_env().as_dict()
    if args.prepare_evidence:
        champion = storage.models.champion(args.symbol.upper())
        if champion is None or not champion.artifact:
            parser.error("train, validate and promote a Champion before preparing release evidence")
        context = release_context(
            commit=commit,
            champion=champion,
            config=safe_config,
            thresholds=thresholds,
            exit_policy=exit_policy,
        )
        args.prepare_evidence.parent.mkdir(parents=True, exist_ok=True)
        with args.prepare_evidence.open("x", encoding="utf-8") as handle:
            json.dump(new_ledger(context), handle, indent=2, sort_keys=True)
        print(f"pending evidence ledger: {args.prepare_evidence}; no baseline frozen")
        return 0
    if args.evidence is None:
        parser.error("--evidence is required; use --prepare-evidence to start a pending ledger")
    target = freeze_v1_baseline(
        root=args.root,
        tag=args.tag,
        symbol=args.symbol,
        storage=storage,
        config_snapshot=safe_config,
        exit_policy=exit_policy,
        evidence=args.evidence,
        commit=commit,
        thresholds=thresholds,
        extra_files=args.include,
    )
    verification = verify_manifest(target)
    print(f"baseline {args.tag} written to {target} ({verification.files} files)")
    print(f"manifest verified: {verification.ok} {list(verification.problems) or ''}")
    print(f'next: git tag -a {args.tag} -m "Aureon V1 baseline {args.tag}"')
    return 0 if verification.ok else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as exc:
        print(f"freeze refused: {exc}", file=sys.stderr)
        sys.exit(2)
