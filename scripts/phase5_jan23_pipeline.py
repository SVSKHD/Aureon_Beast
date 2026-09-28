#!/usr/bin/env python3
"""One-command JAN23 Phase-5 pipeline.

Reuses existing Phase-2/3/4 artifacts. Reconstructs the frozen Phase-4 classifier
from the accepted Phase-3 training partition, verifies it against the saved
Phase-4 test metrics, fetches raw M5 candles only (no AnalysisEngine replay), then
builds and evaluates the four Phase-5 execution baselines.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aureon.config import AureonConfig  # noqa: E402
from aureon.models.base import to_utc  # noqa: E402
from aureon.models.enums import Timeframe  # noqa: E402
from aureon.services.phase4_jan23_challenger import (  # noqa: E402
    _classification_metrics, fit_classifier,
)
from aureon.services.phase5_execution_backtest import (  # noqa: E402
    ExecutionAssumptions, JAN23_MODEL_ID, phase5_report, simulate_trade,
)
from main_backtest import _fetch_mt5_chunked  # noqa: E402


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"naive timestamp in research artifact: {value}")
    return parsed


def _close_enough(left, right, tolerance=1e-12) -> bool:
    return left is not None and right is not None and abs(float(left) - float(right)) <= tolerance


def _verify_phase4(report: dict, train: list[dict], test: list[dict]):
    if report.get("model_id") != JAN23_MODEL_ID:
        raise ValueError("Phase-4 report is not JAN23_RESEARCH_CHALLENGER")
    if report.get("research_only") is not True or report.get("production_eligible") is not False:
        raise ValueError("Phase-4 artifact is not research-only")
    model = fit_classifier(train, "decision")
    reproduced = _classification_metrics(test, model)
    expected = report.get("test", {}).get("decision", {})
    if reproduced.get("samples") != expected.get("samples"):
        raise ValueError("Phase-4 reproduction failed: test sample count changed")
    for key in ("accuracy", "multiclass_brier"):
        if not _close_enough(reproduced.get(key), expected.get(key)):
            raise ValueError(f"Phase-4 reproduction failed: {key} changed")
    if reproduced.get("confusion") != expected.get("confusion"):
        raise ValueError("Phase-4 reproduction failed: confusion matrix changed")
    return model


def _phase2_crosses(rows: list[dict]) -> dict[str, dict]:
    result = {}
    for row in rows:
        snapshot = row.get("snapshot") or {}
        sid = snapshot.get("sequence_id")
        if sid:
            result[sid] = {
                "sequence_id": sid,
                "timestamp": snapshot["timestamp"],
                "direction": str(snapshot["direction"]).upper(),
                "entry_price": float(snapshot["cross_price"]),
                "stage": "CROSS",
            }
    return result


def _direction(row: dict) -> str | None:
    value = row.get("labels", {}).get("direction")
    if value is None:
        return None
    value = str(value).upper()
    return value if value in {"BUY", "SELL"} else None


def _entry(row: dict, crosses: dict[str, dict]) -> float | None:
    value = row.get("features", {}).get("entry_price")
    if value is not None:
        return float(value)
    cross = crosses.get(row.get("sequence_id"))
    return float(cross["entry_price"]) if cross else None


def _signal(row: dict, crosses: dict[str, dict], *, model_id: str | None = None) -> dict | None:
    direction = _direction(row)
    entry = _entry(row, crosses)
    if direction is None or entry is None:
        return None
    return {
        "snapshot_id": row.get("snapshot_id"),
        "sequence_id": row.get("sequence_id"),
        "timestamp": row.get("timestamp"),
        "stage": row.get("stage"),
        "direction": direction,
        "entry_price": entry,
        "model_id": model_id,
    }


def _first_per_sequence(rows: list[dict]) -> list[dict]:
    chosen = {}
    for row in sorted(rows, key=lambda item: (item["timestamp"], item.get("sequence_id") or "")):
        chosen.setdefault(row["sequence_id"], row)
    return sorted(chosen.values(), key=lambda item: item["timestamp"])


def _build_signals(test: list[dict], crosses: dict[str, dict], model) -> dict[str, list[dict]]:
    cross_rows = []
    deterministic = []
    confirmation = []
    ml = []
    for row in sorted(test, key=lambda item: (item["timestamp"], item["sequence_id"], item["stage"])):
        signal = _signal(row, crosses)
        if row["stage"] == "CROSS" and signal:
            cross_rows.append(signal)
        if row["stage"] != "CROSS" and signal:
            # Candidate snapshots exist only when deterministic non-EMA evidence
            # was observable at that candle. Do not use the future outcome label.
            deterministic.append(signal)
        if row["stage"] == "REENTRY_CONTINUATION" and signal:
            confirmation.append(signal)

        probs = model.probabilities(row["features"])
        predicted = max(probs, key=probs.get)
        if predicted == "ENTER" and signal:
            signal = dict(signal)
            signal["model_id"] = JAN23_MODEL_ID
            signal["decision_probabilities"] = probs
            ml.append(signal)

    return {
        "original_cross_immediate": _first_per_sequence(cross_rows),
        "deterministic_aureon": _first_per_sequence(deterministic),
        "wait_confirmation": _first_per_sequence(confirmation),
        "sequence_ml": _first_per_sequence(ml),
    }


def _future(candles, timestamp: datetime, hold_bars: int):
    rows = [bar for bar in candles if to_utc(bar.open_time.utc) > timestamp]
    return rows[:hold_bars]


def _execute(signals: list[dict], candles, assumptions: ExecutionAssumptions) -> list[dict]:
    trades = []
    for signal in sorted(signals, key=lambda item: item["timestamp"]):
        future = _future(candles, _stamp(signal["timestamp"]), assumptions.hold_bars)
        if not future:
            continue
        trades.append(simulate_trade(signal, future, assumptions))
    return trades


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="One-command JAN23 Phase-5 pipeline")
    p.add_argument("--artifacts", default="artifacts")
    p.add_argument("--contract-size", type=float, default=100.0)
    p.add_argument("--spread-price", type=float, default=0.20)
    p.add_argument("--slippage-price", type=float, default=0.10)
    p.add_argument("--target-move", type=float, default=10.0)
    p.add_argument("--stop-move", type=float, default=15.0)
    p.add_argument("--hold-bars", type=int, default=144)
    p.add_argument("--output", default="artifacts/phase5_jan23_execution_report.json")
    args = p.parse_args()

    root = Path(args.artifacts)
    paths = {
        "phase2": root / "phase2_sequences.jsonl",
        "train": root / "phase3_jan23_train.jsonl",
        "test": root / "phase3_jan23_test.jsonl",
        "phase4": root / "phase4_jan23_challenger_report.json",
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing required JAN23 artifact(s): " + ", ".join(missing))

    phase2 = _jsonl(paths["phase2"])
    train = _jsonl(paths["train"])
    test = _jsonl(paths["test"])
    phase4 = _json(paths["phase4"])
    model = _verify_phase4(phase4, train, test)
    crosses = _phase2_crosses(phase2)
    signals = _build_signals(test, crosses, model)

    all_signals = [row for rows in signals.values() for row in rows]
    if not all_signals:
        raise ValueError("Phase 5 generated no executable signals")
    earliest = min(_stamp(row["timestamp"]) for row in all_signals) - timedelta(minutes=10)
    latest = max(_stamp(row["timestamp"]) for row in all_signals) + timedelta(
        minutes=5 * (args.hold_bars + 10)
    )

    config = AureonConfig.from_env()
    from aureon.data.mt5_provider import MT5DataProvider
    provider = MT5DataProvider(
        market_tz=config.market_tz, login=config.mt5_login,
        password=config.mt5_password, server=config.mt5_server,
        terminal_path=config.mt5_terminal_path,
    )
    print("Fetching only raw M5 candles required for Phase 5 (no agent replay)...", flush=True)
    provider.connect()
    try:
        candles = _fetch_mt5_chunked(
            provider, symbol="XAUUSD", start=earliest, end=latest, timeframe=Timeframe.M5,
        )
    finally:
        provider.close()
    if not candles:
        raise ValueError("MT5 returned no XAUUSD M5 candles for Phase-5 execution window")

    assumptions = ExecutionAssumptions(
        contract_size=args.contract_size, spread_price=args.spread_price,
        slippage_price=args.slippage_price, target_move=args.target_move,
        stop_move=args.stop_move, hold_bars=args.hold_bars,
    )
    executed = {name: _execute(rows, candles, assumptions) for name, rows in signals.items()}
    for name, rows in executed.items():
        _write_jsonl(root / f"phase5_jan23_{name}.jsonl", rows)

    phase4_raw = paths["phase4"].read_bytes()
    provenance = {
        "model_id": JAN23_MODEL_ID,
        "dataset": phase4.get("dataset"),
        "sha256": hashlib.sha256(phase4_raw).hexdigest(),
        "source": str(paths["phase4"]),
        "reproduction_verified": True,
    }
    evidence_times = [_stamp(row["timestamp"]) for rows in executed.values() for row in rows]
    start_day = min(evidence_times).date().isoformat()
    end_day = max(evidence_times).date().isoformat()
    report = phase5_report(
        baseline_results=executed,
        assumptions=assumptions,
        unseen_period={"from": start_day, "to": end_day},
        phase4_provenance=provenance,
    )
    report["research_period"] = {"from": "2023-01-01", "to": "2023-01-31"}
    report["pipeline"] = {
        "phase4_reproduction_verified": True,
        "raw_m5_only": True,
        "analysis_engine_replay_performed": False,
        "signal_counts": {name: len(rows) for name, rows in signals.items()},
        "executed_counts": {name: len(rows) for name, rows in executed.items()},
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    print("✓ PHASE-4 CHALLENGER REPRODUCED EXACTLY")
    print("✓ PHASE-5 BASELINES GENERATED")
    print("✓ RAW M5 EXECUTION BACKTEST COMPLETE")
    print("✓ NO PHASE-2 AGENT REPLAY PERFORMED")
    print("✓ LIVE EXECUTION / PROMOTION / REGISTRY WRITES DISABLED")
    print("Signals:", json.dumps(report["pipeline"]["signal_counts"], sort_keys=True))
    print(f"Report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
