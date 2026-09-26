"""Model-governance Agent for Aureon V1.

EvolutionAgent never predicts the market and never executes trades. It only advances or
rejects model lifecycle states using out-of-sample and persistent shadow evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from aureon.models.base import to_utc, utc_now
from aureon.models.ml import EvolutionDecision, ModelRegistryEntry


@dataclass(frozen=True)
class EvolutionPolicy:
    min_oos_samples: int = 30
    min_clean_precision: float = 0.50
    min_clean_recall: float = 0.20
    max_clean_brier: float = 0.30
    max_false_positive_rate: float = 0.55
    comparison_precision_tolerance: float = 0.02
    comparison_brier_tolerance: float = 0.01
    comparison_fpr_tolerance: float = 0.02
    min_shadow_reconciled: int = 20
    max_shadow_clean_brier: float = 0.30


class EvolutionAgent:
    """Govern Candidate → Challenger → Shadow → Champion transitions."""

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

    def evaluate_candidate(self, model_id: str) -> ModelRegistryEntry:
        candidate = self._required(model_id)
        if candidate.status not in {"candidate", "challenger"}:
            raise RuntimeError(
                f"{model_id} is {candidate.status}; candidate evaluation refused"
            )
        backtest = self.models.latest_backtest_for_model(model_id)
        if backtest is None or backtest.status != "complete":
            return self._reject(
                candidate,
                "candidate has no completed chronological walk-forward validation",
                metrics={},
            )
        clean = backtest.aggregate_metrics.get("clean_10")
        if clean is None:
            return self._reject(candidate, "walk-forward has no clean_10 metric", metrics={})

        metrics = clean.model_dump(mode="json")
        failure = self._historical_failure(metrics)
        champion = self.models.active_champion(candidate.symbol)
        if failure is None and champion is not None:
            failure = self._compare_to_champion(candidate, metrics, champion)

        self.models.update_model_metrics(
            candidate.model_id,
            validation_metrics={
                "clean_10": metrics,
                "breakdowns": backtest.breakdown_metrics,
                "backtest_id": backtest.backtest_id,
            },
        )
        if failure is not None:
            return self._reject(candidate, failure, metrics=metrics)

        challenger = self.models.set_status(candidate.model_id, "challenger")
        self._record(
            challenger,
            action="challenger_qualified",
            reason="chronological walk-forward quality gates passed",
            metrics=metrics,
            champion=champion,
        )
        shadow = self.models.activate_shadow(challenger.model_id, at=to_utc(self._now()))
        self._record(
            shadow,
            action="shadow_started",
            reason="qualified Challenger entered persistent non-executing shadow mode",
            metrics=metrics,
            champion=champion,
        )
        return shadow

    def evaluate_shadow(self, model_id: str) -> ModelRegistryEntry:
        challenger = self._required(model_id)
        if challenger.status != "shadow":
            raise RuntimeError(f"{model_id} is {challenger.status}; shadow evaluation refused")
        summary = self.models.prediction_summary_for_model(model_id)
        metrics = summary.model_dump(mode="json")
        self.models.update_model_metrics(model_id, shadow_metrics=metrics)

        if summary.reconciled < self.policy.min_shadow_reconciled:
            self._record(
                challenger,
                action="shadow_evaluated",
                reason=(
                    f"shadow observation continues: {summary.reconciled}/"
                    f"{self.policy.min_shadow_reconciled} reconciled predictions"
                ),
                metrics=metrics,
                champion=self.models.active_champion(challenger.symbol),
            )
            return challenger
        if (
            summary.clean_10_brier is None
            or summary.clean_10_brier > self.policy.max_shadow_clean_brier
        ):
            return self._reject(
                challenger,
                "shadow clean_10 calibration failed promotion gate",
                metrics=metrics,
            )

        champion = self.models.active_champion(challenger.symbol)
        if champion is not None:
            champion_summary = self.models.prediction_summary_for_model(champion.model_id)
            if (
                champion_summary.reconciled >= self.policy.min_shadow_reconciled
                and champion_summary.clean_10_brier is not None
                and summary.clean_10_brier
                > champion_summary.clean_10_brier + self.policy.comparison_brier_tolerance
            ):
                return self._reject(
                    challenger,
                    "shadow clean_10 Brier score is materially worse than Champion",
                    metrics={
                        **metrics,
                        "champion_clean_10_brier": champion_summary.clean_10_brier,
                    },
                )

        reason = (
            "completed chronological validation and persistent shadow gates; "
            "promotion never used training accuracy"
        )
        promoted = self.models.promote_champion(
            challenger.model_id,
            at=to_utc(self._now()),
            reason=reason,
        )
        if champion is not None and champion.model_id != promoted.model_id:
            self._record(
                champion,
                action="champion_retired",
                reason=f"replaced by {promoted.model_id}",
                metrics={},
                champion=promoted,
            )
        self._record(
            promoted,
            action="promoted",
            reason=reason,
            metrics=metrics,
            champion=champion,
        )
        return promoted

    def bootstrap_champion(self, model_id: str, *, reason: str) -> ModelRegistryEntry:
        """Explicit bootstrap only; useful when V1 has no Champion yet.

        The model must already be in shadow and meet the normal shadow sample/calibration
        gates. This method does not bypass validation.
        """
        model = self._required(model_id)
        if model.status != "shadow":
            raise RuntimeError("bootstrap Champion must first be a qualified shadow model")
        return self.evaluate_shadow(model_id)

    def detect_champion_degradation(self, symbol: str) -> bool:
        champion = self.models.active_champion(symbol.upper())
        if champion is None:
            return False
        summary = self.models.prediction_summary_for_model(champion.model_id)
        degraded = bool(
            summary.reconciled >= self.policy.min_shadow_reconciled
            and summary.clean_10_brier is not None
            and summary.clean_10_brier > self.policy.max_shadow_clean_brier
        )
        if degraded:
            self._record(
                champion,
                action="degradation_detected",
                reason="Champion clean_10 live calibration exceeded governance limit",
                metrics=summary.model_dump(mode="json"),
                champion=champion,
            )
        return degraded

    def _historical_failure(self, metrics: dict[str, Any]) -> str | None:
        if int(metrics.get("samples") or 0) < self.policy.min_oos_samples:
            return "insufficient out-of-sample clean_10 samples"
        precision = metrics.get("precision")
        recall = metrics.get("recall")
        brier = metrics.get("brier")
        fpr = metrics.get("false_positive_rate")
        if precision is None or float(precision) < self.policy.min_clean_precision:
            return "clean-win precision below governance minimum"
        if recall is None or float(recall) < self.policy.min_clean_recall:
            return "clean-win recall below governance minimum"
        if brier is None or float(brier) > self.policy.max_clean_brier:
            return "clean-win Brier score above governance maximum"
        if fpr is not None and float(fpr) > self.policy.max_false_positive_rate:
            return "clean-win false-positive rate above governance maximum"
        return None

    def _compare_to_champion(
        self,
        candidate: ModelRegistryEntry,
        metrics: dict[str, Any],
        champion: ModelRegistryEntry,
    ) -> str | None:
        champion_backtest = self.models.latest_backtest_for_model(champion.model_id)
        if champion_backtest is None or champion_backtest.status != "complete":
            return None
        incumbent = champion_backtest.aggregate_metrics.get("clean_10")
        if incumbent is None:
            return None
        incumbent_metrics = incumbent.model_dump(mode="json")
        precision = metrics.get("precision")
        incumbent_precision = incumbent_metrics.get("precision")
        if (
            precision is not None
            and incumbent_precision is not None
            and float(precision)
            < float(incumbent_precision) - self.policy.comparison_precision_tolerance
        ):
            return "Challenger clean precision is materially worse than Champion"
        brier = metrics.get("brier")
        incumbent_brier = incumbent_metrics.get("brier")
        if (
            brier is not None
            and incumbent_brier is not None
            and float(brier)
            > float(incumbent_brier) + self.policy.comparison_brier_tolerance
        ):
            return "Challenger Brier score is materially worse than Champion"
        fpr = metrics.get("false_positive_rate")
        incumbent_fpr = incumbent_metrics.get("false_positive_rate")
        if (
            fpr is not None
            and incumbent_fpr is not None
            and float(fpr)
            > float(incumbent_fpr) + self.policy.comparison_fpr_tolerance
        ):
            return "Challenger false-positive rate is materially worse than Champion"
        return None

    def _reject(
        self,
        model: ModelRegistryEntry,
        reason: str,
        *,
        metrics: dict[str, Any],
    ) -> ModelRegistryEntry:
        rejected = self.models.set_status(model.model_id, "rejected")
        self._record(
            rejected,
            action="rejected",
            reason=reason,
            metrics=metrics,
            champion=self.models.active_champion(model.symbol),
        )
        return rejected

    def _required(self, model_id: str) -> ModelRegistryEntry:
        model = self.models.get_model(model_id)
        if model is None:
            raise LookupError(f"no model {model_id}")
        return model

    def _record(
        self,
        model: ModelRegistryEntry,
        *,
        action: str,
        reason: str,
        metrics: dict[str, Any],
        champion: ModelRegistryEntry | None,
    ) -> None:
        moment = to_utc(self._now())
        digest = hashlib.sha256(
            f"{model.model_id}|{action}|{moment.isoformat()}|{reason}".encode("utf-8")
        ).hexdigest()[:20]
        self.models.write_evolution_decision(
            EvolutionDecision(
                decision_id=f"evolution_{digest}",
                symbol=model.symbol,
                model_id=model.model_id,
                champion_model_id=champion.model_id if champion is not None else None,
                action=action,
                reason=reason,
                metrics=metrics,
                detail={"model_status": model.status, "algorithm": model.algorithm},
                decided_at=moment,
            )
        )
