"""Prediction boundary tests: Champion/Shadow intelligence never executes."""

from datetime import UTC, datetime
from types import SimpleNamespace

from aureon.ml.v1_features import V1FeatureEncoder, raw_v1_features
from aureon.models.enums import Direction, DirectionContext, Timeframe
from aureon.models.learning_v1 import (
    FEATURE_SCHEMA_V1,
    LABEL_SCHEMA_V1,
    MODEL_SCHEMA_V1,
    FeatureSnapshotV1,
)
from aureon.models.ml import ModelRegistryEntry
from aureon.services.prediction_service import PredictionService


class FakeModels:
    def __init__(self, model):
        self.model = model
        self.writes = []
        self.calls = []

    def champion(self, symbol):
        self.calls.append(("champion", symbol))
        return self.model if self.model.status == "champion" else None

    def active_shadow(self, symbol):
        self.calls.append(("shadow", symbol))
        return self.model if self.model.status == "shadow" else None

    def prediction_for(self, model_id, setup_id):
        self.calls.append(("prediction_for", model_id, setup_id))
        return None

    def write_prediction(self, prediction):
        self.calls.append(("write_prediction", prediction.model_id, prediction.setup_id))
        self.writes.append(prediction)
        return prediction


def _model(status: str) -> ModelRegistryEntry:
    seed = FeatureSnapshotV1(
        setup_id="seed",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        frozen_at=datetime(2026, 1, 1, tzinfo=UTC),
        direction=Direction.BUY,
        reference_price=4500.0,
    )
    encoder = V1FeatureEncoder.fit([raw_v1_features(seed)])
    return ModelRegistryEntry(
        model_id=f"m-{status}",
        symbol="XAUUSD",
        algorithm="logistic_regression_v1",
        status=status,
        feature_schema_version=FEATURE_SCHEMA_V1,
        label_schema_version=LABEL_SCHEMA_V1,
        model_schema_version=MODEL_SCHEMA_V1,
        trained_from="2026-01-01",
        trained_through="2026-01-31",
        training_samples=50,
        artifact={
            "encoder": encoder.to_dict(),
            "targets": {
                "clean_10": {"kind": "constant", "probability": 0.72},
                "reach_5": {"kind": "constant", "probability": 0.90},
                "reach_10": {"kind": "constant", "probability": 0.75},
                "reach_20": {"kind": "constant", "probability": 0.55},
                "reach_30": {"kind": "constant", "probability": 0.40},
                "reach_40": {"kind": "constant", "probability": 0.25},
            },
        },
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


def _setup_event():
    at = datetime(2026, 2, 2, 12, 0, tzinfo=UTC)
    setup = SimpleNamespace(
        setup_id="setup-1",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction_context=DirectionContext.BULLISH,
        anchor=SimpleNamespace(price=4500.0),
        context_summary=SimpleNamespace(
            volatility_regime="normal",
            session=None,
            mtf_alignment=SimpleNamespace(value="aligned"),
        ),
    )
    event = SimpleNamespace(
        event_id="event-1",
        market_time=SimpleNamespace(utc=at),
        context_snapshot={
            "reference_price": "4500.0",
            "ema_fast": "4501.0",
            "ema_slow": "4499.0",
            "rsi": "56",
            "atr": "8",
        },
    )
    return setup, event


def test_shadow_prediction_only_persists_intelligence() -> None:
    models = FakeModels(_model("shadow"))
    service = PredictionService(models)
    setup, event = _setup_event()

    intelligence = service.predict_shadow(setup, event)

    assert intelligence is not None
    assert intelligence.decision == "ML_SUPPORT"
    assert intelligence.recommended_target == 20
    assert len(models.writes) == 1
    # The fake repository exposes no broker/execution method. If PredictionService tried to
    # execute, this test would fail with AttributeError.
    assert all(call[0] in {"shadow", "prediction_for", "write_prediction"} for call in models.calls)


def test_schema_mismatch_returns_no_prediction() -> None:
    model = _model("shadow").model_copy(
        update={"feature_schema_version": "EOD_SETUP_FEATURES_V1"}
    )
    models = FakeModels(model)
    service = PredictionService(models)
    setup, event = _setup_event()

    assert service.predict_shadow(setup, event) is None
    assert models.writes == []
