"""JAN23 execution diagnostics for the existing Phase-5 research run."""
from __future__ import annotations

from typing import Any

TARGETS = (6.0, 10.0, 20.0, 30.0, 40.0)


def diagnose_signal(signal: dict[str, Any], future: list[Any], *, hold_bars: int = 144) -> dict[str, Any]:
    direction = str(signal["direction"]).upper()
    if direction not in {"BUY", "SELL"}:
        raise ValueError(f"unsupported direction: {direction}")
    sign = 1 if direction == "BUY" else -1
    entry = float(signal["entry_price"])
    bars = future[:hold_bars]
    favourable, adverse = [], []
    for bar in bars:
        if sign > 0:
            favourable.append(max(0.0, float(bar.high) - entry))
            adverse.append(max(0.0, entry - float(bar.low)))
        else:
            favourable.append(max(0.0, entry - float(bar.low)))
            adverse.append(max(0.0, float(bar.high) - entry))

    def first(values, target):
        for i, value in enumerate(values, 1):
            if value >= target:
                return i
        return None

    ladder = {}
    for target in TARGETS:
        bars_to = first(favourable, target)
        ladder[str(int(target))] = {"reached": bars_to is not None, "bars_to": bars_to}
    return {
        "sequence_id": signal.get("sequence_id"),
        "snapshot_id": signal.get("snapshot_id"),
        "timestamp": signal.get("timestamp"),
        "stage": signal.get("stage"),
        "direction": direction,
        "entry_price": entry,
        "bars_observed": len(bars),
        "mfe": max(favourable, default=0.0),
        "mae": max(adverse, default=0.0),
        "targets": ladder,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def avg(values):
        return sum(values) / len(values) if values else None

    result = {
        "signals": len(rows),
        "mfe": {"mean": avg([r["mfe"] for r in rows]), "max": max((r["mfe"] for r in rows), default=None)},
        "mae": {"mean": avg([r["mae"] for r in rows]), "max": max((r["mae"] for r in rows), default=None)},
        "targets": {},
        "by_stage": {},
    }
    for target in TARGETS:
        key = str(int(target))
        hits = [r for r in rows if r["targets"][key]["reached"]]
        times = [r["targets"][key]["bars_to"] for r in hits]
        result["targets"][key] = {
            "hits": len(hits),
            "hit_rate": len(hits) / len(rows) if rows else None,
            "mean_bars_to": avg(times),
            "median_bars_to": sorted(times)[len(times) // 2] if times else None,
        }
    for stage in sorted({str(r.get("stage") or "UNKNOWN") for r in rows}):
        subset = [r for r in rows if str(r.get("stage") or "UNKNOWN") == stage]
        result["by_stage"][stage] = {
            "signals": len(subset),
            "mean_mfe": avg([r["mfe"] for r in subset]),
            "mean_mae": avg([r["mae"] for r in subset]),
            "reached_6": sum(r["targets"]["6"]["reached"] for r in subset),
            "reached_10": sum(r["targets"]["10"]["reached"] for r in subset),
            "reached_20": sum(r["targets"]["20"]["reached"] for r in subset),
            "reached_30": sum(r["targets"]["30"]["reached"] for r in subset),
            "reached_40": sum(r["targets"]["40"]["reached"] for r in subset),
        }
    return result
