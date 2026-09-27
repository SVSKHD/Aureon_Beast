#!/usr/bin/env python3
"""Foundation training and the true-unseen-month workflow for Aureon V1 (GAP 10/11).

Subcommands (dates are examples, never defaults):

  build    replay historical candles through the real agents + Daily Market Bias Agent,
           freeze canonical examples and persist them to training memory
           python scripts/foundation_training.py build --data-dir data/live_candles \\
               --symbol XAUUSD --timeframe M5 --from 2023-01-01 --to 2025-12-31
  train    train V1 candidates on a date range, walk-forward validate, qualify, admit one
           challenger to shadow through EvolutionAgent (never promotes)
           python scripts/foundation_training.py train --symbol XAUUSD \\
               --from 2023-01-01 --to 2026-01-31
  freeze   print/record the current Champion as the frozen model for the next exam
  exam     declare an unseen period the frozen Champion will be examined on
           python scripts/foundation_training.py exam --symbol XAUUSD \\
               --from 2026-02-01 --to 2026-02-28
  score    score the frozen model on the exam period (predictions recorded before scoring)
  release  let a SCORED exam period enter Challenger training
  report   generation comparison between the Champion and the newest challenger/shadow
  coverage print the training-coverage table of the Champion (or all examples)

The sequence FOUNDATION -> ADAPTATION -> FREEZE -> EXAM(Feb) -> SCORE -> RELEASE -> TRAIN ->
FREEZE -> EXAM(Mar) is enforced by exam state: training refuses an unreleased period.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aureon.config import AureonConfig  # noqa: E402
from aureon.models.base import to_utc  # noqa: E402
from aureon.models.enums import Timeframe  # noqa: E402

log = logging.getLogger("aureon.foundation")


def _bound(raw: str, tz: str, *, inclusive_end: bool = False) -> datetime:
    zone = ZoneInfo(tz)
    if "T" in raw:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return to_utc(parsed)
    day = datetime.fromisoformat(raw).date()
    moment = datetime.combine(day, time.min, tzinfo=zone)
    if inclusive_end:
        moment += timedelta(days=1)
    return to_utc(moment)


def _storage(config: AureonConfig):
    from aureon.storage.runtime import build_storage

    return build_storage(
        account_scope=config.account_scope,
        state_heartbeat_seconds=config.state_heartbeat_seconds,
    )


def _load_candles(
    args, config: AureonConfig, symbol: str, timeframe: Timeframe, start, end, tail_days: int
):
    from main_backtest import _archive_candles, _fetch_mt5_chunked

    if args.data_dir:
        candles, _files = _archive_candles(
            Path(args.data_dir),
            symbol=symbol,
            timeframe=timeframe,
            market_tz=config.market_tz,
            start=start,
            end=end,
            forward_days=tail_days,
        )
        point = None
    elif args.mt5:
        from aureon.data.mt5_provider import MT5DataProvider

        provider = MT5DataProvider(
            market_tz=config.market_tz,
            login=config.mt5_login,
            password=config.mt5_password,
            server=config.mt5_server,
            terminal_path=config.mt5_terminal_path,
        )
        provider.connect()
        try:
            candles = _fetch_mt5_chunked(
                provider,
                symbol=symbol,
                start=start - timedelta(days=14),
                end=end + timedelta(days=tail_days),
            )
            point = provider.symbol_info(symbol).point
        finally:
            provider.close()
    else:
        from aureon.data.historical_provider import HistoricalDataProvider

        provider = HistoricalDataProvider(
            args.data, symbol=symbol, timeframe=timeframe, market_tz=config.market_tz
        )
        provider.connect()
        candles = provider.get_closed_candles(
            symbol, timeframe, start - timedelta(days=14), end + timedelta(days=tail_days)
        )
        point = provider.symbol_info(symbol).point
    return candles, point


def cmd_build(args, config: AureonConfig) -> int:
    from aureon.engine.analysis_engine import AnalysisEngine
    from aureon.services.foundation_pipeline import build_canonical_examples, persist_examples
    from aureon.services.symbol_intelligence_agent import SymbolIntelligenceAgent
    from main_observer import default_agents

    symbol = args.symbol.upper()
    timeframe = Timeframe(args.timeframe)
    config = config.model_copy(update={"symbols": (symbol,), "timeframes": (timeframe,)})
    start = _bound(args.start, config.market_tz)
    end = _bound(args.end, config.market_tz, inclusive_end=("T" not in args.end))
    tail_days = max(2, ((args.horizon_bars * timeframe.minutes + 1439) // 1440) + 4)
    candles, point = _load_candles(args, config, symbol, timeframe, start, end, tail_days)
    if not candles:
        print(f"no {symbol} {timeframe.value} candles for the requested range", file=sys.stderr)
        return 2
    point = point or SymbolIntelligenceAgent().resolve_tuning(symbol).point
    engine = AnalysisEngine(
        default_agents(config, symbol=symbol, point=point),
        account_scope=config.account_scope,
        market_tz=config.market_tz,
        mtf_bars=config.mtf_m5_bars,
        mtf_periods=(config.ema_fast, config.ema_slow),
    )
    print(
        f"replaying {len(candles):,} candles for {symbol} {timeframe.value} "
        f"{args.start}..{args.end}",
        flush=True,
    )
    examples, counts = build_canonical_examples(
        candles=candles,
        engine=engine,
        start=start,
        end=end,
        horizon_bars=args.horizon_bars,
        clean_target=args.clean_target,
        clean_max_mae=args.clean_max_mae,
        on_progress=lambda done, total, rows: (
            print(f"  {done:,}/{total:,} candles, {rows} setups", flush=True)
            if done % max(1, total // 10) == 0 or done == total
            else None
        ),
    )
    print(
        f"setups={counts['replay_rows']} eligible={counts['eligible_rows']} "
        f"examples={counts['examples']} clean_10={counts['clean_10']}"
    )
    if args.dry_run:
        return 0
    storage = _storage(config)
    written = persist_examples(storage.training_memory, examples)
    print(f"persisted {written} canonical examples to training memory")
    return 0


def cmd_train(args, config: AureonConfig) -> int:
    from aureon.services.evolution_agent import EvolutionAgent
    from aureon.services.model_backtest import V1WalkForwardBacktester
    from aureon.services.v1_model_training import V1ModelTrainer

    symbol = args.symbol.upper()
    storage = _storage(config)
    trainer = V1ModelTrainer(training_memory=storage.training_memory, models=storage.models)
    candidates = trainer.train_candidates(
        symbol,
        start_market_date=args.start or "0001-01-01",
        end_market_date=args.end or "9999-12-31",
        min_samples=args.min_samples,
    )
    evolution = EvolutionAgent(storage.models)
    backtester = V1WalkForwardBacktester(
        training_memory=storage.training_memory, models=storage.models
    )
    admitted = None
    for candidate in candidates:
        print(
            f"candidate {candidate.model_id} gen={candidate.generation} "
            f"samples={candidate.training_samples}"
        )
        governed = candidate
        if candidate.status == "candidate":
            governed = evolution.qualify_candidate(candidate.model_id)
        print(f"  -> {governed.status}")
        if governed.status != "challenger":
            continue
        backtest = backtester.run(
            symbol,
            model_id=governed.model_id,
            min_train_days=args.min_train_days,
            test_days=args.test_days,
            min_train_samples=args.min_samples,
            start_market_date=args.start or "0001-01-01",
            end_market_date=args.end or "9999-12-31",
        )
        clean = backtest.aggregate_metrics.get("clean_10")
        print(
            f"  walk-forward {backtest.status} folds={len(backtest.folds)} "
            f"clean_10={None if clean is None else clean.model_dump(mode='json')}"
        )
        if backtest.status == "complete" and admitted is None and not args.no_shadow:
            result = evolution.admit_shadow(governed.model_id)
            print(f"  shadow admission -> {result.status}")
            if result.status == "shadow":
                admitted = result
    champion = storage.models.champion(symbol)
    print(
        f"Champion remains {champion.model_id if champion else 'unassigned'}; "
        "promotion only via shadow evidence"
    )
    return 0


def cmd_freeze(args, config: AureonConfig) -> int:
    storage = _storage(config)
    champion = storage.models.champion(args.symbol.upper())
    if champion is None:
        shadow = storage.models.active_shadow(args.symbol.upper())
        print(
            "no Champion to freeze"
            + (f"; shadow {shadow.model_id} awaits promotion" if shadow else "")
        )
        return 2
    print(
        f"FROZEN CHAMPION {champion.model_id} gen={champion.generation} "
        f"trained {champion.trained_from}..{champion.trained_through} "
        f"samples={champion.training_samples}"
    )
    return 0


def cmd_exam(args, config: AureonConfig) -> int:
    from aureon.services.foundation_pipeline import UnseenMonthWorkflow

    storage = _storage(config)
    workflow = UnseenMonthWorkflow(models=storage.models, training_memory=storage.training_memory)
    exam = workflow.open_exam(
        args.symbol.upper(), period_from=args.start, period_to=args.end, model_id=args.model_id
    )
    print(
        f"exam {exam.exam_id} OPEN {exam.period_from}..{exam.period_to} "
        f"frozen={exam.frozen_model_id}"
    )
    print("training now refuses this period until it is scored and released")
    return 0


def cmd_score(args, config: AureonConfig) -> int:
    from aureon.services.foundation_pipeline import UnseenMonthWorkflow

    storage = _storage(config)
    workflow = UnseenMonthWorkflow(models=storage.models, training_memory=storage.training_memory)
    exam = workflow.score_exam(args.exam_id, clean_threshold=args.clean_threshold)
    clean = (exam.metrics.get("by_target") or {}).get("clean_10") or {}
    print(
        f"exam {exam.exam_id} SCORED samples={exam.metrics.get('samples')} "
        f"experience={exam.metrics.get('experience')}"
    )
    print(
        f"  clean_10 precision={clean.get('precision')} recall={clean.get('recall')} "
        f"fpr={clean.get('false_positive_rate')} brier={clean.get('brier')}"
    )
    return 0


def cmd_release(args, config: AureonConfig) -> int:
    from aureon.services.foundation_pipeline import UnseenMonthWorkflow

    storage = _storage(config)
    workflow = UnseenMonthWorkflow(models=storage.models, training_memory=storage.training_memory)
    exam = workflow.release_exam(args.exam_id)
    print(f"exam {exam.exam_id} RELEASED; its period may now enter Challenger training")
    return 0


def cmd_report(args, config: AureonConfig) -> int:
    from aureon.services.evolution_agent import EvolutionAgent

    storage = _storage(config)
    symbol = args.symbol.upper()
    challenger_id = args.challenger
    if challenger_id is None:
        shadow = storage.models.active_shadow(symbol)
        latest = storage.models.latest_model(symbol)
        challenger_id = (shadow or latest).model_id if (shadow or latest) else None
    if challenger_id is None:
        print("no challenger to compare")
        return 2
    examples = storage.training_memory.canonical_between(symbol, "0001-01-01", "9999-12-31")
    report = EvolutionAgent(storage.models).generation_report(
        challenger_model_id=challenger_id, previous_model_id=args.previous, examples=examples
    )
    print(f"previous={report.previous_model_id} challenger={report.challenger_model_id}")
    print(
        f"previous period={report.previous_training_period} new period={report.new_learning_period}"
    )
    print(
        f"samples_before={report.samples_before} samples_added={report.samples_added} "
        f"compared={report.comparison_samples}"
    )
    for target in ("clean_10", "reach_5", "reach_10", "reach_20", "reach_30", "reach_40"):
        before = report.previous_metrics.get(target) or {}
        after = report.challenger_metrics.get(target) or {}
        print(
            f"  {target:<9} "
            + " ".join(
                f"{label} {before.get(key)}->{after.get(key)}"
                for label, key in (
                    ("precision", "precision"),
                    ("recall", "recall"),
                    ("fpr", "false_positive_rate"),
                    ("brier", "brier"),
                    ("logloss", "log_loss"),
                    ("auc", "roc_auc"),
                    ("mae", "average_mae"),
                    ("mfe", "average_mfe"),
                )
            )
        )
    print(
        f"regimes improved={list(report.regimes_improved)} degraded={list(report.regimes_degraded)}"
    )
    print(
        f"sessions improved={list(report.sessions_improved)} "
        f"degraded={list(report.sessions_degraded)}"
    )
    print(
        f"directional improved={list(report.directional_contexts_improved)} "
        f"degraded={list(report.directional_contexts_degraded)}"
    )
    print(f"new failure patterns={list(report.new_failure_patterns)}")
    print(f"previous failure patterns reduced={list(report.previous_failure_patterns_reduced)}")
    print(report.disclaimer)
    return 0


def cmd_coverage(args, config: AureonConfig) -> int:
    from aureon.services.training_coverage import coverage_report, render_coverage

    storage = _storage(config)
    symbol = args.symbol.upper()
    champion = storage.models.champion(symbol)
    report = (champion.validation_metrics or {}).get("training_coverage") if champion else None
    if report is None or args.all_examples:
        examples = storage.training_memory.canonical_between(
            symbol, args.start or "0001-01-01", args.end or "9999-12-31"
        )
        report = coverage_report(examples)
    print(render_coverage(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build")
    source = build.add_mutually_exclusive_group(required=True)
    source.add_argument("--data")
    source.add_argument("--data-dir")
    source.add_argument("--mt5", action="store_true")
    build.add_argument("--symbol", required=True)
    build.add_argument("--timeframe", default="M5")
    build.add_argument("--from", dest="start", required=True)
    build.add_argument("--to", dest="end", required=True)
    build.add_argument("--horizon-bars", type=int, default=864)
    build.add_argument("--clean-target", type=float, default=10.0)
    build.add_argument("--clean-max-mae", type=float, default=7.0)
    build.add_argument("--dry-run", action="store_true")

    train = sub.add_parser("train")
    train.add_argument("--symbol", required=True)
    train.add_argument("--from", dest="start")
    train.add_argument("--to", dest="end")
    train.add_argument("--min-samples", type=int, default=30)
    train.add_argument("--min-train-days", type=int, default=20)
    train.add_argument("--test-days", type=int, default=5)
    train.add_argument("--no-shadow", action="store_true")

    freeze = sub.add_parser("freeze")
    freeze.add_argument("--symbol", required=True)

    exam = sub.add_parser("exam")
    exam.add_argument("--symbol", required=True)
    exam.add_argument("--from", dest="start", required=True)
    exam.add_argument("--to", dest="end", required=True)
    exam.add_argument("--model-id")

    score = sub.add_parser("score")
    score.add_argument("--exam-id", required=True)
    score.add_argument("--clean-threshold", type=float, default=0.55)

    release = sub.add_parser("release")
    release.add_argument("--exam-id", required=True)

    report = sub.add_parser("report")
    report.add_argument("--symbol", required=True)
    report.add_argument("--challenger")
    report.add_argument("--previous")

    coverage = sub.add_parser("coverage")
    coverage.add_argument("--symbol", required=True)
    coverage.add_argument("--from", dest="start")
    coverage.add_argument("--to", dest="end")
    coverage.add_argument("--all-examples", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    config = AureonConfig.from_env()
    handlers = {
        "build": cmd_build,
        "train": cmd_train,
        "freeze": cmd_freeze,
        "exam": cmd_exam,
        "score": cmd_score,
        "release": cmd_release,
        "report": cmd_report,
        "coverage": cmd_coverage,
    }
    return handlers[args.command](args, config)


if __name__ == "__main__":
    sys.exit(main())
