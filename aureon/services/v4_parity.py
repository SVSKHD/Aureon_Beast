"""Aureon V4 live/replay parity contract (TODO 098)."""
from __future__ import annotations
from typing import Any
from aureon.models.ema_journey_v4 import V4MovementFeatures
from aureon.services.v3_reproducibility import stable_payload_hash
from aureon.services.v4_ema_model import V4RemainingMoveBundle, predict_remaining_move

def assert_v4_live_replay_parity(
    model: V4RemainingMoveBundle,
    live_features: V4MovementFeatures,
    replay_features: V4MovementFeatures,
    *,
    live_context: dict[str, Any] | None = None,
    replay_context: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Require identical frozen V4 inputs and model outputs for live vs replay.

    Optional context pins non-model journey facts such as phase, pullback state,
    session membership and HTF snapshots. Context must be prepared upstream from
    the same closed-candle inputs; this function only hashes and compares it.
    """
    live_feature_hash=stable_payload_hash(live_features)
    replay_feature_hash=stable_payload_hash(replay_features)
    if live_feature_hash != replay_feature_hash:
        raise AssertionError("V4 feature parity failed: live and replay snapshots differ")

    live_context_hash=stable_payload_hash(live_context or {})
    replay_context_hash=stable_payload_hash(replay_context or {})
    if live_context_hash != replay_context_hash:
        raise AssertionError("V4 context parity failed: live and replay journey context differs")

    live_prediction=predict_remaining_move(model,live_features)
    replay_prediction=predict_remaining_move(model,replay_features)
    live_prediction_hash=stable_payload_hash(live_prediction)
    replay_prediction_hash=stable_payload_hash(replay_prediction)
    if live_prediction_hash != replay_prediction_hash:
        raise AssertionError("V4 prediction parity failed: live and replay outputs differ")
    return {
        "feature_hash": live_feature_hash,
        "context_hash": live_context_hash,
        "prediction_hash": live_prediction_hash,
    }
