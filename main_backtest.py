#!/usr/bin/env python3
"""Date-range replay/backtest entrypoint for Aureon's M5 decision stack.

Examples:
  python main_backtest.py --data data/xau_m5.csv --from 2026-01-01 --to 2026-06-30
  python main_backtest.py --data data/xau_m5.parquet --from 2026-01-01 --to 2026-06-30 --train
  python main_backtest.py --data-dir data/live_candles --symbol XAUUSD --from 2026-07-01 --to 2026-07-31 --train
  python main_backtest.py --mt5 --symbol XAUUSD --from 2026-07-01 --to 2026-07-31 --train

Date-only bounds are interpreted in AUREON_MARKET_TZ. --to is inclusive for a date-only value.
The output is historical reference evidence only and is never auto-promoted into live trading.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from time import perf_counter
from pathlib import Path
from zoneinfo import ZoneInfo

from aureon.config import AureonConfig
from aureon.data.historical_provider import HistoricalDataProvider
from aureon.data.live_candle_archive import read_archive
from aureon.data.mt5_provider import MT5DataProvider
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.models.base import to_utc
from aureon.models.enums import Timeframe
from aureon.services.adaptive_learning import (
    TARGETS,
    select_loss_control_times,
    test_adaptive_reference,
    train_adaptive_reference,
)
from aureon.services.decision_backtest import (
    run_decision_replay,
    simulate_money_outcomes,
    test_reference_model,
    train_reference,
)
from aureon.services.symbol_intelligence_agent import SymbolIntelligenceAgent
from main_observer import default_agents


def _bound(raw: str, tz: str, *, inclusive_end: bool = False) -> datetime:
    zone = ZoneInfo(tz)
    if "T" in raw:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return to_utc(parsed)
    day = datetime.fromisoformat(raw).date()
    dt = datetime.combine(day, time.min, tzinfo=zone)
    if inclusive_end:
        dt += timedelta(days=1)
    return to_utc(dt)


def _archive_date(path: Path, *, symbol: str, timeframe: Timeframe) -> date | None:
    prefix = f"{symbol}_{timeframe.value}_"
    if not path.name.startswith(prefix) or path.suffix.lower() != ".parquet":
        return None
    raw = path.stem[len(prefix):]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _archive_candles(
    root: Path,
    *,
    symbol: str,
    timeframe: Timeframe,
    market_tz: str,
    start: datetime,
    end: datetime,
    warmup_days: int = 14,
    forward_days: int = 2,
) -> tuple[list, list[Path]]:
    """Load daily live-candle archives as one chronological stream.

    Files before the requested start are included only to warm EMA/RSI/MTF state.
    A small tail after the requested end lets the final requested-day setups receive
    their forward 12-bar outcome without pretending those future bars were available
    at decision time.
    """

    if not root.exists():
        raise FileNotFoundError(f"archive directory not found: {root}")

    zone = ZoneInfo(market_tz)
    start_day = start.astimezone(zone).date()
    end_day = (end - timedelta(microseconds=1)).astimezone(zone).date()
    load_from = start_day - timedelta(days=warmup_days)
    load_through = end_day + timedelta(days=forward_days)

    selected: list[tuple[date, Path]] = []
    pattern = f"{symbol}_{timeframe.value}_*.parquet"
    for path in root.glob(pattern):
        archive_day = _archive_date(path, symbol=symbol, timeframe=timeframe)
        if archive_day is not None and load_from <= archive_day <= load_through:
            selected.append((archive_day, path))
    selected.sort(key=lambda item: item[0])

    candles_by_open = {}
    used_files: list[Path] = []
    for archive_day, path in selected:
        try:
            day_candles = read_archive(
                symbol,
                timeframe,
                archive_day,
                market_tz=market_tz,
                root=root,
            )
        except Exception as exc:
            raise RuntimeError(f"failed reading archive {path}: {exc}") from exc
        used_files.append(path)
        for candle in day_candles:
            candles_by_open[candle.open_time.utc] = candle

    candles = [
        candles_by_open[key]
        for key in sorted(candles_by_open)
        if key < end + timedelta(days=forward_days)
    ]
    return candles, used_files


def _elapsed(started: float) -> str:
    seconds = max(0, int(perf_counter() - started))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _fetch_mt5_chunked(
    provider: MT5DataProvider,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    chunk_days: int = 7,
) -> list:
    candles_by_open = {}
    cursor = start
    total_seconds = max(1.0, (end - start).total_seconds())
    part = 0
    while cursor < end:
        part += 1
        chunk_end = min(end, cursor + timedelta(days=chunk_days))
        print(
            f"      MT5 chunk {part}: {cursor.date()} -> {chunk_end.date()} ...",
            flush=True,
        )
        chunk = provider.get_closed_candles(
            symbol,
            Timeframe.M5,
            cursor,
            chunk_end,
        )
        for candle in chunk:
            candles_by_open[candle.open_time.utc] = candle
        done = min(1.0, (chunk_end - start).total_seconds() / total_seconds)
        print(
            f"      fetched {len(candles_by_open):,} candles total "
            f"({done * 100:.0f}% of requested fetch span)",
            flush=True,
        )
        cursor = chunk_end
    return [candles_by_open[key] for key in sorted(candles_by_open)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Aureon chronological M5 decision backtest")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--data", help="single CSV or Parquet M5 candle file")
    source.add_argument(
        "--data-dir",
        help="directory of daily live archives, e.g. data/live_candles",
    )
    source.add_argument(
        "--mt5",
        action="store_true",
        help="fetch closed M5 candles directly from the configured MT5 terminal",
    )
    parser.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD or ISO datetime")
    parser.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD or ISO datetime")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--train", action="store_true", help="fit chronological reference model")
    parser.add_argument(
        "--test-model",
        help="load a prior main_backtest JSON artifact and score this date range without retraining",
    )
    parser.add_argument("--target-move", type=float, default=10.0)
    parser.add_argument("--stop-move", type=float, default=None)
    parser.add_argument("--lot-size", type=float, default=None)
    parser.add_argument("--hold-bars", type=int, default=12)
    parser.add_argument("--contract-size", type=float, default=None)
    parser.add_argument("--model-threshold", type=float, default=0.50)
    parser.add_argument("--min-clean-probability", type=float, default=0.55)
    parser.add_argument("--max-stop-probability", type=float, default=0.35)
    parser.add_argument("--max-adaptive-trades", type=int, default=10)
    parser.add_argument(
        "--learning-hold-bars", type=int, default=864,
        help="future M5 bars used by adaptive +5/+10/+20/+30/+40 learning (864 = 72 market hours)",
    )
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    if args.train and args.test_model:
        parser.error("--train and --test-model are mutually exclusive")
    if (args.lot_size is None) != (args.stop_move is None):
        parser.error("--lot-size and --stop-move must be supplied together")
    if not 0.0 <= args.model_threshold <= 1.0:
        parser.error("--model-threshold must be between 0 and 1")
    if not 0.0 <= args.min_clean_probability <= 1.0:
        parser.error("--min-clean-probability must be between 0 and 1")
    if not 0.0 <= args.max_stop_probability <= 1.0:
        parser.error("--max-stop-probability must be between 0 and 1")
    if args.max_adaptive_trades < 0:
        parser.error("--max-adaptive-trades must be >= 0")
    if args.learning_hold_bars < 12:
        parser.error("--learning-hold-bars must be at least 12")

    started = perf_counter()
    print("[1/6] Loading configuration...", flush=True)
    config = AureonConfig.from_env()
    symbol = (args.symbol or config.symbols[0]).upper()
    config = config.model_copy(
        update={"symbols": (symbol,), "timeframes": (Timeframe.M5,)}
    )

    start = _bound(args.start, config.market_tz)
    end = _bound(args.end, config.market_tz, inclusive_end=("T" not in args.end))

    outcome_tail_days = max(2, ((args.learning_hold_bars * 5 + 1439) // 1440) + 4)
    archive_files: list[Path] = []
    source_label = ""
    broker_economics = None
    account_currency = None
    if args.data_dir:
        print(f"[2/6] Loading archived candles from {args.data_dir}...", flush=True)
        all_candles, archive_files = _archive_candles(
            Path(args.data_dir),
            symbol=symbol,
            timeframe=Timeframe.M5,
            market_tz=config.market_tz,
            start=start,
            end=end,
            forward_days=outcome_tail_days,
        )
        replay_candles = all_candles
        requested_candles = [
            candle for candle in all_candles
            if start <= candle.open_time.utc < end
        ]
        point = SymbolIntelligenceAgent().resolve_tuning(symbol).point
        source_label = f"archive:{args.data_dir}"
    elif args.mt5:
        print("[2/6] Connecting to MT5...", flush=True)
        provider = MT5DataProvider(
            market_tz=config.market_tz,
            login=config.mt5_login,
            password=config.mt5_password,
            server=config.mt5_server,
            terminal_path=config.mt5_terminal_path,
        )
        provider.connect()
        print("      MT5 connected.", flush=True)
        try:
            warmup_start = start - timedelta(days=14)
            outcome_end = end + timedelta(days=outcome_tail_days)
            print(
                f"      fetching {symbol} M5 from {warmup_start.date()} "
                f"through {outcome_end.date()} "
                "(includes warmup + outcome tail)...",
                flush=True,
            )
            replay_candles = _fetch_mt5_chunked(
                provider,
                symbol=symbol,
                start=warmup_start,
                end=outcome_end,
            )
            requested_candles = [
                candle for candle in replay_candles
                if start <= candle.open_time.utc < end
            ]
            point = provider.symbol_info(symbol).point
            broker_economics = provider.symbol_economics(symbol)
            terminal = provider.terminal_info()
            account = provider.account_info()
            account_currency = account.get("currency")
            source_label = (
                f"mt5:{account.get('server') or 'unknown'}"
                f"/{account.get('login') or 'unknown'}"
                f" build={terminal.get('build') or 'unknown'}"
            )
        finally:
            provider.close()
    else:
        print(f"[2/6] Loading historical file {args.data}...", flush=True)
        provider = HistoricalDataProvider(
            args.data,
            symbol=symbol,
            timeframe=Timeframe.M5,
            market_tz=config.market_tz,
        )
        provider.connect()
        replay_candles = provider.get_closed_candles(
            symbol, Timeframe.M5, start, end
        )
        requested_candles = replay_candles
        point = provider.symbol_info(symbol).point
        source_label = f"file:{args.data}"

    if not requested_candles:
        source_name = source_label or args.data_dir or args.data or "MT5"
        print(
            f"No {symbol} M5 candles in requested range from {source_name}.",
            file=sys.stderr,
        )
        return 2

    print(
        f"[3/6] Preparing replay engine for {len(replay_candles):,} candles "
        f"(elapsed {_elapsed(started)})...",
        flush=True,
    )
    engine = AnalysisEngine(
        default_agents(config, symbol=symbol, point=point),
        account_scope=config.account_scope,
        market_tz=config.market_tz,
        mtf_bars=config.mtf_m5_bars,
        mtf_periods=(config.ema_fast, config.ema_slow),
    )
    print("[4/6] Replaying candles through Aureon agents...", flush=True)

    last_percent = -1
    def _replay_progress(done: int, total: int, crosses: int) -> None:
        nonlocal last_percent
        percent = int((done / total) * 100) if total else 100
        bucket = (percent // 5) * 5
        if bucket != last_percent or done == total:
            last_percent = bucket
            print(
                f"      replay {percent:3d}% | {done:,}/{total:,} candles "
                f"| Agent20 crosses {crosses:,} | elapsed {_elapsed(started)}",
                flush=True,
            )

    replay_rows = run_decision_replay(
        candles=replay_candles,
        engine=engine,
        target_move=args.target_move,
        on_progress=_replay_progress,
    )
    rows = [
        row
        for row in replay_rows
        if start <= datetime.fromisoformat(row.at) < end
    ]
    eligible = [row for row in rows if row.eligible]
    reached = [row for row in eligible if row.reached_10]

    model_test = None
    adaptive_model = None
    adaptive_test = None
    if args.train:
        print(
            f"[5/6] Training chronological reference model "
            f"(elapsed {_elapsed(started)})...",
            flush=True,
        )
        reference_model = train_reference(rows)
        adaptive_model = train_adaptive_reference(
            rows,
            replay_candles,
            horizon_bars=args.learning_hold_bars,
        )
    elif args.test_model:
        print(
            f"[5/6] Testing saved model {args.test_model} "
            f"(elapsed {_elapsed(started)})...",
            flush=True,
        )
        model_artifact = json.loads(Path(args.test_model).read_text(encoding="utf-8"))
        if str(model_artifact.get("symbol") or "").upper() != symbol:
            raise ValueError(
                f"saved model symbol {model_artifact.get('symbol')!r} does not match {symbol}"
            )
        reference_model = model_artifact.get("reference_model")
        if not isinstance(reference_model, dict):
            raise ValueError("saved artifact has no reference_model block")
        model_test = test_reference_model(rows, reference_model)
        saved_adaptive = model_artifact.get("adaptive_model")
        if isinstance(saved_adaptive, dict) and saved_adaptive.get("status") == "adaptive_reference_only":
            adaptive_test = test_adaptive_reference(
                rows,
                replay_candles,
                saved_adaptive,
                threshold=args.model_threshold,
            )
    else:
        print(
            f"[5/6] Skipping training/model test (elapsed {_elapsed(started)})...",
            flush=True,
        )
        reference_model = None

    agent20_money = None
    model_money = None
    adaptive_loss_money = None
    adaptive_loss_selected_times: set[str] = set()
    money_contract_size = None
    if args.lot_size is not None and args.stop_move is not None:
        money_contract_size = args.contract_size
        if money_contract_size is None and broker_economics:
            money_contract_size = broker_economics.get("contract_size")
        if not money_contract_size:
            raise ValueError("money simulation needs broker contract size; use --mt5 or --contract-size")
        if broker_economics:
            profit_currency = str(broker_economics.get("currency_profit") or "").upper()
            if profit_currency and profit_currency != "USD":
                raise ValueError(f"USD output requested but broker profit currency is {profit_currency}")
        if account_currency and str(account_currency).upper() != "USD":
            raise ValueError(f"USD output requested but MT5 account currency is {account_currency}")
        agent20_money = simulate_money_outcomes(
            rows, replay_candles, target_move=args.target_move, stop_move=args.stop_move,
            hold_bars=args.hold_bars, lot_size=args.lot_size,
            contract_size=float(money_contract_size),
        )
        if model_test is not None:
            selected_times = {
                item["at"] for item in model_test.get("scored_rows", [])
                if float(item.get("probability_reach_10", 0.0)) >= args.model_threshold
            }
            model_money = simulate_money_outcomes(
                rows, replay_candles, target_move=args.target_move, stop_move=args.stop_move,
                hold_bars=args.hold_bars, lot_size=args.lot_size,
                contract_size=float(money_contract_size), selected_times=selected_times,
            )
        if adaptive_test is not None:
            adaptive_loss_selected_times = select_loss_control_times(
                adaptive_test,
                min_clean_probability=args.min_clean_probability,
                max_stop_probability=args.max_stop_probability,
                max_trades=args.max_adaptive_trades,
            )
            adaptive_loss_money = simulate_money_outcomes(
                rows, replay_candles, target_move=args.target_move, stop_move=args.stop_move,
                hold_bars=args.hold_bars, lot_size=args.lot_size,
                contract_size=float(money_contract_size),
                selected_times=adaptive_loss_selected_times,
            )
    target_misses = len(eligible) - len(reached)
    position_summary = {
        "positions": len(eligible),
        "target_wins": len(reached),
        "target_misses": target_misses,
        "target_win_rate": (len(reached) / len(eligible)) if eligible else None,
        "target_miss_rate": (target_misses / len(eligible)) if eligible else None,
        "gross_target_move": len(reached) * args.target_move,
        "realized_pnl": None,
        "realized_pnl_note": (
            "Not calculated until a deterministic stop/exit rule is configured. "
            "A target miss is not automatically a losing trade."
        ),
    }

    artifact = {
        "schema": "AUREON_DECISION_BACKTEST_V2",
        "historical_reference_only": True,
        "symbol": symbol,
        "timeframe": "M5",
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "candles": len(requested_candles),
        "replay_candles_with_warmup": len(replay_candles),
        "archive_files_loaded": [str(path) for path in archive_files],
        "data_source": source_label,
        "agent20_crosses": len(rows),
        "eligible_crosses": len(eligible),
        "eligible_reached_target": len(reached),
        "target_move": args.target_move,
        "position_summary": position_summary,
        "money_simulation": {
            "currency": "USD" if agent20_money is not None else None,
            "agent20_eligible": agent20_money,
            "saved_model_selected": model_money,
            "adaptive_loss_control_selected": adaptive_loss_money,
            "model_threshold": args.model_threshold if model_test is not None else None,
            "loss_control": {
                "min_clean_probability": args.min_clean_probability,
                "max_stop_probability": args.max_stop_probability,
                "max_adaptive_trades": args.max_adaptive_trades,
                "selected_times": sorted(adaptive_loss_selected_times),
            },
            "broker_economics": broker_economics,
            "account_currency": account_currency,
        },
        "eligibility_rule": {
            "ema_fast": config.ema_fast,
            "ema_slow": config.ema_slow,
            "long": f"bullish cross and RSI < {config.ema_rsi_long_below:g}",
            "short": f"bearish cross and RSI > {config.ema_rsi_short_above:g}",
        },
        "rows": [row.to_dict() for row in rows],
        "reference_model": reference_model if args.train else None,
        "adaptive_model": adaptive_model if args.train else None,
        "tested_model_artifact": args.test_model,
        "model_test": model_test,
        "adaptive_model_test": adaptive_test,
        "note": (
            "Historical replay is reference evidence for matching live scenarios. "
            "It does not assume future live regimes will reproduce historical outcomes."
        ),
    }

    print(f"[6/6] Writing backtest artifact... (elapsed {_elapsed(started)})", flush=True)
    output = Path(args.output) if args.output else Path("data/backtests") / (
        f"agent20_{symbol}_{args.start.replace(':','-')}_{args.end.replace(':','-')}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")

    rate = (len(reached) / len(eligible)) if eligible else None
    print(f"Aureon decision backtest — {symbol} M5")
    print(f"  source               {source_label}")
    if args.data_dir:
        print(f"  parquet files loaded {len(archive_files)}")
    if args.data_dir or args.mt5:
        print(f"  replay candles       {len(replay_candles)} (includes warmup/outcome tail)")
    print(f"  candles in range     {len(requested_candles)}")
    print(f"  EMA/RSI cross rows   {len(rows)}")
    print(f"  eligible             {len(eligible)}")
    print(f"  reached +{args.target_move:g}       {len(reached)}")
    print(f"  historical hit rate  {'—' if rate is None else f'{rate:.1%}'}")
    print("  --- position summary ---")
    print(f"  positions            {position_summary['positions']}")
    print(f"  target wins          {position_summary['target_wins']}")
    print(f"  target misses        {position_summary['target_misses']}")
    target_win_rate = position_summary["target_win_rate"]
    print(
        f"  target win rate      "
        f"{'—' if target_win_rate is None else format(target_win_rate, '.1%')}"
    )
    print(f"  gross target move    +{position_summary['gross_target_move']:.2f}")
    if agent20_money is None:
        print("  realized P&L         — (add --lot-size and --stop-move)")
    else:
        def _money_block(title: str, summary: dict | None) -> None:
            print(f"  --- {title} ---")
            if summary is None:
                print("  trades               0")
                print("  USD made             $0.00")
                print("  USD lost             $0.00")
                print("  NET P&L              $0.00")
                return
            print(f"  trades               {summary['trades']}")
            print(f"  TP wins              {summary['wins']}")
            print(f"  SL losses            {summary['losses']}")
            print(f"  timeouts             {summary['timeouts']}")
            print(f"  ambiguous M5 bars    {summary['ambiguous']}")
            print(f"  profitable exits     {summary['profitable_exits']}")
            print(f"  losing exits         {summary['losing_exits']}")
            print(f"  USD made             +${summary['usd_made']:.2f}")
            print(f"  USD lost             -${summary['usd_lost']:.2f}")
            sign = "+" if summary["net_usd"] >= 0 else ""
            print(f"  NET P&L              {sign}${summary['net_usd']:.2f}")
            print(f"  worst drawdown move  ${summary['max_mae_before_exit']:.2f}")
        print(f"  money assumptions    {args.lot_size:g} lot | TP +${args.target_move:g} | SL -${args.stop_move:g} | max {args.hold_bars} M5 bars")
        _money_block("MONEY RESULTS - ALL AGENT20 ELIGIBLE", agent20_money)
        if args.test_model:
            _money_block(f"MONEY RESULTS - SAVED MODEL >= {args.model_threshold:.2f}", model_money)
        if adaptive_test is not None:
            _money_block(
                "MONEY RESULTS - ADAPTIVE LOSS CONTROL "
                f"(clean>={args.min_clean_probability:.2f}, stop<={args.max_stop_probability:.2f}, "
                f"max {args.max_adaptive_trades})",
                adaptive_loss_money,
            )
    if args.train and adaptive_model is not None:
        print("  --- ADAPTIVE FIVE-OUTPUT MODEL ---")
        print(f"  adaptive status      {adaptive_model.get('status')}")
        print(f"  learning horizon     {adaptive_model.get('horizon_bars', args.learning_hold_bars)} M5 bars")
        for target in TARGETS:
            key = str(int(target))
            block = (adaptive_model.get("target_models") or {}).get(key, {})
            if block.get("status") == "trained":
                chosen = block.get("chosen_model")
                validation = (block.get("validation") or {}).get(chosen, {})
                brier = validation.get("brier")
                auc = validation.get("roc_auc")
                print(f"  P(+{key}) model       {chosen} | brier={brier} | auc={auc}")
            else:
                print(f"  P(+{key}) model       {block.get('status', 'unavailable')}")
        mae = adaptive_model.get("winner_mae_before_10") or {}
        print(f"  winner MAE before +10 median {mae.get('median')} | max {mae.get('max')}")
    if args.test_model and adaptive_test is not None:
        print("  --- ADAPTIVE FIVE-OUTPUT TEST ---")
        print(f"  adaptive samples     {adaptive_test.get('samples')}")
        print(f"  selected >= +5       {adaptive_test.get('selected_for_5')}")
        print(f"  selected >= +10      {adaptive_test.get('selected_for_10')}")
        print(f"  runner >= +20        {adaptive_test.get('runner_candidates_20_plus')}")
        print(f"  loss gate selected   {len(adaptive_loss_selected_times)}")
        print(
            f"  loss gate            clean>={args.min_clean_probability:.2f} | "
            f"stop<={args.max_stop_probability:.2f} | max {args.max_adaptive_trades}"
        )
        loss_metrics = adaptive_test.get("loss_control_metrics") or {}
        clean_metrics = loss_metrics.get("clean_10_before_stop_15") or {}
        stop_metrics = loss_metrics.get("stop_15_before_clean_10") or {}
        if clean_metrics:
            print(
                f"  clean +10<-15 test   brier={clean_metrics.get('brier')} | "
                f"auc={clean_metrics.get('roc_auc')}"
            )
        if stop_metrics:
            print(
                f"  stop -15<+10 test    brier={stop_metrics.get('brier')} | "
                f"auc={stop_metrics.get('roc_auc')}"
            )
        for target in TARGETS:
            key = str(int(target))
            metrics = (adaptive_test.get("metrics_by_target") or {}).get(key)
            if metrics:
                print(f"  P(+{key}) test        brier={metrics.get('brier')} | auc={metrics.get('roc_auc')}")
    if args.train:
        model = artifact["reference_model"] or {}
        print(f"  reference model      {model.get('status', 'unknown')}")
        metrics = model.get("test_metrics") or {}
        if metrics:
            print(f"  OOS samples          {metrics.get('samples')}")
            print(f"  OOS brier            {metrics.get('brier')}")
            print(f"  OOS roc_auc          {metrics.get('roc_auc')}")
    elif args.test_model:
        result = model_test or {}
        metrics = result.get("metrics") or {}
        print(f"  tested saved model   {args.test_model}")
        print(f"  holdout samples      {result.get('samples', 0)}")
        print(f"  holdout accuracy     {metrics.get('accuracy')}")
        print(f"  holdout precision    {metrics.get('precision')}")
        print(f"  holdout recall       {metrics.get('recall')}")
        print(f"  holdout brier        {metrics.get('brier')}")
        print(f"  holdout roc_auc      {metrics.get('roc_auc')}")
        print(f"  model positions      {result.get('predicted_positive_50', 0)}")
        print(f"  model target wins    {result.get('predicted_positive_hits', 0)}")
        print(f"  model target misses  {result.get('predicted_positive_misses', 0)}")
        print(f"  >= 0.50 hit rate     {result.get('predicted_positive_hit_rate')}")
    print(f"  output               {output}")
    print(f"  total elapsed        {_elapsed(started)}")
    print("  NOTE: historical reference only; no live model is auto-activated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
