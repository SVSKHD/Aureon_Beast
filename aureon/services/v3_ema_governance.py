"""Contract-isolated governance for Aureon V3 EMA movement models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aureon.ml.logistic import binary_metrics
from aureon.models.base import to_utc, utc_now
from aureon.models.ema_journey_v3 import (
    EMA_FEATURE_SCHEMA_V3,
    EMA_LABEL_SCHEMA_V3,
    EMA_MODEL_SCHEMA_V3,
)


@dataclass(frozen=True)
class V3EMAGovernancePolicy:
    min_validation_samples: int = 20
    min_clean_precision: float = 0.55
    max_clean_false_positive_rate: float = 0.45
    min_shadow_samples: int = 20
    min_shadow_clean_precision: float = 0.55
    max_shadow_false_positive_rate: float = 0.40


class V3EMAGovernance:
    """Candidate -> Challenger -> Shadow -> Champion for the V3 EMA family only."""

    def __init__(
        self,
        models: Any,
        learning: Any,
        *,
        policy: V3EMAGovernancePolicy | None = None,
        now: Any = utc_now,
    ) -> None:
        self.models = models
        self.learning = learning
        self.policy = policy or V3EMAGovernancePolicy()
        self._now = now

    def qualify_candidate(self, model_id: str) -> Any:
        model = self._require_contract(model_id, "candidate")
        clean = model.target_metrics.get("clean_10")
        if clean is None:
            return self.models.set_status(
                model_id,
                "rejected",
                promotion_reason="no clean_10 metrics",
            )
        failures = []
        if clean.samples < self.policy.min_validation_samples:
            failures.append(
                f"validation samples {clean.samples} < {self.policy.min_validation_samples}"
            )
        if clean.precision is None or clean.precision < self.policy.min_clean_precision:
            failures.append(f"clean_10 precision {clean.precision} below policy")
        if (
            clean.false_positive_rate is None
            or clean.false_positive_rate > self.policy.max_clean_false_positive_rate
        ):
            failures.append(f"clean_10 FPR {clean.false_positive_rate} above policy")

        walk = (model.validation_metrics or {}).get("walk_forward") or {}
        if not walk.get("complete"):
            failures.append("walk-forward validation incomplete")
        if failures:
            return self.models.set_status(
                model_id,
                "rejected",
                promotion_reason="; ".join(failures),
            )
        return self.models.set_status(
            model_id,
            "challenger",
            promotion_reason="V3 chronological validation gates passed",
        )

    def admit_shadow(self, model_id: str) -> Any:
        model = self._require_contract(model_id, "challenger")
        walk = (model.validation_metrics or {}).get("walk_forward") or {}
        if not walk.get("complete"):
            raise ValueError("V3 challenger has no completed walk-forward evidence")
        return self.models.activate_shadow_for_contract(
            model_id,
            at=to_utc(self._now()),
        )

    def evaluate_shadow(self, model_id: str) -> Any:
        model = self._require_contract(model_id, "shadow")
        rows = self.learning.predictions_for_model(
            model_id,
            reconciled_only=True,
        )
        labels: list[int] = []
        probabilities: list[float] = []
        for row in rows:
            actual = (row.get("actual_outcome") or {}).get("clean_10")
            probability = (row.get("payload") or {}).get("probability_clean_10")
            if actual is None or probability is None:
                continue
            labels.append(1 if bool(actual) else 0)
            probabilities.append(float(probability))

        if len(labels) < self.policy.min_shadow_samples:
            return model

        metrics = binary_metrics(labels, probabilities)
        precision = metrics.get("precision")
        fpr = metrics.get("false_positive_rate")
        if precision is None or precision < self.policy.min_shadow_clean_precision:
            return self.models.set_status(
                model_id,
                "rejected",
                promotion_reason=f"shadow clean_10 precision {precision} below policy",
            )
        if fpr is None or fpr > self.policy.max_shadow_false_positive_rate:
            return self.models.set_status(
                model_id,
                "rejected",
                promotion_reason=f"shadow clean_10 FPR {fpr} above policy",
            )

        return self.models.promote_champion_for_contract(
            model_id,
            at=to_utc(self._now()),
            reason="V3 EMA shadow evidence passed policy",
        )

    def _require_contract(self, model_id: str, status: str) -> Any:
        model = self.models.get_model(model_id)
        if model is None:
            raise LookupError(f"no model {model_id}")
        if (
            model.feature_schema_version != EMA_FEATURE_SCHEMA_V3
            or model.label_schema_version != EMA_LABEL_SCHEMA_V3
            or model.model_schema_version != EMA_MODEL_SCHEMA_V3
        ):
            raise ValueError(f"{model_id} is not a V3 EMA movement model")
        if model.status != status:
            raise ValueError(f"{model_id} is {model.status}; expected {status}")
        return model
