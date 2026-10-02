"""V4 Gold model governance for TODO 081-084."""
from __future__ import annotations
from dataclasses import dataclass, replace
from statistics import mean
from typing import Any
from aureon.models.ema_journey_v4 import V4RemainingMovementExample
from aureon.services.v4_ema_model import predict_remaining_move
from aureon.services.v4_learning_loop import V4CandidateVersion

@dataclass(frozen=True)
class V4GovernancePolicy:
    min_validation_samples: int = 20
    min_base_rate_brier_improvement: float = 0.01
    min_unseen_samples: int = 10
    max_unseen_brier: float = 0.24
    min_shadow_samples: int = 20
    drift_min_samples: int = 20
    drift_max_brier: float = 0.26
    drift_max_brier_degradation: float = 0.04

@dataclass(frozen=True)
class V4GovernedModel:
    candidate: V4CandidateVersion
    status: str = "candidate"
    validation_metrics: dict[str, Any] | None = None
    unseen_metrics: dict[str, Any] | None = None
    shadow_metrics: dict[str, Any] | None = None
    promotion_reason: str = ""

def evaluate_examples(model: V4CandidateVersion, rows: list[V4RemainingMovementExample], target: str = "reached_5") -> dict[str, Any]:
    if not rows:
        return {"samples": 0, "target": target, "sufficient": False}
    probs = [float(predict_remaining_move(model.artifact, row.features)[target]) for row in rows]
    labels = [bool(getattr(row.labels, target)) for row in rows]
    rate = sum(labels) / len(labels)
    brier = mean((p - float(y)) ** 2 for p, y in zip(probs, labels, strict=True))
    base_brier = mean((rate - float(y)) ** 2 for y in labels)
    return {"samples": len(rows), "target": target, "brier": brier, "base_rate": rate, "base_rate_brier": base_brier, "brier_improvement": base_brier - brier, "beats_base_rate": brier < base_brier, "sufficient": True}

class V4Governance:
    def __init__(self, policy: V4GovernancePolicy | None = None) -> None:
        self.policy = policy or V4GovernancePolicy()

    def qualify_candidate(self, model: V4GovernedModel, validation: list[V4RemainingMovementExample]) -> V4GovernedModel:
        self._require(model, "candidate")
        metrics = evaluate_examples(model.candidate, validation)
        if metrics["samples"] < self.policy.min_validation_samples or float(metrics.get("brier_improvement") or 0.0) < self.policy.min_base_rate_brier_improvement:
            return replace(model, status="rejected", validation_metrics=metrics, promotion_reason="candidate validation/base-rate gate failed")
        return replace(model, status="challenger", validation_metrics=metrics, promotion_reason="V4 validation and base-rate gates passed")

    def admit_shadow(self, model: V4GovernedModel, unseen_week: list[V4RemainingMovementExample]) -> V4GovernedModel:
        self._require(model, "challenger")
        if any(row.market_date <= model.candidate.trained_through for row in unseen_week):
            raise ValueError("weekly unseen validation overlaps V4 training period")
        metrics = evaluate_examples(model.candidate, unseen_week)
        passed = metrics["samples"] >= self.policy.min_unseen_samples and float(metrics.get("brier") or 1.0) <= self.policy.max_unseen_brier and float(metrics.get("brier_improvement") or 0.0) >= self.policy.min_base_rate_brier_improvement
        if not passed:
            return replace(model, status="rejected", unseen_metrics=metrics, promotion_reason="weekly unseen validation failed")
        return replace(model, status="shadow", unseen_metrics=metrics, promotion_reason="V4 weekly unseen validation passed")

    def evaluate_shadow(self, model: V4GovernedModel, shadow_rows: list[V4RemainingMovementExample]) -> V4GovernedModel:
        self._require(model, "shadow")
        metrics = evaluate_examples(model.candidate, shadow_rows)
        if metrics["samples"] < self.policy.min_shadow_samples or float(metrics.get("brier_improvement") or 0.0) < self.policy.min_base_rate_brier_improvement:
            return replace(model, status="rejected", shadow_metrics=metrics, promotion_reason="shadow base-rate gate failed")
        return replace(model, status="champion", shadow_metrics=metrics, promotion_reason="V4 Shadow evidence passed")

    @staticmethod
    def _require(model: V4GovernedModel, expected: str) -> None:
        if model.status != expected:
            raise ValueError(f"V4 lifecycle violation: model is {model.status}; expected {expected}")

def monitor_champion_drift(model: V4GovernedModel, recent: list[V4RemainingMovementExample], policy: V4GovernancePolicy | None = None) -> dict[str, Any]:
    if model.status != "champion":
        raise ValueError("V4 drift monitoring requires Champion status")
    policy = policy or V4GovernancePolicy()
    metrics = evaluate_examples(model.candidate, recent)
    if metrics["samples"] < policy.drift_min_samples:
        return {"drifted": False, "action": "wait", "metrics": metrics}
    reference = (model.shadow_metrics or {}).get("brier")
    failures = []
    if metrics.get("brier") is None or float(metrics["brier"]) > policy.drift_max_brier:
        failures.append("absolute_brier")
    if reference is not None and metrics.get("brier") is not None and float(metrics["brier"]) - float(reference) > policy.drift_max_brier_degradation:
        failures.append("brier_degradation")
    if not metrics.get("beats_base_rate", False):
        failures.append("lost_base_rate_edge")
    return {"drifted": bool(failures), "action": "trigger_emergency_retraining_and_review" if failures else "keep", "metrics": metrics, "failures": failures, "auto_promote_replacement": False}
