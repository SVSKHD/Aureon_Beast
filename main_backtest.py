#!/usr/bin/env python3
"""Date-range replay/backtest entrypoint for Aureon's M5 decision stack.

Examples:
  python main_backtest.py --data data/xau_m5.csv --from 2026-01-01 --to 2026-06-30
  python main_backtest.py --data data/xau_m5.parquet --from 2026-01-01 --to 2026-06-30 --train

Date-only bounds are interpreted in AUREON_MARKET_TZ. --to is inclusive for a date-only value.
The output is historical reference evidence only and is never auto-promoted into live trading.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aureon.config import AureonConfig
from aureon.data.historical_provider import HistoricalDataProvider
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.models.base import to_utc
from aureon.models.enums import Timeframe
from aureon.services.decision_backtest import run_decision_replay, train_reference
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Aureon chronological M5 decision backtest")
    parser.add_argument("--data", required=True, help="CSV or Parquet M5 candle file")
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

    provider = HistoricalDataProvider(
        args.data,
        symbol=symbol,
        timeframe=Timeframe.M5,
        market_tz=config.market_tz,
    )
    provider.connect()
    start = _bound(args.start, config.market_tz)
    end = _bound(args.end, config.market_tz, inclusive_end=("T" not in args.end))
    candles = provider.get_closed_candles(symbol, Timeframe.M5, start, end)
    if not candles:
        print("No candles in requested range.", file=sys.stderr)
        return 2

    tuning = provider.symbol_info(symbol)
    engine = AnalysisEngine(
        default_agents(config, symbol=symbol, point=tuning.point),
        account_scope=config.account_scope,
        market_tz=config.market_tz,
        mtf_bars=config.mtf_m5_bars,
        mtf_periods=(config.ema_fast, config.ema_slow),
    )
    rows = run_decision_replay(
        candles=candles,
        engine=engine,
        target_move=args.target_move,
    )
    eligible = [row for row in rows if row.eligible]
    reached = [row for row in eligible if row.reached_10]

    artifact = {
        "schema": "AUREON_DECISION_BACKTEST_V1",
        "historical_reference_only": True,
        "symbol": symbol,
        "timeframe": "M5",
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "candles": len(candles),
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
    print(f"  candles              {len(candles)}")
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
