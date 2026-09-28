#!/usr/bin/env python3
"""Explain JAN23 Phase-5 entry/timeout behaviour without retraining."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aureon.config import AureonConfig
from aureon.models.base import to_utc
from aureon.models.enums import Timeframe
from aureon.services.jan23_execution_diagnostics import diagnose_signal, summarize
from main_backtest import _fetch_mt5_chunked
from scripts.phase5_jan23_pipeline import (
    _build_signals, _json, _jsonl, _phase2_crosses, _stamp, _verify_phase4,
)


def main() -> int:
    p = argparse.ArgumentParser(description="JAN23 Phase-5 execution diagnostics")
    p.add_argument("--artifacts", default="artifacts")
    p.add_argument("--hold-bars", type=int, default=144)
    p.add_argument("--output", default="artifacts/jan23_execution_diagnostics.json")
    args = p.parse_args()
    root = Path(args.artifacts)

    phase2 = _jsonl(root / "phase2_sequences.jsonl")
    train = _jsonl(root / "phase3_jan23_train.jsonl")
    test = _jsonl(root / "phase3_jan23_test.jsonl")
    phase4 = _json(root / "phase4_jan23_challenger_report.json")
    phase5 = _json(root / "phase5_jan23_execution_report.json")
    model = _verify_phase4(phase4, train, test)
    signals = _build_signals(test, _phase2_crosses(phase2), model)
    all_signals = [row for values in signals.values() for row in values]
    if not all_signals:
        raise ValueError("no JAN23 signals available")

    start = min(_stamp(row["timestamp"]) for row in all_signals) - timedelta(minutes=10)
    end = max(_stamp(row["timestamp"]) for row in all_signals) + timedelta(minutes=5 * (args.hold_bars + 10))
    config = AureonConfig.from_env()
    from aureon.data.mt5_provider import MT5DataProvider
    provider = MT5DataProvider(
        market_tz=config.market_tz, login=config.mt5_login,
        password=config.mt5_password, server=config.mt5_server,
        terminal_path=config.mt5_terminal_path,
    )
    print("Fetching raw M5 candles only; no Phase-2 replay...", flush=True)
    provider.connect()
    try:
        candles = _fetch_mt5_chunked(provider, symbol="XAUUSD", start=start, end=end, timeframe=Timeframe.M5)
    finally:
        provider.close()

    diagnostics = {}
    detail = {}
    for name, rows in signals.items():
        diagnosed = []
        for row in rows:
            future = [bar for bar in candles if to_utc(bar.open_time.utc) > _stamp(row["timestamp"])][:args.hold_bars]
            diagnosed.append(diagnose_signal(row, future, hold_bars=args.hold_bars))
        diagnostics[name] = summarize(diagnosed)
        detail[name] = diagnosed

    report = {
        "study": "JAN23_EXECUTION_DIAGNOSTICS",
        "research_only": True,
        "new_phase": False,
        "retraining_performed": False,
        "analysis_engine_replay_performed": False,
        "phase5_acceptance": phase5.get("acceptance"),
        "execution_window": phase5.get("unseen_period"),
        "hold_bars": args.hold_bars,
        "diagnostics": diagnostics,
        "signals": detail,
        "interpretation_guardrail": "Descriptive evidence only. Do not auto-promote, tune on the untouched test partition, or enable live execution.",
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print("✓ JAN23 EXECUTION DIAGNOSTICS COMPLETE")
    print("✓ +6/+10/+20/+30/+40, MFE, MAE AND TIME-TO-TARGET RECORDED")
    print("✓ NO RETRAINING / NO PHASE-2 REPLAY / NO LIVE EXECUTION")
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
