"""Frozen V1 registry scoring and probability-ordering tests."""

from datetime import UTC, datetime, timedelta

from aureon.ml.v1_features import V1FeatureEncoder, raw_v1_features
from aureon.models.enums import Direction, Timeframe
from aureon.models.learning_v1 import (
    CanonicalTrainingExample,
    CleanMoveOutcomeV1,
    FEATURE_SCHEMA_V1,
    LABEL_SCHEMA_V1,
    MODEL_SCHEMA_V1,
    FeatureSnapshotV1,
)
from aureon.models.ml import ModelRegistryEntry
from aureon.services.v1_model_training import predict_v1_artifact, score_v1_model


def _feature(index: int) -> FeatureSnapshotV1:
    at = datetime(2026, 3, 1, tzinfo=UTC) + timedelta(minutes=5 * index)
    return FeatureSnapshotV1(
        setup_id=f"s-{index}",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        timestamp=at,
        frozen_at=at,
        direction=Direction.BUY,
        reference_price=4500.0,
        rsi=55.0,
        atr=8.0,
    )


def _model(seed: FeatureSnapshotV1) -> ModelRegistryEntry:
    encoder = V1FeatureEncoder.fit([raw_v1_features(seed)])
    return ModelRegistryEntry(
        model_id="registry-v1",
        symbol="XAUUSD",
        algorithm="logistic_regression_v1",
        status="champion",
        feature_schema_version=FEATURE_SCHEMA_V1,
        label_schema_version=LABEL_SCHEMA_V1,
        model_schema_version=MODEL_SCHEMA_V1,
        trained_from="2026-01-01",
        trained_through="2026-02-28",
        training_samples=50,
        artifact={
            "encoder": encoder.to_dict(),
            "targets": {
                "clean_10": {"kind": "constant", "probability": 0.70},
                "reach_5": {"kind": "constant", "probability": 0.75},
                "reach_10": {"kind": "constant", "probability": 0.90},
                "reach_20": {"kind": "constant", "probability": 0.80},
                "reach_30": {"kind": "constant", "probability": 0.65},
                "reach_40": {"kind": "constant", "probability": 0.72},
            },
        },
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )


def test_reach_probabilities_are_monotonic_at_prediction_boundary() -> None:
    feature = _feature(0)
    probabilities = predict_v1_artifact(_model(feature), feature)
    ladder = [
        probabilities["reach_5"],
        probabilities["reach_10"],
        probabilities["reach_20"],
        probabilities["reach_30"],
        probabilities["reach_40"],
    ]
    assert ladder == sorted(ladder, reverse=True)


def test_registry_scoring_is_frozen_and_does_not_refit() -> None:
    feature = _feature(0)
    model = _model(feature)
    examples = []
    for index, clean in enumerate((True, False, True, False)):
        features = _feature(index)
        examples.append(
            CanonicalTrainingExample(
                example_id=f"e-{index}",
                market_date=features.timestamp.date().isoformat(),
                setup_id=features.setup_id,
                symbol="XAUUSD",
                timeframe=Timeframe.M5,
                features=features,
                outcome=CleanMoveOutcomeV1(
                    resolved_at=features.timestamp + timedelta(hours=1),
                    horizon_bars=12,
                    clean_10=clean,
                    reached_5=True,
                    reached_10=clean,
                    reached_20=False,
                    reached_30=False,
                    reached_40=False,
                    bars_to_5=2,
                    bars_to_10=6 if clean else None,
                    max_favourable_move=12.0 if clean else 7.0,
                    max_adverse_move=3.0 if clean else 9.0,
                    mae_before_10=3.0 if clean else None,
                ),
                generated_at=features.timestamp + timedelta(hours=1),
            )
        )

    before = model.model_dump(mode="json")
    result = score_v1_model(model, examples, decision_threshold=0.55)
    after = model.model_dump(mode="json")

    assert result["status"] == "tested_registry_model"
    assert result["samples"] == 4
    assert result["selected_clean_setups"] == 4
    assert result["selected_clean_wins"] == 2
    assert before == after
