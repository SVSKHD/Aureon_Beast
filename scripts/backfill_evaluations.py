#!/usr/bin/env python3
"""Evaluate every historical detection from the Phase 2 replay (Phase 3).

Replays the committed fixture through the observer's agents, tracks outcomes with a
frozen rule, and prints the reached-N report. With ``--write`` it also upserts the
evaluations to Firestore.

Re-running is safe: both the detection ids and the evaluation document ids are pure
functions of the candles that produced them, so a second run overwrites the same
documents with the same content rather than duplicating them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.agents.breakout_agent import BreakoutAgent  # noqa: E402
from aureon.agents.ema_cross_agent import EmaCrossAgent  # noqa: E402
from aureon.agents.liquidity_agent import LiquidityAgent  # noqa: E402
from aureon.agents.rsi_agent import RsiAgent  # noqa: E402
from aureon.agents.session_trend_agent import SessionTrendAgent  # noqa: E402
from aureon.agents.wick_agent import WickAgent  # noqa: E402
from aureon.data.historical_provider import HistoricalDataProvider  # noqa: E402
from aureon.engine.levels import LevelTracker  # noqa: E402
from aureon.evaluation.backfill import (  # noqa: E402
    build_report,
    format_report,
    run_backfill,
)
from aureon.evaluation.rules import get_rule  # noqa: E402

FIXTURE = REPO_ROOT / "aureon" / "data" / "fixtures" / "XAUUSD_M5.csv"
MARKET_TZ = "Europe/Athens"
ACCOUNT_SCOPE = "primary"
POINT = 0.01


def build_agents(*, cross_only: bool, ema_fast: int, ema_slow: int) -> list:
    """The agent roster to replay.

    ``--cross-only`` restricts it to ``ema_cross``, which is what ``EMA_OUTCOME_V1``
    was written for; the default evaluates every directional detection, which is what
    the phase asks for.

    The EMA periods are passed in rather than defaulted, for the reason
    ``EmaCrossAgent`` has no defaults: a backfill that silently used a different pair
    from the observer would produce detections at ids the observer never wrote.
    """
    if cross_only:
        return [EmaCrossAgent(fast_period=ema_fast, slow_period=ema_slow)]
    levels = LevelTracker()  # shared by liquidity and breakout (§15, §17)
    return [
        EmaCrossAgent(fast_period=ema_fast, slow_period=ema_slow),
        RsiAgent(),
        SessionTrendAgent(point=POINT),
        WickAgent(point=POINT),
        LiquidityAgent(point=POINT, level_tracker=levels),
        BreakoutAgent(point=POINT, level_tracker=levels),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rule",
        default=None,
        help="evaluation rule id; defaults to AUREON_EVALUATION_RULE_ID",
    )
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument(
        "--cross-only",
        action="store_true",
        help="evaluate only ema_cross detections",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="upsert the evaluations to Firestore (otherwise report only)",
    )
    args = parser.parse_args(argv)

    # Periods come from the same config the observer reads, so a backfill and the
    # live run cannot disagree about which pair produced a cross.
    from aureon.config import AureonConfig

    config = AureonConfig.from_env()
    rule = get_rule(args.rule or config.evaluation_rule_id)
    provider = HistoricalDataProvider(args.fixture, market_tz=MARKET_TZ)
    candles = provider.candles

    result = run_backfill(
        candles,
        build_agents(
            cross_only=args.cross_only,
            ema_fast=config.ema_fast,
            ema_slow=config.ema_slow,
        ),
        rule,
        account_scope=ACCOUNT_SCOPE,
        market_tz=MARKET_TZ,
        point=POINT,
    )

    print(
        f"replayed {len(candles)} candles -> {len(result.detections)} detections, "
        f"{result.evaluated} evaluated "
        f"({result.skipped_context_only} context-only detections have no direction "
        "and are not evaluable)"
    )
    print()
    print(format_report(build_report(result.evaluations, rule), rule))

    if args.write:
        from aureon.config import AureonConfig
        from aureon.storage.evaluation_repository import EvaluationRepository
        from aureon.storage.firebase_service import get_client

        config = AureonConfig.from_env()
        client = get_client(
            project_id=config.firebase_project_id,
            emulator_host=config.firestore_emulator_host,
        )
        written = EvaluationRepository(client).upsert_many(result.evaluations)
        print(f"\nwrote {written} evaluation document(s) to Firestore")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
