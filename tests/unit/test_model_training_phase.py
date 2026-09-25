"""Model-phase regression tests: training, walk-forward chronology and shadow inference."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aureon.models.enums import DirectionContext, SetupFamily, Timeframe
from aureon.models.ml import ModelRegistryEntry
from aureon.models.training import TrainingExample
from aureon.services.model_backtest import WalkForwardBacktester
from aureon.services.model_training import fit_bundle
from aureon.services.shadow_model import ShadowModelService

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _example(index: int, day: int) -> TrainingExample:
    positive = index % 2 == 0
    return TrainingExample(
        example_id=f"ex-{day}-{index}",
        market_date=(datetime(2026, 8, 1, tzinfo=UTC) + timedelta(days=day)).date().isoformat(),
        symbol="XAUUSD",
        timeframe=Timeframe.M5 if index % 3 else Timeframe.M15,
        setup_id=f"setup-{day}-{index}",
        family=SetupFamily.MOMENTUM_TRANSITION,
        direction_context=DirectionContext.BULLISH,
        setup_version="1",
        context={
            "ema_fast": 2406.0 if positive else 2398.0,
            "ema_slow": 2400.0,
            "rsi": 62.0 if positive else 41.0,
            "trend": "bullish" if positive else "bearish",
            "session": "london",
            "mtf_alignment": "bullish" if positive else "mixed",
            "volatility_regime": "normal",
            "agent_confidence_pct": 83.0 if positive else 33.0,
        },
        agent_read={
            name: {
                "stance": "bullish" if positive else "bearish",
                "alignment": "aligned" if positive else "opposed",
                "observation": name,
            }
            for name in (
                "ema_cross",
                "rsi",
                "session_trend",
                "wick",
                "liquidity",
                "breakout",
            )
        },
        six_dollar_status="reached" if positive else "not_reached_eod",
        six_dollar_reached=positive,
        twenty_dollar_reached=index % 4 == 0,
        forty_dollar_reached=index % 8 == 0,
        generated_at=NOW,
    )


def test_baseline_model_trains_all_three_movement_targets() -> None:
    examples = [_example(index, index // 4) for index in range(64)]

    bundle = fit_bundle(
        examples,
        min_samples=20,
        min_class_samples=3,
    )

    assert set(bundle.models) == {"six", "twenty", "forty"}
    assert bundle.metrics["six"].samples == 64
    assert bundle.metrics["six"].roc_auc is not None
    assert bundle.metrics["six"].roc_auc > 0.9
    assert bundle.to_artifact()["model_schema_version"] == "AUREON_MOVE_MODEL_V1"


class _TrainingMemory:
    def __init__(self, examples):
        self.examples = examples

    def examples_between(self, symbol, start_market_date, end_market_date):
        return [
            example
            for example in self.examples
            if example.symbol == symbol
            and start_market_date <= example.market_date < end_market_date
        ]


class _ModelStore:
    def __init__(self):
        self.backtests = []
        self.predictions = {}
        self.shadow = None

    def write_backtest(self, value):
        self.backtests.append(value)
        return value

    def active_shadow(self, symbol):
        return self.shadow if self.shadow is not None and self.shadow.symbol == symbol else None

    def prediction_for(self, model_id, setup_id):
        return self.predictions.get((model_id, setup_id))

    def write_prediction(self, prediction):
        self.predictions[(prediction.model_id, prediction.setup_id)] = prediction
        return prediction


def test_walk_forward_folds_never_train_on_future_dates() -> None:
    examples = [
        _example(index, day)
        for day in range(14)
        for index in range(4)
    ]
    models = _ModelStore()

    result = WalkForwardBacktester(
        training_memory=_TrainingMemory(examples),
        models=models,
        now=lambda: NOW,
    ).run(
        "XAUUSD",
        min_train_days=6,
        test_days=2,
        min_train_samples=20,
        min_class_samples=2,
    )

    assert result.status == "complete"
    assert result.folds
    for fold in result.folds:
        assert fold.train_through < fold.test_from
    assert result.aggregate_metrics["six"].samples > 0


def test_shadow_service_writes_probabilities_only() -> None:
    examples = [_example(index, index // 4) for index in range(64)]
    bundle = fit_bundle(examples, min_samples=20, min_class_samples=3)
    model = ModelRegistryEntry(
        model_id="shadow-1",
        symbol="XAUUSD",
        status="shadow",
        feature_schema_version="EOD_SETUP_FEATURES_V1",
        label_schema_version="FAVOURABLE_MOVE_LADDER_V2",
        trained_from="2026-08-01",
        trained_through="2026-08-16",
        training_samples=len(examples),
        target_metrics=bundle.metrics,
        artifact=bundle.to_artifact(),
        created_at=NOW,
        activated_at=NOW,
    )
    store = _ModelStore()
    store.shadow = model

    setup = SimpleNamespace(
        setup_id="live-setup",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        family=SetupFamily.MOMENTUM_TRANSITION,
        direction_context=DirectionContext.BULLISH,
        context_summary=SimpleNamespace(
            session=SimpleNamespace(value="london"),
            mtf_alignment=SimpleNamespace(value="bullish"),
            volatility_regime="normal",
        ),
    )
    event = SimpleNamespace(
        event_id="confirm-1",
        market_time=SimpleNamespace(utc=NOW),
        context_snapshot={
            "ema_fast": "2406",
            "ema_slow": "2400",
            "rsi": "62",
            "trend": "bullish",
            "agent_confidence_pct": "83",
            **{
                f"agent_{name}_alignment": "aligned"
                for name in (
                    "ema_cross",
                    "rsi",
                    "session_trend",
                    "wick",
                    "liquidity",
                    "breakout",
                )
            },
        },
    )

    prediction = ShadowModelService(store).predict_setup(setup, event)

    assert prediction is not None
    assert set(prediction.probabilities) == {"six", "twenty", "forty"}
    assert all(0.0 <= value <= 1.0 for value in prediction.probabilities.values())
    assert len(store.predictions) == 1
