"""V3 feature/prediction parity over the real live-vs-replay detection paths."""
from __future__ import annotations

from aureon.ml.v1_features import V1FeatureEncoder
from aureon.models.ml import ModelRegistryEntry, TargetMetrics
from aureon.services.v3_ema_learning import freeze_anchor_features
from aureon.services.v3_ema_model import V3_BINARY_TARGETS, raw_v3_features
from aureon.services.v3_ema_ops import assert_live_replay_parity
from tests.replay.test_parity import run_live, run_replay


def _constant_model(features, *, session: str, direction: str) -> ModelRegistryEntry:
    encoder = V1FeatureEncoder.fit([raw_v3_features(features)])
    target_metrics = {
        target: TargetMetrics(
            samples=50,
            positives=25,
            negatives=25,
            accuracy=0.60,
            precision=0.60,
            recall=0.60,
            brier=0.20,
            log_loss=0.60,
            false_positive_rate=0.30,
        )
        for target in V3_BINARY_TARGETS
    }
    targets = {
        "clean_10": {"kind": "constant", "probability": 0.60},
        "reach_3": {"kind": "constant", "probability": 0.90},
        "reach_5": {"kind": "constant", "probability": 0.82},
        "reach_10": {"kind": "constant", "probability": 0.70},
        "reach_20": {"kind": "constant", "probability": 0.50},
        "reach_30": {"kind": "constant", "probability": 0.35},
        "reach_40": {"kind": "constant", "probability": 0.20},
    }
    return ModelRegistryEntry(
        model_id="v3-parity",
        symbol="XAUUSD",
        algorithm="constant-test",
        status="champion",
        feature_schema_version="AUREON_EMA_FEATURES_V3",
        label_schema_version="AUREON_EMA_MOVEMENT_V3",
        model_schema_version="AUREON_EMA_MODEL_V3",
        trained_from="2026-01-01",
        trained_through="2026-09-30",
        training_samples=100,
        target_metrics=target_metrics,
        validation_metrics={
            "base_rate_gate": {
                "clean_10": {
                    "beats_base_rate": True,
                    "cells": {
                        f"{session}:{direction}": {
                            "samples": 50,
                            "base_rate": 0.50,
                        }
                    },
                }
            }
        },
        artifact={
            "encoder": encoder.to_dict(),
            "targets": targets,
            "mfe_model": {"weights": [0.0] * len(encoder.feature_names), "bias": 12.0},
            "mae_model": {"weights": [0.0] * len(encoder.feature_names), "bias": 3.0},
        },
        created_at="2026-10-01T00:00:00Z",
    )


def test_v3_features_and_predictions_match_live_and_replay(
    historical,
    candles,
) -> None:
    replayed = run_replay(historical)
    live = run_live(candles)

    assert replayed and live
    live_by_id = {detection.detection_id: detection for detection in live}
    detection = next(
        replay
        for replay in replayed
        if replay.detection_id in live_by_id
    )
    live_detection = live_by_id[detection.detection_id]

    replay_features = freeze_anchor_features(
        detection,
        same_candle=[detection],
    )
    live_features = freeze_anchor_features(
        live_detection,
        same_candle=[live_detection],
    )

    direction = detection.direction.value
    model = _constant_model(
        replay_features,
        session=replay_features.session,
        direction=direction,
    )
    hashes = assert_live_replay_parity(
        model,
        live_features,
        replay_features,
        direction=direction,
        min_samples=30,
        min_cell_samples=20,
    )

    assert hashes["feature_hash"]
    assert hashes["prediction_hash"]
