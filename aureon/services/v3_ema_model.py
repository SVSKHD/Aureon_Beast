"""Multi-output training and inference for Aureon V3 EMA movement examples.

The model predicts movement probabilities plus expected MFE/MAE. Agent agreement
remains a separate deterministic input and is never relabelled as model confidence.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from aureon.ml.logistic import LogisticModel, binary_metrics, fit_logistic
from aureon.ml.v1_features import V1FeatureEncoder
from aureon.models.ema_journey_v3 import (
    EMA_FEATURE_SCHEMA_V3,
    EMA_LABEL_SCHEMA_V3,
    EMA_MODEL_SCHEMA_V3,
    EMAAnchorFeaturesV3,
)
from aureon.models.ml import ModelRegistryEntry, TargetMetrics
from aureon.models.base import to_utc, utc_now
from aureon.services.v3_ema_learning import (
    CanonicalEMAExampleV3,
    EMAModelConfidenceV3,
    calibration_buckets,
)

V3_BINARY_TARGETS = (
    "clean_10",
    "reach_3",
    "reach_5",
    "reach_10",
    "reach_20",
    "reach_30",
    "reach_40",
)


def raw_v3_features(features: EMAAnchorFeaturesV3) -> tuple[dict[str, float], dict[str, str]]:
    numeric = {
        "ema20": float(features.ema20 or 0.0),
        "ema50": float(features.ema50 or 0.0),
        "ema200": float(features.ema200 or 0.0),
        "ema_gap": float(features.ema_gap or 0.0),
        "ema_gap_change": float(features.ema_gap_change or 0.0),
        "ema_slope": float(features.ema_slope or 0.0),
        "rsi": float(features.rsi if features.rsi is not None else 50.0),
        "rsi_change": float(features.rsi_change or 0.0),
        "atr": float(features.atr or 0.0),
        "tick_volume": float(features.tick_volume or 0.0),
        "volume_ratio_to_median": float(features.volume_ratio_to_median or 0.0),
        "volume_percentile": float(features.volume_percentile or 0.0),
        "volume_3bar_mean": float(features.pre_cross_volume_3bar_mean or 0.0),
        "volume_5bar_mean": float(features.pre_cross_volume_5bar_mean or 0.0),
        "spread_points": float(features.spread_points or 0.0),
        "minutes_to_news": float(features.minutes_to_news or 9999.0),
        "agent_supportive": float(features.agent_confidence.supportive),
        "agent_neutral": float(features.agent_confidence.neutral),
        "agent_conflicting": float(features.agent_confidence.conflicting),
        "agent_coverage": float(features.agent_confidence.coverage),
    }
    categorical = {
        "trend": features.trend_direction,
        "pattern": features.pattern,
        "cross_quality": features.cross_quality,
        "price_vs_ema200": features.price_vs_ema200,
        "ema20_50_relation": features.ema20_50_relation,
        "volatility_regime": features.volatility_regime,
        "volume_state": features.volume_state,
        "volume_price_alignment": features.volume_price_alignment,
        "news_event": features.news_event or "NONE",
        "high_impact_news": "yes" if features.high_impact_news else "no",
        "session": features.session,
        "session_phase": features.session_phase,
        "market_structure": features.market_structure,
        "htf_alignment": features.htf_alignment,
        "agent_confidence_label": features.agent_confidence.label,
    }
    for name, state in sorted(features.internal_agents.items()):
        categorical[f"agent:{name}"] = state
    return numeric, categorical


def _target(example: CanonicalEMAExampleV3, name: str) -> bool:
    outcome = example.outcome
    return {
        "clean_10": outcome.clean_10,
        "reach_3": outcome.reached_3,
        "reach_5": outcome.reached_5,
        "reach_10": outcome.reached_10,
        "reach_20": outcome.reached_20,
        "reach_30": outcome.reached_30,
        "reach_40": outcome.reached_40,
    }[name]


def _constant(labels: list[int]) -> dict[str, Any]:
    return {
        "kind": "constant",
        "probability": (sum(labels) + 1.0) / (len(labels) + 2.0),
    }


def _fit_binary(
    vectors: list[list[float]],
    labels: list[int],
    sample_weights: list[float] | None = None,
) -> dict[str, Any]:
    if not labels:
        raise ValueError("cannot fit empty target")
    if all(label == labels[0] for label in labels):
        return _constant(labels)
    return {
        "kind": "logistic",
        "model": fit_logistic(
            vectors,
            labels,
            sample_weights=sample_weights,
        ).to_dict(),
    }


def _predict_binary(payload: dict[str, Any], vector: list[float]) -> float:
    if payload["kind"] == "constant":
        return float(payload["probability"])
    return LogisticModel.from_dict(payload["model"]).probability(vector)


def _fit_linear(vectors: list[list[float]], values: list[float]) -> dict[str, Any]:
    """Small deterministic ridge-like linear regressor using gradient descent."""
    if not vectors or len(vectors) != len(values):
        raise ValueError("linear target requires aligned rows")
    width = len(vectors[0])
    weights = [0.0] * width
    bias = sum(values) / len(values)
    lr = 0.01
    l2 = 0.001
    for _ in range(300):
        grad_w = [0.0] * width
        grad_b = 0.0
        for vector, value in zip(vectors, values, strict=True):
            pred = bias + sum(w * x for w, x in zip(weights, vector, strict=True))
            error = pred - value
            grad_b += error
            for i, x in enumerate(vector):
                grad_w[i] += error * x
        n = float(len(values))
        bias -= lr * grad_b / n
        for i in range(width):
            weights[i] -= lr * (grad_w[i] / n + l2 * weights[i])
    return {"weights": weights, "bias": bias}


def _predict_linear(payload: dict[str, Any], vector: list[float]) -> float:
    return max(
        0.0,
        float(payload["bias"])
        + sum(float(w) * x for w, x in zip(payload["weights"], vector, strict=True)),
    )


def _ood(
    encoder: V1FeatureEncoder,
    numeric: dict[str, float],
    categorical: dict[str, str],
) -> tuple[bool, str]:
    unseen = [
        f"{key}={value}"
        for key, value in categorical.items()
        if key in encoder.categorical_values
        and value not in encoder.categorical_values[key]
    ]
    extreme = []
    for key in encoder.numeric_names:
        scale = max(encoder.scales.get(key, 1.0), 1e-9)
        z = abs((numeric.get(key, 0.0) - encoder.means.get(key, 0.0)) / scale)
        if z > 5.0:
            extreme.append(f"{key}:z={z:.1f}")
    if unseen or extreme:
        return True, "unseen context " + ", ".join((unseen + extreme)[:6])
    return False, ""


@dataclass(frozen=True)
class V3Bundle:
    encoder: V1FeatureEncoder
    targets: dict[str, dict[str, Any]]
    mfe_model: dict[str, Any]
    mae_model: dict[str, Any]
    metrics: dict[str, TargetMetrics]
    validation: dict[str, Any]

    def artifact(self) -> dict[str, Any]:
        return {
            "model_schema_version": EMA_MODEL_SCHEMA_V3,
            "feature_schema_version": EMA_FEATURE_SCHEMA_V3,
            "label_schema_version": EMA_LABEL_SCHEMA_V3,
            "encoder": self.encoder.to_dict(),
            "targets": self.targets,
            "mfe_model": self.mfe_model,
            "mae_model": self.mae_model,
        }


def fit_v3_bundle(
    examples: list[CanonicalEMAExampleV3],
    *,
    min_samples: int = 30,
    train_fraction: float = 0.8,
) -> V3Bundle:
    ordered = sorted(examples, key=lambda e: (e.generated_at, e.detection_id))
    if len(ordered) < min_samples:
        raise ValueError(f"need at least {min_samples} V3 EMA examples; have {len(ordered)}")
    split = max(20, min(len(ordered) - 5, int(len(ordered) * train_fraction)))
    validation = ordered[split:]
    validation_start = validation[0].generated_at
    train = [e for e in ordered[:split] if e.outcome.resolved_at <= validation_start]
    if len(train) < min(20, min_samples):
        raise ValueError("not enough leak-free V3 examples before validation")

    raw_train = [raw_v3_features(e.features) for e in train]
    encoder = V1FeatureEncoder.fit(raw_train)
    train_vectors = [encoder.transform(*row) for row in raw_train]
    train_weights = [float(e.independence_weight) for e in train]
    validation_vectors = [
        encoder.transform(*raw_v3_features(e.features))
        for e in validation
    ]

    targets: dict[str, dict[str, Any]] = {}
    metrics: dict[str, TargetMetrics] = {}
    calibration: dict[str, Any] = {}
    for target in V3_BINARY_TARGETS:
        train_labels = [1 if _target(e, target) else 0 for e in train]
        fitted = _fit_binary(
            train_vectors,
            train_labels,
            sample_weights=train_weights,
        )
        targets[target] = fitted
        labels = [1 if _target(e, target) else 0 for e in validation]
        probs = [_predict_binary(fitted, vector) for vector in validation_vectors]
        metric = binary_metrics(labels, probs)
        positives = [e for e, y in zip(validation, labels, strict=True) if y]
        metric["average_mae"] = (
            sum(e.outcome.mae for e in positives) / len(positives)
            if positives else None
        )
        metric["average_mfe"] = (
            sum(e.outcome.mfe for e in positives) / len(positives)
            if positives else None
        )
        metrics[target] = TargetMetrics.model_validate(metric)
        calibration[target] = calibration_buckets(probs, [bool(y) for y in labels])

    mfe_model = _fit_linear(train_vectors, [e.outcome.mfe for e in train])
    mae_model = _fit_linear(train_vectors, [e.outcome.mae for e in train])

    validation_payload = {
        "chronological": {
            "train_samples": len(train),
            "validation_samples": len(validation),
            "train_from": train[0].market_date,
            "train_through": train[-1].market_date,
            "validation_from": validation[0].market_date,
            "validation_through": validation[-1].market_date,
        },
        "calibration": calibration,
        "walk_forward_ready": True,
    }
    return V3Bundle(
        encoder=encoder,
        targets=targets,
        mfe_model=mfe_model,
        mae_model=mae_model,
        metrics=metrics,
        validation=validation_payload,
    )




def _score_artifact(
    artifact: dict[str, Any],
    examples: list[CanonicalEMAExampleV3],
) -> dict[str, Any]:
    encoder = V1FeatureEncoder.from_dict(artifact["encoder"])
    metrics: dict[str, Any] = {}
    calibration: dict[str, Any] = {}
    for target in V3_BINARY_TARGETS:
        labels = [1 if _target(example, target) else 0 for example in examples]
        probs = []
        for example in examples:
            vector = encoder.transform(*raw_v3_features(example.features))
            probs.append(_predict_binary(artifact["targets"][target], vector))
        metrics[target] = binary_metrics(labels, probs)
        calibration[target] = calibration_buckets(probs, [bool(v) for v in labels])
    return {"metrics": metrics, "calibration": calibration, "samples": len(examples)}


def walk_forward_v3(
    examples: list[CanonicalEMAExampleV3],
    *,
    min_train_samples: int = 30,
    test_days: int = 1,
) -> dict[str, Any]:
    """Expanding-window chronological validation by market day."""
    ordered = sorted(examples, key=lambda e: (e.market_date, e.generated_at, e.detection_id))
    dates = sorted({e.market_date for e in ordered})
    folds: list[dict[str, Any]] = []
    aggregate_labels: dict[str, list[int]] = {t: [] for t in V3_BINARY_TARGETS}
    aggregate_probs: dict[str, list[float]] = {t: [] for t in V3_BINARY_TARGETS}

    for index in range(1, len(dates), test_days):
        test_dates = dates[index:index + test_days]
        if not test_dates:
            continue
        train_dates = set(dates[:index])
        train = [e for e in ordered if e.market_date in train_dates]
        test = [e for e in ordered if e.market_date in set(test_dates)]
        if len(train) < min_train_samples or not test:
            continue
        first_test_time = min(e.generated_at for e in test)
        train = [e for e in train if e.outcome.resolved_at <= first_test_time]
        if len(train) < min_train_samples:
            continue

        bundle = fit_v3_bundle(
            train,
            min_samples=min_train_samples,
            train_fraction=0.8,
        )
        artifact = bundle.artifact()
        score = _score_artifact(artifact, test)
        folds.append(
            {
                "train_from": min(e.market_date for e in train),
                "train_through": max(e.market_date for e in train),
                "test_from": min(test_dates),
                "test_through": max(test_dates),
                "train_samples": len(train),
                "test_samples": len(test),
                "metrics": score["metrics"],
            }
        )
        encoder = V1FeatureEncoder.from_dict(artifact["encoder"])
        for example in test:
            vector = encoder.transform(*raw_v3_features(example.features))
            for target in V3_BINARY_TARGETS:
                aggregate_labels[target].append(1 if _target(example, target) else 0)
                aggregate_probs[target].append(
                    _predict_binary(artifact["targets"][target], vector)
                )

    aggregate = {
        target: binary_metrics(aggregate_labels[target], aggregate_probs[target])
        for target in V3_BINARY_TARGETS
        if aggregate_labels[target]
    }
    return {
        "complete": bool(folds),
        "folds": folds,
        "aggregate_metrics": aggregate,
        "out_of_sample_predictions": sum(len(v) for v in aggregate_labels.values())
        // max(1, len(V3_BINARY_TARGETS)),
    }


class V3EMAModelTrainer:
    """Train a V3 Candidate. Existing EvolutionAgent owns later lifecycle stages."""

    def __init__(self, examples: Any, models: Any, *, now: Any = utc_now) -> None:
        self.examples = examples
        self.models = models
        self._now = now

    def train_candidate(
        self,
        symbol: str,
        *,
        start_market_date: str = "0001-01-01",
        end_market_date: str = "9999-12-31",
        min_samples: int = 30,
    ) -> ModelRegistryEntry:
        rows = self.examples.examples_between(
            symbol.upper(), start_market_date, end_market_date
        )
        rows = [
            row for row in rows
            if not self.examples.is_held_out(row.symbol, row.market_date)
        ]
        false_positive_ids = self.examples.false_positive_detection_ids(symbol.upper())
        weighted_rows = list(rows) + [
            row for row in rows if row.detection_id in false_positive_ids
        ]
        bundle = fit_v3_bundle(weighted_rows, min_samples=min_samples)
        walk_forward = walk_forward_v3(
            rows,
            min_train_samples=min_samples,
            test_days=1,
        )
        created = to_utc(self._now())
        artifact = bundle.artifact()
        digest = hashlib.sha256(
            json.dumps(
                {
                    "symbol": symbol.upper(),
                    "schema": EMA_MODEL_SCHEMA_V3,
                    "examples": [row.example_id for row in rows],
                    "artifact": artifact,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:20]
        model_id = f"{symbol.lower()}_ema_v3_{digest}"
        existing = self.models.get_model(model_id)
        if existing is not None:
            return existing

        entry = ModelRegistryEntry(
            model_id=model_id,
            symbol=symbol.upper(),
            algorithm="logistic_linear_v3",
            status="candidate",
            feature_schema_version=EMA_FEATURE_SCHEMA_V3,
            label_schema_version=EMA_LABEL_SCHEMA_V3,
            model_schema_version=EMA_MODEL_SCHEMA_V3,
            trained_from=min(row.market_date for row in rows),
            trained_through=max(row.market_date for row in rows),
            training_samples=len(rows),
            target_metrics=bundle.metrics,
            validation_metrics={
                **bundle.validation,
                "walk_forward": walk_forward,
                "false_positive_replay_weighted": len(weighted_rows) - len(rows),
            },
            artifact=artifact,
            created_at=created,
        )
        self.models.write_model(entry)

        # Freeze the next unseen market date immediately after training. The model id
        # is fixed before any event on that date is scored.
        from aureon.services.v3_ema_learning import EMAHoldoutDayV3
        from datetime import UTC, datetime, timedelta
        next_date = (
            datetime.fromisoformat(entry.trained_through).replace(tzinfo=UTC)
            + timedelta(days=1)
        ).date().isoformat()
        holdout_id = hashlib.sha256(
            f"{symbol.upper()}|{next_date}|{entry.model_id}|V3_EMA_HOLDOUT".encode()
        ).hexdigest()
        self.examples.write_holdout(
            EMAHoldoutDayV3(
                holdout_id=holdout_id,
                symbol=symbol.upper(),
                market_date=next_date,
                frozen_model_id=entry.model_id,
                status="open",
                created_at=created,
            )
        )
        return entry


def predict_v3(
    model: ModelRegistryEntry,
    features: EMAAnchorFeaturesV3,
    *,
    min_samples: int = 30,
) -> EMAModelConfidenceV3:
    if (
        model.feature_schema_version != EMA_FEATURE_SCHEMA_V3
        or model.label_schema_version != EMA_LABEL_SCHEMA_V3
        or model.model_schema_version != EMA_MODEL_SCHEMA_V3
    ):
        raise ValueError("V3 EMA model contract mismatch")

    sample_count = int(model.training_samples)
    if sample_count < min_samples:
        return EMAModelConfidenceV3(
            model_id=model.model_id,
            sample_count=sample_count,
            sufficient_data=False,
            reason=f"INSUFFICIENT TRAINING DATA: {sample_count}/{min_samples}",
        )

    encoder = V1FeatureEncoder.from_dict(model.artifact["encoder"])
    raw = raw_v3_features(features)
    is_ood, ood_reason = _ood(encoder, *raw)
    if is_ood:
        return EMAModelConfidenceV3(
            model_id=model.model_id,
            sample_count=sample_count,
            sufficient_data=False,
            out_of_distribution=True,
            reason=f"OUT_OF_DISTRIBUTION: {ood_reason}",
        )

    vector = encoder.transform(*raw)
    probs = {
        name: _predict_binary(payload, vector)
        for name, payload in model.artifact["targets"].items()
    }
    previous = 1.0
    for name in ("reach_3", "reach_5", "reach_10", "reach_20", "reach_30", "reach_40"):
        probs[name] = min(previous, probs[name])
        previous = probs[name]

    return EMAModelConfidenceV3(
        model_id=model.model_id,
        sample_count=sample_count,
        sufficient_data=True,
        probability_reach_3=probs.get("reach_3"),
        probability_reach_5=probs.get("reach_5"),
        probability_reach_10=probs.get("reach_10"),
        probability_reach_20=probs.get("reach_20"),
        probability_reach_30=probs.get("reach_30"),
        probability_reach_40=probs.get("reach_40"),
        probability_clean_10=probs.get("clean_10"),
        expected_mfe=_predict_linear(model.artifact["mfe_model"], vector),
        expected_mae=_predict_linear(model.artifact["mae_model"], vector),
        reason="validated historical estimate; not a guarantee",
    )
