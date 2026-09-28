"""Phase-3 leakage-safe sequence curriculum construction.

Research only. This module never fits, scores, registers, promotes, shadows, or executes a model.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

FORBIDDEN_FEATURE_KEYS = {
    "future", "outcome", "mfe", "mae", "targets", "reached_6", "reached_10",
    "reached_20", "reached_30", "reached_40", "bars_to_6", "bars_to_10",
    "bars_to_20", "bars_to_30", "bars_to_40", "max_favourable_move",
    "max_adverse_move", "continuation_move", "pullback_move", "pullback_bars",
    "counter_move_outcome", "continuation_candidate_outcome", "path_ambiguous",
}


class DecisionLabel(StrEnum):
    ENTER = "ENTER"
    WAIT = "WAIT"
    INVALIDATE = "INVALIDATE"


class Stage(StrEnum):
    CROSS = "CROSS"
    PULLBACK = "PULLBACK"
    COUNTER_MOVE = "COUNTER_MOVE"
    EXHAUSTION = "EXHAUSTION"
    REENTRY_CONTINUATION = "REENTRY_CONTINUATION"


FEATURE_SCHEMA = (
    "ema_fast", "ema_slow", "ema_fast_slope", "ema_slow_slope",
    "ema_separation", "ema_separation_change", "rsi", "rsi_change", "atr",
    "htf_alignment", "daily_market_bias", "market_regime", "volatility_regime",
    "session", "wick_state", "liquidity_state", "breakout_state",
    "market_structure", "participation", "agent_direction", "agent_confidence",
    "agent_states", "context", "entry_price", "evidence_agents", "evidence",
)


@dataclass(frozen=True)
class Split:
    train: tuple[str, ...]
    validation: tuple[str, ...]
    test: tuple[str, ...]


def _enum(value: Any) -> Any:
    return getattr(value, "value", value)


def _clean_category(value: Any) -> str | None:
    value = _enum(value)
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("value", "state", "regime", "alignment", "direction", "name"):
            if value.get(key) is not None:
                return _clean_category(value[key])
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "unknown", "{}"}:
        return None
    return text.lower()


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items() if k not in FORBIDDEN_FEATURE_KEYS}
    if isinstance(value, list):
        return [_safe(v) for v in value]
    return _enum(value)


def _has_forbidden(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key in FORBIDDEN_FEATURE_KEYS:
                found.append(child_path)
            found.extend(_has_forbidden(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_has_forbidden(child, f"{path}[{index}]"))
    return found


def _targets(ladder: Any) -> dict[str, Any]:
    if ladder is None:
        return {f"reached_{t}": False for t in (6, 10, 20, 30, 40)} | {
            f"bars_to_{t}": None for t in (6, 10, 20, 30, 40)
        }
    return {
        key: getattr(ladder, key)
        for t in (6, 10, 20, 30, 40)
        for key in (f"reached_{t}", f"bars_to_{t}")
    }


def _cross_features(record: Any) -> dict[str, Any]:
    s = record.snapshot
    features = {
        "ema_fast": s.ema_fast, "ema_slow": s.ema_slow,
        "ema_fast_slope": s.ema_fast_slope, "ema_slow_slope": s.ema_slow_slope,
        "ema_separation": s.ema_separation,
        "ema_separation_change": s.ema_separation_change,
        "rsi": s.rsi, "rsi_change": s.rsi_change, "atr": s.atr,
        "htf_alignment": _clean_category(s.htf_alignment),
        "daily_market_bias": _clean_category(s.daily_market_bias),
        "market_regime": _clean_category(s.market_regime),
        "volatility_regime": _clean_category(s.volatility_regime),
        "session": _clean_category(s.session),
        "wick_state": _clean_category(s.wick_state),
        "liquidity_state": _clean_category(s.liquidity_state),
        "breakout_state": _clean_category(s.breakout_state),
        "market_structure": _clean_category(s.market_structure),
        "participation": _clean_category(s.participation),
        "agent_direction": _clean_category(s.agent_direction),
        "agent_confidence": s.agent_confidence,
        "agent_states": _safe(s.agent_states),
        "context": _safe(s.context),
    }
    features.update({"entry_price": None, "evidence_agents": [], "evidence": {}})
    return features


def _candidate_features(candidate: Any) -> dict[str, Any]:
    context = _safe(candidate.context)
    features = {key: None for key in FEATURE_SCHEMA}
    for key in FEATURE_SCHEMA:
        if key in context:
            value = context[key]
            features[key] = _clean_category(value) if key in {
                "htf_alignment", "daily_market_bias", "market_regime",
                "volatility_regime", "session", "wick_state", "liquidity_state",
                "breakout_state", "market_structure", "participation",
                "agent_direction",
            } else value
    features.update({
        "entry_price": candidate.entry_price,
        "evidence_agents": list(candidate.evidence_agents),
        "evidence": _safe(candidate.evidence),
        "context": context,
    })
    return features


def _example(record: Any, stage: Stage, timestamp: Any, direction: Any,
             features: dict[str, Any], decision: DecisionLabel, setup_type: str | None,
             movement: dict[str, Any]) -> dict[str, Any]:
    sid = record.snapshot.sequence_id
    stamp = timestamp.isoformat()
    snapshot_id = hashlib.sha256(f"{sid}|{stage.value}|{stamp}".encode()).hexdigest()[:24]
    return {
        "dataset": "JAN23_RESEARCH",
        "research_only": True,
        "production_eligible": False,
        "champion_promotion_allowed": False,
        "live_execution_allowed": False,
        "sequence_id": sid,
        "snapshot_id": snapshot_id,
        "timestamp": stamp,
        "stage": stage.value,
        "features": features,
        "labels": {
            "decision": decision.value,
            "setup_type": setup_type,
            "direction": _enum(direction) if direction is not None else None,
            **movement,
        },
    }


def build_examples(records: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda r: r.snapshot.timestamp):
        label = record.label
        cross_targets = _targets(label.continuation)
        cross_movement = {
            **cross_targets, "mfe": label.max_favourable_move, "mae": label.max_adverse_move,
        }
        outcome = _enum(label.outcome)
        if outcome == "IMMEDIATE_CONTINUATION":
            decision, setup = DecisionLabel.ENTER, "IMMEDIATE_CONTINUATION"
        elif outcome in {"FAILED_DIRECTION", "AMBIGUOUS_PATH"}:
            decision, setup = DecisionLabel.INVALIDATE, None
        else:
            decision, setup = DecisionLabel.WAIT, None
        rows.append(_example(
            record, Stage.CROSS, record.snapshot.timestamp, record.snapshot.direction,
            _cross_features(record), decision, setup, cross_movement,
        ))

        counter = label.counter_move_candidate
        if counter is not None:
            outcome_row = label.counter_move_outcome
            success = bool(outcome_row and outcome_row.targets.reached_6)
            movement = {
                **_targets(outcome_row.targets if outcome_row else None),
                "mfe": outcome_row.mfe if outcome_row else 0.0,
                "mae": outcome_row.mae if outcome_row else 0.0,
            }
            rows.append(_example(
                record, Stage.COUNTER_MOVE, counter.timestamp, counter.direction,
                _candidate_features(counter),
                DecisionLabel.ENTER if success else DecisionLabel.INVALIDATE,
                "COUNTER_MOVE" if success else None, movement,
            ))

        exhaustion = label.exhaustion_candidate
        if exhaustion is not None:
            rows.append(_example(
                record, Stage.EXHAUSTION, exhaustion.timestamp, exhaustion.direction,
                _candidate_features(exhaustion), DecisionLabel.WAIT, None,
                {**_targets(None), "mfe": None, "mae": None},
            ))

        continuation = label.continuation_candidate
        if continuation is not None:
            outcome_row = label.continuation_candidate_outcome
            success = bool(outcome_row and outcome_row.targets.reached_6)
            movement = {
                **_targets(outcome_row.targets if outcome_row else None),
                "mfe": outcome_row.mfe if outcome_row else 0.0,
                "mae": outcome_row.mae if outcome_row else 0.0,
            }
            rows.append(_example(
                record, Stage.REENTRY_CONTINUATION, continuation.timestamp,
                continuation.direction, _candidate_features(continuation),
                DecisionLabel.ENTER if success else DecisionLabel.INVALIDATE,
                "CONTINUATION" if success else None, movement,
            ))
    return sorted(rows, key=lambda row: (row["timestamp"], row["sequence_id"], row["stage"]))


def chronological_split(records: list[Any]) -> Split:
    ordered = [r.snapshot.sequence_id for r in sorted(records, key=lambda r: r.snapshot.timestamp)]
    n = len(ordered)
    train_end = int(n * 0.70)
    validation_end = int(n * 0.85)
    return Split(
        train=tuple(ordered[:train_end]),
        validation=tuple(ordered[train_end:validation_end]),
        test=tuple(ordered[validation_end:]),
    )


def _label_consistency_failures(examples: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for row in examples:
        labels = row["labels"]
        hits = [bool(labels[f"reached_{target}"]) for target in (6, 10, 20, 30, 40)]
        if any(hits[index] and not hits[index - 1] for index in range(1, len(hits))):
            failures.append(f"{row['snapshot_id']}:non_monotonic_targets")
        for target in (6, 10, 20, 30, 40):
            hit = bool(labels[f"reached_{target}"])
            bars = labels[f"bars_to_{target}"]
            if hit != (bars is not None):
                failures.append(f"{row['snapshot_id']}:target_time_mismatch_{target}")
        if labels["decision"] == "ENTER" and labels["direction"] not in {"BUY", "SELL"}:
            failures.append(f"{row['snapshot_id']}:enter_without_direction")
    return failures


def acceptance_report(records: list[Any], examples: list[dict[str, Any]], split: Split) -> dict[str, Any]:
    ids = [row["snapshot_id"] for row in examples]
    partitions = {"train": set(split.train), "validation": set(split.validation), "test": set(split.test)}
    overlaps = {
        "train_validation": sorted(partitions["train"] & partitions["validation"]),
        "train_test": sorted(partitions["train"] & partitions["test"]),
        "validation_test": sorted(partitions["validation"] & partitions["test"]),
    }
    forbidden = {
        row["snapshot_id"]: _has_forbidden(row["features"])
        for row in examples if _has_forbidden(row["features"])
    }
    decisions = Counter(row["labels"]["decision"] for row in examples)
    directions = Counter(row["labels"]["direction"] for row in examples if row["labels"]["direction"])
    stages = Counter(row["stage"] for row in examples)
    setups = Counter(row["labels"]["setup_type"] for row in examples if row["labels"]["setup_type"])
    failures = []
    schemas = {tuple(sorted(row["features"].keys())) for row in examples}
    expected_schema = tuple(sorted(FEATURE_SCHEMA))
    schema_stable = len(schemas) == 1 and next(iter(schemas), ()) == expected_schema
    label_failures = _label_consistency_failures(examples)
    if not schema_stable: failures.append("feature_schema_not_stable")
    if label_failures: failures.append("labels_not_internally_consistent")
    if len(ids) != len(set(ids)): failures.append("duplicate_snapshot_ids")
    if any(overlaps.values()): failures.append("sequence_partition_overlap")
    if forbidden: failures.append("future_fields_in_features")
    if not {"BUY", "SELL"}.issubset(set(directions)): failures.append("both_directions_not_represented")
    if not {"ENTER", "WAIT", "INVALIDATE"}.issubset(set(decisions)):
        failures.append("decision_classes_not_represented")
    timestamps = [row["timestamp"] for row in examples]
    if timestamps != sorted(timestamps): failures.append("examples_not_chronological")
    return {
        "phase": 3, "dataset": "JAN23_RESEARCH", "research_only": True,
        "production_eligible": False, "champion_promotion_allowed": False,
        "live_execution_allowed": False, "training_performed": False,
        "sequences": len(records), "snapshots": len(examples),
        "decision_counts": dict(decisions), "direction_counts": dict(directions),
        "stage_counts": dict(stages), "setup_type_counts": dict(setups),
        "split_sequence_counts": {k: len(v) for k, v in partitions.items()},
        "negative_examples": sum(
            row["labels"]["decision"] in {"WAIT", "INVALIDATE"} for row in examples
        ),
        "anti_leakage": {
            "forbidden_feature_occurrences": forbidden,
            "partition_overlaps": overlaps,
            "duplicate_snapshot_ids": len(ids) - len(set(ids)),
            "feature_schema_stable": schema_stable,
            "label_consistency_failures": label_failures,
        },
        "acceptance_gate": {
            "status": "PHASE_3_CURRICULUM_PASS" if not failures else "STOP",
            "anti_leakage": "PASS" if not forbidden and not any(overlaps.values()) else "FAIL",
            "failures": failures,
        },
    }



class Phase3SequenceCurriculum:
    """Explicit Phase-3 research façade; intentionally contains no model-training API."""

    dataset = "JAN23_RESEARCH"
    research_only = True
    production_eligible = False
    champion_promotion_allowed = False
    live_execution_allowed = False

    def build(self, records: list[Any]) -> tuple[list[dict[str, Any]], Split, dict[str, Any]]:
        examples = build_examples(records)
        split = chronological_split(records)
        report = acceptance_report(records, examples, split)
        return examples, split, report
