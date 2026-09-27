#!/usr/bin/env python3
"""Build the Phase-2 XAUUSD M5 EMA-sequence research dataset and evidence report."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aureon.config import AureonConfig  # noqa: E402
from aureon.config.sessions import session_for  # noqa: E402
from aureon.engine.analysis_engine import AnalysisEngine  # noqa: E402
from aureon.models.base import to_utc  # noqa: E402
from aureon.models.enums import Timeframe  # noqa: E402
from aureon.services.ema_sequence_labeler import (  # noqa: E402
    EMASequenceConfig,
    EMASequenceLabeler,
)
from aureon.services.market_snapshot import MarketSnapshot  # noqa: E402
from aureon.services.sequence_hypothesis import build_phase2_report  # noqa: E402
from aureon.services.symbol_intelligence_agent import SymbolIntelligenceAgent  # noqa: E402
from main_backtest import _archive_candles, _fetch_mt5_chunked  # noqa: E402
from main_observer import default_agents  # noqa: E402


def _bound(raw: str, tz: str, *, inclusive_end: bool = False) -> datetime:
    zone = ZoneInfo(tz)
    day = datetime.fromisoformat(raw).date()
    result = datetime.combine(day, time.min, tzinfo=zone)
    if inclusive_end:
        result += timedelta(days=1)
    return to_utc(result)


def _detection_row(detection) -> dict:
    return {
        "agent": detection.agent_name,
        "direction": getattr(detection.direction, "value", None),
        "event": detection.event_key,
        "evidence": detection.evidence.model_dump(mode="json"),
    }


def _state_context(candle, engine, snapshot, detections) -> dict:
    read = engine.indicator_read(candle.symbol, candle.timeframe)
    state = snapshot.as_state()
    mtf = engine.mtf_context(candle.symbol, candle.timeframe)
    return {
        "session": session_for(candle.open_time.market).value,
        "rsi_change": (
            read.rsi - read.previous_rsi
            if read.rsi is not None and read.previous_rsi is not None
            else None
        ),
        "atr": read.atr,
        "market_regime": state.get("market_regime"),
        "volatility_regime": (
            state.get("market_regime", {}).get("volatility_state")
            if isinstance(state.get("market_regime"), dict)
            else None
        ),
        "htf_alignment": getattr(getattr(mtf, "alignment", None), "value", None),
        "agent_states": {row.agent_name: row.event_key for row in detections},
        "detections": [_detection_row(row) for row in detections],
    }


def _quality(candles, horizon: int) -> dict:
    opens = [candle.open_time.utc for candle in candles]
    duplicates = len(opens) - len(set(opens))
    chronological = all(left < right for left, right in zip(opens, opens[1:]))
    gaps = []
    for left, right in zip(opens, opens[1:]):
        delta = (right - left).total_seconds() / 60
        if delta > 5 and delta < 60 * 24:
            gaps.append(delta)
    return {
        "candles": len(candles),
        "chronological": chronological,
        "duplicate_open_times": duplicates,
        "intraday_gap_count": len(gaps),
        "max_intraday_gap_minutes": max(gaps, default=0),
        "horizon_bars": horizon,
        "missing_required_horizon": 0,
    }


def replay_sequences(candles, engine, start, end, horizon: int):
    labeler = EMASequenceLabeler(EMASequenceConfig(horizon_bars=horizon))
    snapshot = MarketSnapshot(symbol="XAUUSD")
    detections_by_index = []
    crosses = []
    contexts = []

    for index, candle in enumerate(candles):
        detections = engine.on_closed_candle(candle)
        session = session_for(candle.open_time.market)
        snapshot.observe_candle(
            high=candle.high, low=candle.low, open=candle.open,
            close=candle.close, session=session,
        )
        for detection in detections:
            snapshot.observe(detection)
        context = _state_context(candle, engine, snapshot, detections)
        contexts.append(context)
        detections_by_index.append(detections)
        for detection in detections:
            if detection.agent_name == "ema_cross" and start <= detection.detected_at.utc < end:
                crosses.append((index, detection, context))

    records = []
    missing_horizon = 0
    for index, cross, context in crosses:
        future = candles[index + 1:index + 1 + horizon]
        if len(future) < horizon:
            missing_horizon += 1
            continue
        observable = []
        for future_index in range(index + 1, index + 1 + horizon):
            state = dict(contexts[future_index])
            state["detections"] = [
                _detection_row(row) for row in detections_by_index[future_index]
            ]
            observable.append(state)
        records.append(labeler.label(
            cross_detection=cross,
            frozen_context=context,
            future_candles=future,
            observable_states=observable,
        ))
    return records, missing_horizon


def main() -> int:
    parser = argparse.ArgumentParser(description="Aureon Phase-2 EMA sequence research")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--data")
    source.add_argument("--data-dir")
    source.add_argument("--mt5", action="store_true")
    parser.add_argument("--from", dest="start", default="2023-01-01")
    parser.add_argument("--to", dest="end", default="2026-01-31")
    parser.add_argument("--horizon-bars", type=int, default=144)
    parser.add_argument("--output", default="artifacts/phase2_sequence_report.json")
    parser.add_argument("--dataset", default="artifacts/phase2_sequences.jsonl")
    args = parser.parse_args()

    config = AureonConfig.from_env().model_copy(
        update={"symbols": ("XAUUSD",), "timeframes": (Timeframe.M5,)}
    )
    if (config.ema_fast, config.ema_slow) != (20, 50):
        raise ValueError("Phase 2 requires EMA 20/50 configuration")
    start = _bound(args.start, config.market_tz)
    end = _bound(args.end, config.market_tz, inclusive_end=True)
    warmup_start = start - timedelta(days=14)
    tail_days = max(2, ((args.horizon_bars * 5 + 1439) // 1440) + 4)

    source_meta = {"symbol": "XAUUSD", "timeframe": "M5", "from": args.start, "to": args.end}
    if args.data_dir:
        candles, files = _archive_candles(
            Path(args.data_dir), symbol="XAUUSD", timeframe=Timeframe.M5,
            market_tz=config.market_tz, start=start, end=end,
            warmup_days=14, forward_days=tail_days,
        )
        source_meta.update({"type": "archive", "path": args.data_dir, "files": len(files)})
        point = SymbolIntelligenceAgent().resolve_tuning("XAUUSD").point
    elif args.mt5:
        from aureon.data.mt5_provider import MT5DataProvider

        provider = MT5DataProvider(
            market_tz=config.market_tz, login=config.mt5_login,
            password=config.mt5_password, server=config.mt5_server,
            terminal_path=config.mt5_terminal_path,
        )
        provider.connect()
        try:
            candles = _fetch_mt5_chunked(
                provider, symbol="XAUUSD", start=warmup_start,
                end=end + timedelta(days=tail_days), timeframe=Timeframe.M5,
            )
            point = provider.symbol_info("XAUUSD").point
            source_meta.update({"type": "mt5", "point": point})
        finally:
            provider.close()
    else:
        from aureon.data.historical_provider import HistoricalDataProvider

        provider = HistoricalDataProvider(
            args.data, symbol="XAUUSD", timeframe=Timeframe.M5,
            market_tz=config.market_tz,
        )
        provider.connect()
        candles = provider.get_closed_candles(
            "XAUUSD", Timeframe.M5, warmup_start, end + timedelta(days=tail_days)
        )
        point = provider.symbol_info("XAUUSD").point
        source_meta.update({"type": "file", "path": args.data, "point": point})

    quality = _quality(candles, args.horizon_bars)
    engine = AnalysisEngine(
        default_agents(config, symbol="XAUUSD", point=point),
        account_scope=config.account_scope, market_tz=config.market_tz,
        mtf_bars=config.mtf_m5_bars, mtf_periods=(20, 50),
    )
    records, missing = replay_sequences(candles, engine, start, end, args.horizon_bars)
    quality["missing_required_horizon"] = missing

    report = build_phase2_report(records, source=source_meta, quality=quality)
    dataset_path = Path(args.dataset)
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    dataset_path.write_text(
        "".join(json.dumps(row.model_dump(mode="json"), sort_keys=True) + "\n" for row in records),
        encoding="utf-8",
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report["evidence_gate"], indent=2))
    print(f"sequences={len(records)} dataset={dataset_path} report={output}")
    return 0 if report["evidence_gate"]["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
