"""Phase-2 historical hypothesis validation for EMA sequence intelligence.

Pure research aggregation: no fitting, model registry writes, promotion or execution.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import mean, median
from typing import Any

from aureon.models.enums import Direction
from aureon.models.sequence_v1 import EMASequenceRecord

TARGET_NAMES = ("6", "10", "20", "30", "40")


@dataclass(frozen=True)
class EvidenceGate:
    """Explicit, auditable gate. No arbitrary profitability threshold is invented."""

    min_sequences: int = 100
    require_both_directions: bool = True
    max_ambiguous_fraction: float = 0.20


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    fraction = pos - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    data = [float(value) for value in values]
    return {
        "n": len(data),
        "mean": mean(data) if data else None,
        "median": median(data) if data else None,
        "p25": percentile(data, 0.25),
        "p50": percentile(data, 0.50),
        "p75": percentile(data, 0.75),
        "p90": percentile(data, 0.90),
    }


def ladder_rates(outcomes: Iterable[Any]) -> dict[str, dict[str, float | int | None]]:
    rows = list(outcomes)
    result = {}
    for target in TARGET_NAMES:
        reached = sum(bool(getattr(row.targets, f"reached_{target}")) for row in rows)
        times = [
            getattr(row.targets, f"bars_to_{target}")
            for row in rows
            if getattr(row.targets, f"bars_to_{target}") is not None
        ]
        result[target] = {
            "eligible": len(rows),
            "hits": reached,
            "hit_rate": reached / len(rows) if rows else None,
            "bars_to_target": distribution(times),
        }
    return result


def _movement_summary(records: list[EMASequenceRecord]) -> dict[str, Any]:
    return {
        "sequences": len(records),
        "mfe": distribution([row.label.max_favourable_move for row in records]),
        "mae": distribution([row.label.max_adverse_move for row in records]),
        "pullback_depth": distribution([row.label.pullback_move for row in records]),
        "pullback_duration_bars": distribution(
            [row.label.pullback_bars for row in records if row.label.pullback_bars is not None]
        ),
    }


def _candidate_fields(prefix: str) -> tuple[str, str]:
    """Resolve candidate/outcome fields without duplicating the candidate suffix."""
    candidate_field = prefix if prefix.endswith("_candidate") else f"{prefix}_candidate"
    return candidate_field, f"{candidate_field}_outcome"


def _candidate_summary(records: list[EMASequenceRecord], prefix: str) -> dict[str, Any]:
    candidate_field, outcome_field = _candidate_fields(prefix)
    outcomes = [
        getattr(row.label, outcome_field)
        for row in records
        if getattr(row.label, outcome_field) is not None
    ]
    candidates = [
        getattr(row.label, candidate_field)
        for row in records
        if getattr(row.label, candidate_field) is not None
    ]
    return {
        "candidates": len(candidates),
        "candidate_rate": len(candidates) / len(records) if records else None,
        "mfe": distribution([row.mfe for row in outcomes]),
        "mae": distribution([row.mae for row in outcomes]),
        "targets": ladder_rates(outcomes),
    }


def _evidence_association(records: list[EMASequenceRecord], prefix: str) -> dict[str, Any]:
    candidate_field, outcome_field = _candidate_fields(prefix)
    rows: dict[str, Counter] = defaultdict(Counter)
    for record in records:
        candidate = getattr(record.label, candidate_field)
        outcome = getattr(record.label, outcome_field)
        if candidate is None or outcome is None:
            continue
        success = bool(outcome.targets.reached_6)
        for agent in candidate.evidence_agents:
            rows[agent]["candidates"] += 1
            rows[agent]["reached_6"] += int(success)
    return {
        agent: {
            "candidates": counts["candidates"],
            "reached_6": counts["reached_6"],
            "reached_6_rate": (
                counts["reached_6"] / counts["candidates"] if counts["candidates"] else None
            ),
        }
        for agent, counts in sorted(rows.items())
    }


def _group(records: list[EMASequenceRecord], key) -> dict[str, Any]:
    grouped: dict[str, list[EMASequenceRecord]] = defaultdict(list)
    for record in records:
        grouped[str(key(record) or "unknown")].append(record)
    return {name: _movement_summary(rows) for name, rows in sorted(grouped.items())}


def _entry_comparison(records: list[EMASequenceRecord]) -> dict[str, Any]:
    reentries = [
        row for row in records if row.label.continuation_candidate_outcome is not None
    ]
    return {
        "ema_cross_entry": _movement_summary(records),
        "confirmed_reentry": {
            **_candidate_summary(reentries, "continuation_candidate"),
            "coverage_of_all_crosses": len(reentries) / len(records) if records else None,
        },
        "note": (
            "Movement comparison only; no spread/slippage or trade P&L is claimed in Phase 2. "
            "Phase 5 owns true execution economics."
        ),
    }


def build_phase2_report(
    records: list[EMASequenceRecord],
    *,
    source: dict[str, Any],
    quality: dict[str, Any],
    gate: EvidenceGate | None = None,
) -> dict[str, Any]:
    gate = gate or EvidenceGate()
    outcome_counts = Counter(row.label.outcome.value for row in records)
    direction_counts = Counter(row.snapshot.direction.value for row in records)
    ambiguous = outcome_counts.get("AMBIGUOUS_PATH", 0)
    ambiguous_fraction = ambiguous / len(records) if records else 1.0

    failures = []
    if len(records) < gate.min_sequences:
        failures.append(f"sequence_count<{gate.min_sequences}")
    if gate.require_both_directions and not (
        direction_counts.get(Direction.BUY.value) and direction_counts.get(Direction.SELL.value)
    ):
        failures.append("both_directions_not_covered")
    if ambiguous_fraction > gate.max_ambiguous_fraction:
        failures.append("ambiguous_fraction_above_gate")
    if not quality.get("chronological", False):
        failures.append("candles_not_strictly_chronological")
    if quality.get("duplicate_open_times", 0):
        failures.append("duplicate_open_times")
    if quality.get("missing_required_horizon", 0):
        failures.append("missing_required_horizon")
    if not quality.get("full_research_period_covered", False):
        failures.append("full_2023_through_jan_2026_period_not_covered")
    if not quality.get("source_fingerprint"):
        failures.append("source_fingerprint_missing")

    counter_records = [row for row in records if row.label.counter_move_candidate is not None]
    continuation_records = [
        row for row in records if row.label.continuation_candidate is not None
    ]

    report = {
        "phase": 2,
        "research_only": True,
        "ml_training_allowed": False,
        "source": source,
        "data_quality": quality,
        "sequence_count": len(records),
        "direction_counts": dict(direction_counts),
        "outcome_counts": dict(outcome_counts),
        "outcome_rates": {
            key: value / len(records) if records else None
            for key, value in sorted(outcome_counts.items())
        },
        "pullback": {
            "depth": distribution([row.label.pullback_move for row in records]),
            "duration_bars": distribution(
                [row.label.pullback_bars for row in records if row.label.pullback_bars is not None]
            ),
        },
        "sequence_excursions": _movement_summary(records),
        "counter_move": {
            "all": _candidate_summary(records, "counter_move"),
            "buy_cross_to_sell": _candidate_summary(
                [row for row in records if row.snapshot.direction is Direction.BUY],
                "counter_move",
            ),
            "sell_cross_to_buy": _candidate_summary(
                [row for row in records if row.snapshot.direction is Direction.SELL],
                "counter_move",
            ),
            "agent_evidence": _evidence_association(counter_records, "counter_move"),
        },
        "continuation_reentry": {
            "all": _candidate_summary(records, "continuation_candidate"),
            "agent_evidence": _evidence_association(
                continuation_records, "continuation_candidate"
            ),
        },
        "immediate_vs_post_pullback": {
            "immediate": _movement_summary(
                [row for row in records if row.label.outcome.value == "IMMEDIATE_CONTINUATION"]
            ),
            "post_pullback": _movement_summary(
                [
                    row for row in records
                    if row.label.outcome.value in {
                        "PULLBACK_THEN_CONTINUATION",
                        "DEEP_PULLBACK_THEN_CONTINUATION",
                    }
                ]
            ),
        },
        "cross_vs_wait_for_reentry": _entry_comparison(records),
        "breakdowns": {
            "direction": _group(records, lambda row: row.snapshot.direction.value),
            "session": _group(records, lambda row: row.snapshot.session),
            "regime": _group(records, lambda row: row.snapshot.market_regime),
            "volatility": _group(records, lambda row: row.snapshot.volatility_regime),
            "htf_alignment": _group(records, lambda row: row.snapshot.htf_alignment),
            "daily_bias": _group(records, lambda row: row.snapshot.daily_market_bias),
        },
    }
    report["evidence_gate"] = {
        "status": "DATA_QUALITY_PASS" if not failures else "STOP",
        "failures": failures,
        "criteria": {
            "min_sequences": gate.min_sequences,
            "require_both_directions": gate.require_both_directions,
            "max_ambiguous_fraction": gate.max_ambiguous_fraction,
        },
        "hypothesis_supported": None,
        "phase3_approved": False,
        "reason": (
            "Statistical support must be explicitly reviewed after the report is generated; "
            "a data-quality PASS alone never auto-approves ML training."
        ),
    }
    return report
