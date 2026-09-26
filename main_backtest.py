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
from pathlib import Path
from zoneinfo import ZoneInfo

from aureon.config import AureonConfig
from aureon.data.historical_provider import HistoricalDataProvider
from aureon.data.live_candle_archive import read_archive
from aureon.data.mt5_provider import MT5DataProvider
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.models.base import to_utc
from aureon.models.enums import Timeframe
from aureon.services.decision_backtest import run_decision_replay, train_reference
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
    parser.add_argument("--target-move", type=float, default=10.0)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    config = AureonConfig.from_env()
    symbol = (args.symbol or config.symbols[0]).upper()
    config = config.model_copy(
        update={"symbols": (symbol,), "timeframes": (Timeframe.M5,)}
    )

    start = _bound(args.start, config.market_tz)
    end = _bound(args.end, config.market_tz, inclusive_end=("T" not in args.end))

    archive_files: list[Path] = []
    source_label = ""
    if args.data_dir:
        all_candles, archive_files = _archive_candles(
            Path(args.data_dir),
            symbol=symbol,
            timeframe=Timeframe.M5,
            market_tz=config.market_tz,
            start=start,
            end=end,
        )
        replay_candles = all_candles
        requested_candles = [
            candle for candle in all_candles
            if start <= candle.open_time.utc < end
        ]
        point = SymbolIntelligenceAgent().resolve_tuning(symbol).point
        source_label = f"archive:{args.data_dir}"
    elif args.mt5:
        provider = MT5DataProvider(
            market_tz=config.market_tz,
            login=config.mt5_login,
            password=config.mt5_password,
            server=config.mt5_server,
            terminal_path=config.mt5_terminal_path,
        )
        provider.connect()
        try:
            warmup_start = start - timedelta(days=14)
            outcome_end = end + timedelta(days=2)
            replay_candles = provider.get_closed_candles(
                symbol,
                Timeframe.M5,
                warmup_start,
                outcome_end,
            )
            requested_candles = [
                candle for candle in replay_candles
                if start <= candle.open_time.utc < end
            ]
            point = provider.symbol_info(symbol).point
            terminal = provider.terminal_info()
            account = provider.account_info()
            source_label = (
                f"mt5:{account.get('server') or 'unknown'}"
                f"/{account.get('login') or 'unknown'}"
                f" build={terminal.get('build') or 'unknown'}"
            )
        finally:
            provider.close()
    else:
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

    engine = AnalysisEngine(
        default_agents(config, symbol=symbol, point=point),
        account_scope=config.account_scope,
        market_tz=config.market_tz,
        mtf_bars=config.mtf_m5_bars,
        mtf_periods=(config.ema_fast, config.ema_slow),
    )
    replay_rows = run_decision_replay(
        candles=replay_candles,
        engine=engine,
        target_move=args.target_move,
    )
    rows = [
        row
        for row in replay_rows
        if start <= datetime.fromisoformat(row.at) < end
    ]
    eligible = [row for row in rows if row.eligible]
    reached = [row for row in eligible if row.reached_10]

    artifact = {
        "schema": "AUREON_DECISION_BACKTEST_V1",
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
        "eligibility_rule": {
            "ema_fast": config.ema_fast,
            "ema_slow": config.ema_slow,
            "long": f"bullish cross and RSI < {config.ema_rsi_long_below:g}",
            "short": f"bearish cross and RSI > {config.ema_rsi_short_above:g}",
        },
        "rows": [row.to_dict() for row in rows],
        "reference_model": train_reference(rows) if args.train else None,
        "note": (
            "Historical replay is reference evidence for matching live scenarios. "
            "It does not assume future live regimes will reproduce historical outcomes."
        ),
    }

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
    if args.train:
        model = artifact["reference_model"] or {}
        print(f"  reference model      {model.get('status', 'unknown')}")
        metrics = model.get("test_metrics") or {}
        if metrics:
            print(f"  OOS samples          {metrics.get('samples')}")
            print(f"  OOS brier            {metrics.get('brier')}")
            print(f"  OOS roc_auc          {metrics.get('roc_auc')}")
    print(f"  output               {output}")
    print("  NOTE: historical reference only; no live model is auto-activated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
