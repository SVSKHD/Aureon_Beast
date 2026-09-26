"""Model-governance agent for Aureon V1.

EvolutionAgent never predicts the market and never executes. It moves model artifacts through
candidate -> challenger -> shadow -> champion only when chronological validation and persistent
shadow evidence satisfy explicit gates.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from aureon.ml.logistic import binary_metrics
from aureon.models.base import to_utc, utc_now
from aureon.models.learning_v1 import (
    EvolutionDecision,
    ModelLifecycleStatus,
)
from aureon.models.ml import ModelRegistryEntry


@dataclass(frozen=True)
class EvolutionPolicy:
    min_validation_samples: int = 20
    min_clean_precision: float = 0.55
    max_clean_false_positive_rate: float = 0.45
    max_brier_degradation_vs_champion: float = 0.02
    min_shadow_samples: int = 20
    min_shadow_clean_precision: float = 0.55
    max_shadow_false_positive_rate: float = 0.40
    min_precision_improvement_for_promotion: float = 0.0
    max_brier_degradation_for_promotion: float = 0.0


class EvolutionAgent:
    """Govern model lifecycle with persistent, explainable decisions."""

    agent_name = "evolution_agent"
    agent_version = "1.0.0"

    def __init__(
        self,
        models: Any,
        *,
        policy: EvolutionPolicy | None = None,
        now: Any = utc_now,
    ) -> None:
        self.models = models
        self.policy = policy or EvolutionPolicy()
        self._now = now

    def qualify_candidate(self, model_id: str) -> ModelRegistryEntry:
        model = self._require(model_id, ModelLifecycleStatus.CANDIDATE)
        clean = model.target_metrics.get("clean_10")
        if clean is None:
            return self._reject(model, "candidate has no clean_10 validation metrics")
        reasons = self._validation_failures(clean)
        champion = self.models.champion(model.symbol)
        if champion is not None:
            champion_clean = champion.target_metrics.get("clean_10")
            if (
                champion_clean is not None
                and clean.brier is not None
                and champion_clean.brier is not None
                and clean.brier
                > champion_clean.brier + self.policy.max_brier_degradation_vs_champion
            ):
                reasons.append(
                    "clean_10 Brier is materially worse than current Champion"
                )
        if reasons:
            return self._reject(model, "; ".join(reasons))

        updated = self.models.set_status(
            model.model_id,
            ModelLifecycleStatus.CHALLENGER,
        )
        self._record(
            updated,
            action="candidate_qualified",
            reason="chronological validation gates passed; candidate became challenger",
            metrics=self._clean_metrics(clean),
        )
        return updated

    def admit_shadow(self, model_id: str) -> ModelRegistryEntry:
        model = self._require(model_id, ModelLifecycleStatus.CHALLENGER)
        backtest = self.models.latest_backtest_for_model(model.model_id)
        if backtest is None or backtest.status != "complete":
            raise ValueError(
                "challenger cannot enter shadow without a completed walk-forward backtest"
            )
        clean = backtest.aggregate_metrics.get("clean_10")
        if clean is None:
            raise ValueError("walk-forward backtest has no clean_10 metrics")
        failures = self._validation_failures(clean)
        if failures:
            rejected = self._reject(
                model,
                "walk-forward rejection: " + "; ".join(failures),
            )
            return rejected

        activated = self.models.activate_shadow(model.model_id, at=to_utc(self._now()))
        self._record(
            activated,
            action="entered_shadow",
            reason="completed walk-forward validation passed clean_10 gates",
            metrics=self._clean_metrics(clean),
        )
        return activated

    def evaluate_shadow(self, model_id: str) -> ModelRegistryEntry:
        model = self._require(model_id, ModelLifecycleStatus.SHADOW)
        predictions = self.models.predictions_for_model(
            model.model_id,
            reconciled_only=True,
        )
        labels: list[int] = []
        probabilities: list[float] = []
        for prediction in predictions:
            actual = (prediction.actual_outcomes or {}).get("clean_10")
            probability = prediction.probabilities.get("clean_10")
            if actual is None or probability is None:
                continue
            labels.append(1 if bool(actual) else 0)
            probabilities.append(float(probability))

        if len(labels) < self.policy.min_shadow_samples:
            self._record(
                model,
                action="shadow_wait",
                reason=(
                    f"need {self.policy.min_shadow_samples} reconciled clean_10 shadow "
                    f"examples; have {len(labels)}"
                ),
                metrics={"samples": len(labels)},
            )
            return model

        metrics = binary_metrics(labels, probabilities)
        self.models.update_shadow_metrics(model.model_id, {"clean_10": metrics})
        precision = metrics.get("precision")
        fpr = metrics.get("false_positive_rate")
        if precision is None or precision < self.policy.min_shadow_clean_precision:
            return self._reject(
                model,
                f"shadow clean-win precision {precision} below policy",
                metrics=metrics,
            )
        if fpr is None or fpr > self.policy.max_shadow_false_positive_rate:
            return self._reject(
                model,
                f"shadow false-positive rate {fpr} above policy",
                metrics=metrics,
            )

        champion = self.models.champion(model.symbol)
        if champion is not None:
            champion_predictions = self.models.predictions_for_model(
                champion.model_id,
                reconciled_only=True,
            )
            champion_labels: list[int] = []
            champion_probabilities: list[float] = []
            for prediction in champion_predictions:
                actual = (prediction.actual_outcomes or {}).get("clean_10")
                probability = prediction.probabilities.get("clean_10")
                if actual is None or probability is None:
                    continue
                champion_labels.append(1 if bool(actual) else 0)
                champion_probabilities.append(float(probability))
            if champion_labels:
                champion_metrics = binary_metrics(
                    champion_labels,
                    champion_probabilities,
                )
                candidate_precision = float(precision)
                champion_precision = champion_metrics.get("precision")
                candidate_brier = metrics.get("brier")
                champion_brier = champion_metrics.get("brier")
                if (
                    champion_precision is not None
                    and candidate_precision
                    < champion_precision
                    + self.policy.min_precision_improvement_for_promotion
                ):
                    self._record(
                        model,
                        action="shadow_hold",
                        reason="shadow precision has not cleared Champion promotion margin",
                        metrics={
                            "challenger": metrics,
                            "champion": champion_metrics,
                        },
                    )
                    return model
                if (
                    candidate_brier is not None
                    and champion_brier is not None
                    and candidate_brier
                    > champion_brier
                    + self.policy.max_brier_degradation_for_promotion
                ):
                    self._record(
                        model,
                        action="shadow_hold",
                        reason="shadow Brier is worse than Champion",
                        metrics={
                            "challenger": metrics,
                            "champion": champion_metrics,
                        },
                    )
                    return model

        reason = (
            "shadow clean_10 evidence passed policy and Champion comparison"
            if champion is not None
            else "bootstrap Champion: shadow clean_10 evidence passed policy"
        )
        promoted = self.models.promote_champion(
            model.model_id,
            at=to_utc(self._now()),
            reason=reason,
        )
        self._record(
            promoted,
            action="promoted_champion",
            reason=reason,
            metrics=metrics,
            champion_model_id=champion.model_id if champion is not None else None,
        )
        return promoted

    def degradation_signal(self, symbol: str) -> dict[str, Any]:
        champion = self.models.champion(symbol)
        if champion is None:
            return {"degraded": True, "reason": "no Champion"}
        predictions = self.models.predictions_for_model(
            champion.model_id,
            reconciled_only=True,
            limit=200,
        )
        labels: list[int] = []
        probabilities: list[float] = []
        for prediction in predictions:
            actual = (prediction.actual_outcomes or {}).get("clean_10")
            probability = prediction.probabilities.get("clean_10")
            if actual is None or probability is None:
                continue
            labels.append(1 if bool(actual) else 0)
            probabilities.append(float(probability))
        if len(labels) < self.policy.min_shadow_samples:
            return {
                "degraded": False,
                "reason": "insufficient recent reconciled Champion evidence",
                "samples": len(labels),
            }
        metrics = binary_metrics(labels, probabilities)
        degraded = bool(
            metrics.get("precision") is not None
            and float(metrics["precision"]) < self.policy.min_clean_precision
        )
        return {
            "degraded": degraded,
            "reason": "clean-win precision below policy" if degraded else "within policy",
            "metrics": metrics,
        }

    def _validation_failures(self, metrics: Any) -> list[str]:
        failures: list[str] = []
        if metrics.samples < self.policy.min_validation_samples:
            failures.append(
                f"validation samples {metrics.samples} < {self.policy.min_validation_samples}"
            )
        if metrics.precision is None or metrics.precision < self.policy.min_clean_precision:
            failures.append(
                f"clean precision {metrics.precision} < {self.policy.min_clean_precision}"
            )
        if (
            metrics.false_positive_rate is None
            or metrics.false_positive_rate > self.policy.max_clean_false_positive_rate
        ):
            failures.append(
                "clean false-positive rate "
                f"{metrics.false_positive_rate} > {self.policy.max_clean_false_positive_rate}"
            )
        return failures

    def _reject(
        self,
        model: ModelRegistryEntry,
        reason: str,
        *,
        metrics: dict[str, Any] | None = None,
    ) -> ModelRegistryEntry:
        rejected = self.models.set_status(
            model.model_id,
            ModelLifecycleStatus.REJECTED,
        )
        self._record(
            rejected,
            action="rejected",
            reason=reason,
            metrics=metrics or {},
        )
        return rejected

    def _require(
        self,
        model_id: str,
        status: ModelLifecycleStatus,
    ) -> ModelRegistryEntry:
        model = self.models.get_model(model_id)
        if model is None:
            raise LookupError(f"no model {model_id}")
        if model.status != status.value:
            raise ValueError(
                f"model {model_id} is {model.status}; expected {status.value}"
            )
        return model

    def _record(
        self,
        model: ModelRegistryEntry,
        *,
        action: str,
        reason: str,
        metrics: dict[str, Any],
        champion_model_id: str | None = None,
    ) -> EvolutionDecision:
        created = to_utc(self._now())
        current_champion = self.models.champion(model.symbol)
        champion_id = (
            champion_model_id
            if champion_model_id is not None
            else current_champion.model_id if current_champion is not None else None
        )
        digest = hashlib.sha256(
            f"{model.model_id}|{action}|{created.isoformat()}".encode("utf-8")
        ).hexdigest()[:24]
        decision = EvolutionDecision(
            decision_id=f"evolution_{digest}",
            symbol=model.symbol,
            model_id=model.model_id,
            champion_model_id=champion_id,
            action=action,
            reason=reason,
            metrics=metrics,
            created_at=created,
        )
        self.models.write_evolution_decision(decision)
        return decision

    @staticmethod
    def _clean_metrics(metrics: Any) -> dict[str, Any]:
        return metrics.model_dump(mode="json") if hasattr(metrics, "model_dump") else dict(metrics)
