"""Operational safeguards for Aureon V3 EMA models.

Covers reproducibility, live drift, live/replay parity, rolling unseen validation,
and deterministic retraining triggers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aureon.ml.logistic import binary_metrics
from aureon.models.base import to_utc, utc_now
from aureon.models.ema_journey_v3 import EMAAnchorFeaturesV3
from aureon.services.v3_ema_learning import calibration_buckets
from aureon.services.v3_ema_model import expected_calibration_error, predict_v3
from aureon.services.v3_reproducibility import stable_payload_hash


def assert_live_replay_parity(
    model: Any,
    live_features: EMAAnchorFeaturesV3,
    replay_features: EMAAnchorFeaturesV3,
    *,
    direction: str,
    min_samples: int = 30,
    min_cell_samples: int = 20,
) -> dict[str, str]:
    live_feature_hash = stable_payload_hash(live_features)
    replay_feature_hash = stable_payload_hash(replay_features)
    if live_feature_hash != replay_feature_hash:
        raise AssertionError(
            "V3 feature parity failed: live and replay snapshots differ"
        )
    live = predict_v3(
        model,
        live_features,
        min_samples=min_samples,
        direction=direction,
        min_cell_samples=min_cell_samples,
    )
    replay = predict_v3(
        model,
        replay_features,
        min_samples=min_samples,
        direction=direction,
        min_cell_samples=min_cell_samples,
    )
    live_prediction_hash = stable_payload_hash(live)
    replay_prediction_hash = stable_payload_hash(replay)
    if live_prediction_hash != replay_prediction_hash:
        raise AssertionError(
            "V3 prediction parity failed: live and replay outputs differ"
        )
    return {
        "feature_hash": live_feature_hash,
        "prediction_hash": live_prediction_hash,
    }


def rolling_unseen_summary(
    holdouts: list[Any],
    *,
    window_days: int = 15,
    min_days: int = 10,
    max_days: int = 20,
) -> dict[str, Any]:
    window_days = max(min_days, min(max_days, int(window_days)))
    scored = [
        row
        for row in holdouts
        if row.status in {"scored", "released"} and row.scored_at is not None
    ]
    scored.sort(key=lambda row: row.market_date)
    selected = scored[-window_days:]

    samples = 0
    false_positive = 0
    false_negative = 0
    briers: list[float] = []
    for row in selected:
        metrics = row.metrics or {}
        samples += int(metrics.get("samples") or 0)
        false_positive += int(metrics.get("false_positives_clean_10") or 0)
        false_negative += int(metrics.get("false_negatives_clean_10") or 0)
        clean = (metrics.get("targets") or {}).get("clean_10") or {}
        brier = clean.get("brier")
        if brier is not None:
            briers.append(float(brier))

    return {
        "window_days": window_days,
        "days_available": len(selected),
        "sufficient_days": len(selected) >= min_days,
        "market_dates": [row.market_date for row in selected],
        "samples": samples,
        "false_positives_clean_10": false_positive,
        "false_negatives_clean_10": false_negative,
        "mean_clean_10_brier": (
            sum(briers) / len(briers) if briers else None
        ),
    }


@dataclass(frozen=True)
class RetrainingPolicy:
    cadence_days: int = 7
    minimum_new_examples: int = 30
    drift_triggered: bool = True


@dataclass(frozen=True)
class RetrainingDecision:
    due: bool
    reasons: tuple[str, ...]


def retraining_due(
    *,
    last_trained_at: datetime | None,
    now: datetime,
    new_examples: int,
    drift_detected: bool,
    policy: RetrainingPolicy | None = None,
) -> RetrainingDecision:
    policy = policy or RetrainingPolicy()
    reasons: list[str] = []
    if last_trained_at is None:
        reasons.append("no_previous_training")
    elif to_utc(now) - to_utc(last_trained_at) >= timedelta(days=policy.cadence_days):
        reasons.append("scheduled_cadence")
    if new_examples >= policy.minimum_new_examples:
        reasons.append("enough_new_examples")
    if policy.drift_triggered and drift_detected:
        reasons.append("champion_drift")
    return RetrainingDecision(bool(reasons), tuple(reasons))


@dataclass(frozen=True)
class DriftPolicy:
    min_samples: int = 30
    max_brier: float = 0.24
    max_calibration_error: float = 0.12
    max_false_positive_rate: float = 0.45


def champion_drift_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    labels: list[int] = []
    probabilities: list[float] = []
    for row in rows:
        actual = (row.get("actual_outcome") or {}).get("clean_10")
        probability = (row.get("payload") or {}).get("probability_clean_10")
        if actual is None or probability is None:
            continue
        labels.append(1 if bool(actual) else 0)
        probabilities.append(float(probability))
    metrics = binary_metrics(labels, probabilities)
    buckets = calibration_buckets(
        probabilities,
        [bool(label) for label in labels],
    )
    return {
        **metrics,
        "calibration_error": expected_calibration_error(buckets),
    }


class V3ChampionDriftMonitor:
    def __init__(
        self,
        *,
        models: Any,
        learning: Any,
        policy: DriftPolicy | None = None,
        now: Any = utc_now,
    ) -> None:
        self.models = models
        self.learning = learning
        self.policy = policy or DriftPolicy()
        self._now = now

    def evaluate(self, model_id: str, *, recent_limit: int = 100) -> dict[str, Any]:
        model = self.models.get_model(model_id)
        if model is None or model.status != "champion":
            raise ValueError("drift monitor requires current Champion")
        rows = self.learning.predictions_for_model(
            model_id,
            reconciled_only=True,
        )[-recent_limit:]
        metrics = champion_drift_metrics(rows)
        samples = int(metrics.get("samples") or 0)
        if samples < self.policy.min_samples:
            return {"drifted": False, "metrics": metrics, "action": "wait"}

        failures: list[str] = []
        if metrics.get("brier") is None or float(metrics["brier"]) > self.policy.max_brier:
            failures.append("brier")
        if (
            metrics.get("calibration_error") is None
            or float(metrics["calibration_error"]) > self.policy.max_calibration_error
        ):
            failures.append("calibration")
        if (
            metrics.get("false_positive_rate") is None
            or float(metrics["false_positive_rate"]) > self.policy.max_false_positive_rate
        ):
            failures.append("false_positive_rate")
        if not failures:
            return {"drifted": False, "metrics": metrics, "action": "keep"}

        rollback = self.models.rollback_champion_for_contract(
            model_id,
            at=to_utc(self._now()),
            reason="V3 live drift: " + ",".join(failures),
        )
        return {
            "drifted": True,
            "metrics": metrics,
            "action": "rollback",
            "rollback_model_id": rollback.model_id if rollback else None,
            "failures": failures,
        }


class V3RetrainingPlanner:
    """Weekly-or-evidence-driven retraining decision for one symbol.

    A run is due when any one of these is true:
    - seven calendar days passed since the latest V3 model;
    - at least 30 newly resolved examples exist after its training cutoff;
    - live Champion drift has been detected.
    The resulting model still enters Candidate/Challenger governance; this never
    promotes a model directly.
    """

    def __init__(
        self,
        *,
        learning: Any,
        models: Any,
        policy: RetrainingPolicy | None = None,
    ) -> None:
        self.learning = learning
        self.models = models
        self.policy = policy or RetrainingPolicy()

    def evaluate(
        self,
        symbol: str,
        *,
        now: datetime,
        drift_detected: bool = False,
    ) -> RetrainingDecision:
        latest = self.models.latest_model(symbol)
        if latest is None:
            return RetrainingDecision(True, ("no_previous_training",))

        rows = self.learning.examples_between(
            symbol.upper(),
            latest.trained_through,
            "9999-12-31",
        )
        new_examples = sum(
            row.market_date > latest.trained_through
            for row in rows
        )
        return retraining_due(
            last_trained_at=latest.created_at,
            now=now,
            new_examples=new_examples,
            drift_detected=drift_detected,
            policy=self.policy,
        )
