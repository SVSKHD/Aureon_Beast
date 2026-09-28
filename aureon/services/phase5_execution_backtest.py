"""Phase-5 true execution research backtest.

No live orders. No model promotion. Conservative when M5 cannot establish intrabar order.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

LOT_SIZES = (0.01, 0.05, 0.10, 0.25, 0.50, 1.00)
REQUIRED_BASELINES = {
    "original_cross_immediate", "deterministic_aureon",
    "wait_confirmation", "sequence_ml",
}
JAN23_MODEL_ID = "JAN23_RESEARCH_CHALLENGER"


@dataclass(frozen=True)
class ExecutionAssumptions:
    contract_size: float
    spread_price: float
    slippage_price: float
    stop_move: float = 15.0
    target_move: float = 10.0
    hold_bars: int = 144

    def __post_init__(self):
        for name in ("contract_size", "stop_move", "target_move"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        if self.spread_price < 0 or self.slippage_price < 0:
            raise ValueError("spread/slippage must be >= 0")
        if self.hold_bars < 1:
            raise ValueError("hold_bars must be >= 1")


def _price_pnl(move: float, lot: float, contract_size: float) -> float:
    return move * lot * contract_size


def _parse_timestamp(value: Any) -> datetime:
    if not value:
        raise ValueError("Phase-5 evidence row missing timestamp")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Phase-5 evidence timestamps must be timezone-aware")
    return parsed


def simulate_trade(signal: dict[str, Any], future: list[Any], assumptions: ExecutionAssumptions) -> dict[str, Any]:
    direction = str(signal["direction"]).upper()
    if direction not in {"BUY", "SELL"}:
        raise ValueError(f"unsupported trade direction: {direction!r}")
    sign = 1 if direction == "BUY" else -1
    raw_entry = float(signal["entry_price"])
    entry = raw_entry + sign * (assumptions.spread_price + assumptions.slippage_price)
    result = "TIMEOUT"
    exit_move = 0.0
    exit_bar = None
    ambiguous = False

    for offset, bar in enumerate(future[:assumptions.hold_bars], start=1):
        favourable = (float(bar.high) - entry) if sign > 0 else (entry - float(bar.low))
        adverse = (entry - float(bar.low)) if sign > 0 else (float(bar.high) - entry)
        hit_tp = favourable >= assumptions.target_move
        hit_sl = adverse >= assumptions.stop_move
        if hit_tp and hit_sl:
            result, exit_move, exit_bar, ambiguous = "AMBIGUOUS_AS_LOSS", -assumptions.stop_move, offset, True
            break
        if hit_sl:
            result, exit_move, exit_bar = "LOSS", -assumptions.stop_move, offset
            break
        if hit_tp:
            result, exit_move, exit_bar = "WIN", assumptions.target_move, offset
            break

    if result == "TIMEOUT" and future:
        close = float(future[min(len(future), assumptions.hold_bars) - 1].close)
        exit_move = (close - entry) * sign

    net_move = exit_move - assumptions.slippage_price
    return {
        "signal_id": signal.get("snapshot_id"),
        "sequence_id": signal.get("sequence_id"),
        "timestamp": signal.get("timestamp"),
        "stage": signal.get("stage"),
        "model_id": signal.get("model_id"),
        "direction": direction,
        "raw_entry_price": raw_entry,
        "effective_entry_price": entry,
        "result": result,
        "intrabar_ambiguous": ambiguous,
        "bars_held": exit_bar or min(len(future), assumptions.hold_bars),
        "gross_move": exit_move,
        "net_move": net_move,
        "pnl_by_lot": {
            str(lot): _price_pnl(net_move, lot, assumptions.contract_size) for lot in LOT_SIZES
        },
    }


def _max_drawdown(values: list[float]) -> float:
    equity = peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _validate_evidence(
    baseline_results: dict[str, list[dict[str, Any]]],
    unseen_period: dict[str, str],
    phase4_provenance: dict[str, Any],
) -> None:
    missing = sorted(REQUIRED_BASELINES - set(baseline_results))
    if missing:
        raise ValueError(f"missing Phase-5 baselines: {', '.join(missing)}")
    if phase4_provenance.get("model_id") != JAN23_MODEL_ID:
        raise ValueError("Phase 5 requires JAN23_RESEARCH_CHALLENGER provenance")
    if not phase4_provenance.get("sha256"):
        raise ValueError("Phase 5 requires SHA-256 provenance for the frozen Phase-4 report")
    start = _parse_timestamp(unseen_period["from"] + "T00:00:00+00:00")
    end = _parse_timestamp(unseen_period["to"] + "T23:59:59.999999+00:00")
    for name, rows in baseline_results.items():
        previous = None
        seen = set()
        for row in rows:
            ts = _parse_timestamp(row.get("timestamp"))
            if not start <= ts <= end:
                raise ValueError(f"{name}: evidence timestamp outside declared unseen period")
            if previous is not None and ts < previous:
                raise ValueError(f"{name}: evidence is not chronological")
            previous = ts
            identity = row.get("signal_id") or row.get("snapshot_id")
            if identity and identity in seen:
                raise ValueError(f"{name}: duplicate signal id {identity}")
            if identity:
                seen.add(identity)
            if name == "sequence_ml" and row.get("model_id") != JAN23_MODEL_ID:
                raise ValueError("sequence_ml row is not bound to JAN23_RESEARCH_CHALLENGER")


def summarize(trades: list[dict[str, Any]], assumptions: ExecutionAssumptions) -> dict[str, Any]:
    wins = sum(row["result"] == "WIN" for row in trades)
    losses = sum(row["result"] in {"LOSS", "AMBIGUOUS_AS_LOSS"} for row in trades)
    ambiguous = sum(bool(row["intrabar_ambiguous"]) for row in trades)
    pnl_by_lot = {
        str(lot): sum(float(row["pnl_by_lot"][str(lot)]) for row in trades)
        for lot in LOT_SIZES
    }
    max_drawdown_by_lot = {
        str(lot): _max_drawdown([float(row["pnl_by_lot"][str(lot)]) for row in trades])
        for lot in LOT_SIZES
    }
    results: dict[str, int] = {}
    stages: dict[str, dict[str, Any]] = {}
    for row in trades:
        results[row["result"]] = results.get(row["result"], 0) + 1
        stage = str(row.get("stage") or "UNKNOWN")
        bucket = stages.setdefault(stage, {"trades": 0, "net_move": 0.0})
        bucket["trades"] += 1
        bucket["net_move"] += float(row["net_move"])
    return {
        "trades": len(trades), "wins": wins, "losses": losses,
        "timeouts": sum(row["result"] == "TIMEOUT" for row in trades),
        "ambiguous_as_loss": ambiguous,
        "win_rate": wins / len(trades) if trades else None,
        "net_move": sum(float(row["net_move"]) for row in trades),
        "pnl_by_lot": pnl_by_lot,
        "max_drawdown_by_lot": max_drawdown_by_lot,
        "results": results,
        "by_stage": stages,
        "execution_assumptions": {
            "contract_size": assumptions.contract_size,
            "spread_price": assumptions.spread_price,
            "slippage_price": assumptions.slippage_price,
            "stop_move": assumptions.stop_move,
            "target_move": assumptions.target_move,
            "hold_bars": assumptions.hold_bars,
            "intrabar_rule": "same-M5-bar TP+SL => conservative loss; use M1/tick when available",
        },
    }


def phase5_report(
    *,
    baseline_results: dict[str, list[dict[str, Any]]],
    assumptions: ExecutionAssumptions,
    unseen_period: dict[str, str],
    phase4_provenance: dict[str, Any],
) -> dict[str, Any]:
    _validate_evidence(baseline_results, unseen_period, phase4_provenance)
    return {
        "phase": 5, "research_only": True, "live_execution_allowed": False,
        "champion_promotion_allowed": False,
        "registry_write_allowed": False,
        "unseen_period": unseen_period,
        "phase4_provenance": phase4_provenance,
        "baselines": {
            name: summarize(rows, assumptions) for name, rows in baseline_results.items()
        },
        "acceptance": {
            "status": "PHASE_5_EVIDENCE_READY",
            "note": "Evidence ready for human review; no profitability threshold auto-promotes a model.",
        },
    }
