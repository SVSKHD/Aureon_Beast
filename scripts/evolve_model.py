#!/usr/bin/env python3
"""Run one explicit Aureon V1 model-governance action."""

from __future__ import annotations

import argparse
import logging
import sys

from aureon.config import AureonConfig
from aureon.services.evolution_agent import EvolutionAgent
from aureon.storage.runtime import build_storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True)
    parser.add_argument(
        "action",
        choices=("qualify", "shadow", "evaluate-shadow"),
        help="candidate->challenger, challenger->shadow, or shadow->champion/reject/hold",
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
    evolution = EvolutionAgent(storage.models)

    if args.action == "qualify":
        model = evolution.qualify_candidate(args.model_id)
    elif args.action == "shadow":
        model = evolution.admit_shadow(args.model_id)
    else:
        model = evolution.evaluate_shadow(args.model_id)

    print(
        f"model {model.model_id} [{model.symbol}] status={model.status} "
        f"reason={model.promotion_reason or '—'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
