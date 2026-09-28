"""Candle-path research for EMA-cross and confirmed re-entry signals.

Research-only. Measures first-touch survivability and continuation; it does not
choose a production exit policy or modify Champion/Shadow/live state.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

MOVE_LEVELS = (5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0)
ADVERSE_LEVELS = (5.0, 7.0, 10.0, 15.0)
RSI_BUCKETS = ((0.0, 40.0, "LT40"), (40.0, 50.0, "40_50"), (50.0, 60.0, "50_60"), (60.0, 100.000001, "GE60"))


def _fav_adv(direction: str, entry: float, bar: Any) -> tuple[float, float]:
    if direction == "BUY":
        return max(0.0, float(bar.high) - entry), max(0.0, entry - float(bar.low))
    if direction == "SELL":
        return max(0.0, entry - float(bar.low)), max(0.0, float(bar.high) - entry)
    raise ValueError(f"unsupported direction: {direction}")


def _rsi_bucket(value: Any) -> str:
    if value is None:
        return "MISSING"
    rsi = float(value)
    for lo, hi, name in RSI_BUCKETS:
        if lo <= rsi < hi:
            return name
    return "OUT_OF_RANGE"


def trace_signal(signal: dict[str, Any], future: list[Any], *, hold_bars: int = 288) -> dict[str, Any]:
    direction = str(signal["direction"]).upper()
    entry = float(signal["entry_price"])
    first_favourable = {level: None for level in MOVE_LEVELS}
    first_adverse = {level: None for level in ADVERSE_LEVELS}
    ambiguous = defaultdict(list)
    mfe = mae = 0.0
    mfe_bar = mae_bar = None

    for i, bar in enumerate(future[:hold_bars], 1):
        fav, adv = _fav_adv(direction, entry, bar)
        if fav > mfe:
            mfe, mfe_bar = fav, i
        if adv > mae:
            mae, mae_bar = adv, i
        for level in MOVE_LEVELS:
            if first_favourable[level] is None and fav >= level:
                first_favourable[level] = i
        for level in ADVERSE_LEVELS:
            if first_adverse[level] is None and adv >= level:
                first_adverse[level] = i
        for target in MOVE_LEVELS:
            for stop in ADVERSE_LEVELS:
                if fav >= target and adv >= stop:
                    ambiguous[(target, stop)].append(i)

    survivability = {}
    for target in MOVE_LEVELS:
        target_bar = first_favourable[target]
        survivability[str(int(target))] = {}
        for stop in ADVERSE_LEVELS:
            stop_bar = first_adverse[stop]
            # Same M5 bar is intentionally unresolved, never counted as a clean win.
            clean = target_bar is not None and (stop_bar is None or target_bar < stop_bar)
            same_bar = target_bar is not None and stop_bar is not None and target_bar == stop_bar
            survivability[str(int(target))][str(int(stop))] = {
                "target_first": clean,
                "same_bar_ambiguous": same_bar,
                "target_bar": target_bar,
                "adverse_bar": stop_bar,
            }

    reached10 = first_favourable[10.0]
    continuation_after_10 = max(
        (level for level in MOVE_LEVELS if first_favourable[level] is not None and
         reached10 is not None and first_favourable[level] >= reached10),
        default=0.0,
    )
    return {
        "sequence_id": signal.get("sequence_id"),
        "snapshot_id": signal.get("snapshot_id"),
        "timestamp": signal.get("timestamp"),
        "stage": signal.get("stage"),
        "direction": direction,
        "entry_price": entry,
        "rsi": signal.get("rsi"),
        "rsi_bucket": _rsi_bucket(signal.get("rsi")),
        "bars_observed": min(len(future), hold_bars),
        "mfe": mfe, "mfe_bar": mfe_bar, "mae": mae, "mae_bar": mae_bar,
        "first_favourable": {str(int(k)): v for k, v in first_favourable.items()},
        "first_adverse": {str(int(k)): v for k, v in first_adverse.items()},
        "survivability": survivability,
        "continuation_after_10": continuation_after_10,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def group(items):
        n = len(items)
        moves = {}
        for level in MOVE_LEVELS:
            key = str(int(level))
            reached = [r for r in items if r["first_favourable"][key] is not None]
            clean7 = [r for r in items if r["survivability"][key]["7"]["target_first"]]
            moves[key] = {
                "reached": len(reached), "reached_rate": len(reached) / n if n else None,
                "before_minus7": len(clean7), "before_minus7_rate": len(clean7) / n if n else None,
            }
        return {
            "signals": n,
            "mean_mfe": sum(r["mfe"] for r in items) / n if n else None,
            "mean_mae": sum(r["mae"] for r in items) / n if n else None,
            "moves": moves,
        }

    by_direction = {d: group([r for r in rows if r["direction"] == d]) for d in ("BUY", "SELL")}
    buckets = sorted({r["rsi_bucket"] for r in rows})
    return {
        **group(rows),
        "by_direction": by_direction,
        "by_rsi_bucket": {b: group([r for r in rows if r["rsi_bucket"] == b]) for b in buckets},
        "continuation_after_10": {
            str(int(level)): sum(r["continuation_after_10"] >= level for r in rows)
            for level in MOVE_LEVELS if level >= 10
        },
    }
