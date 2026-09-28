"""Phase-4 JAN23 research challenger.

Consumes only accepted Phase-3 artifacts. Never writes Champion/Shadow/live state.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

DECISIONS = ("ENTER", "WAIT", "INVALIDATE")
TARGETS = (6, 10, 20, 30, 40)


@dataclass
class CategoricalNB:
    classes: tuple[str, ...]
    class_counts: dict[str, int]
    feature_counts: dict[str, dict[str, Counter]]
    feature_values: dict[str, set[str]]

    def probabilities(self, features: dict[str, Any]) -> dict[str, float]:
        total = sum(self.class_counts.values())
        logs = {}
        for cls in self.classes:
            logs[cls] = math.log((self.class_counts.get(cls, 0) + 1) / (total + len(self.classes)))
            for key, value in features.items():
                token = _token(value)
                values = max(1, len(self.feature_values.get(key, set())))
                count = self.feature_counts.get(key, {}).get(cls, Counter()).get(token, 0)
                denom = self.class_counts.get(cls, 0) + values
                logs[cls] += math.log((count + 1) / denom)
        peak = max(logs.values())
        exps = {cls: math.exp(value - peak) for cls, value in logs.items()}
        denom = sum(exps.values())
        return {cls: value / denom for cls, value in exps.items()}


def _token(value: Any) -> str:
    if value is None:
        return "<MISSING>"
    if isinstance(value, float):
        return f"{value:.3g}"
    if isinstance(value, (dict, list)):
        import json
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def fit_classifier(rows: list[dict[str, Any]], label_key: str) -> CategoricalNB:
    class_counts = Counter(str(row["labels"][label_key]) for row in rows)
    classes = tuple(sorted(class_counts))
    feature_counts: dict[str, dict[str, Counter]] = {}
    feature_values: dict[str, set[str]] = {}
    for row in rows:
        cls = str(row["labels"][label_key])
        for key, value in row["features"].items():
            token = _token(value)
            feature_values.setdefault(key, set()).add(token)
            feature_counts.setdefault(key, {}).setdefault(cls, Counter())[token] += 1
    return CategoricalNB(classes, dict(class_counts), feature_counts, feature_values)


def _classification_metrics(rows: list[dict[str, Any]], model: CategoricalNB) -> dict[str, Any]:
    confusion = {actual: {pred: 0 for pred in DECISIONS} for actual in DECISIONS}
    correct = 0
    brier = 0.0
    for row in rows:
        actual = row["labels"]["decision"]
        probs = model.probabilities(row["features"])
        pred = max(probs, key=probs.get)
        if actual in confusion and pred in confusion[actual]:
            confusion[actual][pred] += 1
        correct += int(pred == actual)
        brier += sum((probs.get(cls, 0.0) - (1.0 if actual == cls else 0.0)) ** 2 for cls in DECISIONS)
    return {
        "samples": len(rows),
        "accuracy": correct / len(rows) if rows else None,
        "multiclass_brier": brier / len(rows) if rows else None,
        "confusion": confusion,
    }


def _binary_rate_model(train: list[dict[str, Any]], target: int) -> dict[str, float]:
    by_stage: dict[str, list[bool]] = {}
    key = f"reached_{target}"
    for row in train:
        if row["labels"].get("mfe") is None:
            continue
        by_stage.setdefault(row["stage"], []).append(bool(row["labels"][key]))
    return {
        stage: (sum(values) + 1) / (len(values) + 2)
        for stage, values in by_stage.items()
    }


def _movement_metrics(rows: list[dict[str, Any]], rates: dict[int, dict[str, float]]) -> dict[str, Any]:
    report = {}
    for target in TARGETS:
        key = f"reached_{target}"
        eligible = [row for row in rows if row["labels"].get("mfe") is not None]
        if not eligible:
            report[str(target)] = {"samples": 0, "brier": None}
            continue
        errors = []
        for row in eligible:
            probability = rates[target].get(row["stage"], 0.5)
            actual = 1.0 if row["labels"][key] else 0.0
            errors.append((probability - actual) ** 2)
        report[str(target)] = {"samples": len(eligible), "brier": sum(errors) / len(errors)}
    return report


def train_jan23_challenger(
    train: list[dict[str, Any]],
    validation: list[dict[str, Any]],
    test: list[dict[str, Any]],
    *,
    phase3_report: dict[str, Any],
) -> dict[str, Any]:
    gate = phase3_report.get("acceptance_gate") or {}
    if gate.get("status") != "PHASE_3_CURRICULUM_PASS" or gate.get("anti_leakage") != "PASS":
        raise ValueError("Phase 4 blocked: Phase-3 curriculum/anti-leakage gate has not passed")
    if not train or not validation or not test:
        raise ValueError("Phase 4 requires non-empty chronological train/validation/test partitions")

    decision_model = fit_classifier(train, "decision")
    rates = {target: _binary_rate_model(train, target) for target in TARGETS}
    return {
        "phase": 4,
        "model_id": "JAN23_RESEARCH_CHALLENGER",
        "dataset": "JAN23_RESEARCH",
        "research_only": True,
        "production_eligible": False,
        "champion_promotion_allowed": False,
        "live_execution_allowed": False,
        "registry_write_allowed": False,
        "decision_model": {
            "type": "categorical_naive_bayes_reference",
            "classes": list(decision_model.classes),
        },
        "validation": {
            "decision": _classification_metrics(validation, decision_model),
            "movement_potential": _movement_metrics(validation, rates),
        },
        "test": {
            "decision": _classification_metrics(test, decision_model),
            "movement_potential": _movement_metrics(test, rates),
        },
        "movement_rate_reference": {str(k): v for k, v in rates.items()},
        "note": (
            "JAN23 research challenger only. Test is evaluated once after fitting on train; "
            "no Champion/Shadow/live model state is modified."
        ),
    }
