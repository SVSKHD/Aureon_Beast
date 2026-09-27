"""Shared builders for the V1 gap-closure tests. Not a test module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.learning_v1 import (
    FEATURE_SCHEMA_V1,
    LABEL_SCHEMA_V1,
    MODEL_SCHEMA_V1,
    CanonicalTrainingExample,
    CleanMoveOutcomeV1,
    FeatureSnapshotV1,
)
from aureon.models.ml import ModelRegistryEntry, TargetMetrics
from aureon.services.daily_market_bias import DailyBiasInputs
from aureon.services.learning_contract import canonical_example

START = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)


def snapshot(
    index: int,
    *,
    direction: Direction = Direction.BUY,
    session: str = "london",
    daily_bias: str = "bullish",
    volatility: str = "normal",
    htf: str = "aligned",
    at: datetime | None = None,
    symbol: str = "XAUUSD",
) -> FeatureSnapshotV1:
    moment = at or (START + timedelta(minutes=5 * index))
    return FeatureSnapshotV1(
        setup_id=f"setup-{index}",
        symbol=symbol,
        timeframe=Timeframe.M5,
        timestamp=moment,
        frozen_at=moment,
        direction=direction,
        reference_price=2400.0 + index * 0.1,
        ema_fast=2401.0,
        ema_slow=2399.0,
        rsi=55.0 + (index % 7),
        atr=4.0,
        session=session,
        current_session=session,
        daily_bias_at_entry=daily_bias,
        daily_bias_strength_at_entry=0.6,
        session_bias_at_entry=daily_bias,
        session_bias_strength_at_entry=0.5,
        volatility_regime=volatility,
        htf_alignment=htf,
        market_regime="trending",
    )


def outcome(*, clean: bool, resolved_at: datetime | None = None) -> CleanMoveOutcomeV1:
    return CleanMoveOutcomeV1(
        resolved_at=resolved_at,
        horizon_bars=12,
        clean_10=clean,
        reached_5=True,
        reached_10=clean,
        reached_20=clean,
        max_favourable_move=22.0 if clean else 4.0,
        max_adverse_move=3.0 if clean else 9.0,
        mae_before_10=3.0 if clean else None,
    )


def example(
    index: int,
    *,
    clean: bool,
    market_date: str | None = None,
    **kwargs,
) -> CanonicalTrainingExample:
    features = snapshot(index, **kwargs)
    resolved = features.timestamp + timedelta(minutes=60)
    return canonical_example(
        features=features,
        outcome=outcome(clean=clean, resolved_at=resolved),
        market_date=market_date or features.timestamp.date().isoformat(),
        generated_at=resolved,
    )


def examples(
    count: int,
    *,
    clean_every: int = 2,
    days: int = 30,
    per_day: int | None = None,
    **kwargs,
) -> list[CanonicalTrainingExample]:
    """``count`` examples spread over ``days`` market dates, alternating outcomes."""
    per_day = per_day or max(1, count // days)
    result = []
    for index in range(count):
        day = index // per_day
        at = START + timedelta(days=day, minutes=5 * (index % per_day))
        features = snapshot(index, at=at, **kwargs)
        resolved = at + timedelta(minutes=60)
        result.append(
            canonical_example(
                features=features,
                outcome=outcome(clean=(index % clean_every == 0), resolved_at=resolved),
                market_date=at.date().isoformat(),
                generated_at=resolved,
            )
        )
    return result


def constant_model(
    model_id: str,
    *,
    status: str = "champion",
    clean: float = 0.7,
    trained_through: str = "2026-01-31",
    generation: int = 0,
    validation_metrics: dict | None = None,
) -> ModelRegistryEntry:
    from aureon.ml.v1_features import V1FeatureEncoder, raw_v1_features

    encoder = V1FeatureEncoder.fit([raw_v1_features(snapshot(0))])
    return ModelRegistryEntry(
        model_id=model_id,
        symbol="XAUUSD",
        algorithm="logistic_regression_v1",
        status=status,
        feature_schema_version=FEATURE_SCHEMA_V1,
        label_schema_version=LABEL_SCHEMA_V1,
        model_schema_version=MODEL_SCHEMA_V1,
        generation=generation,
        trained_from="2026-01-01",
        trained_through=trained_through,
        training_samples=50,
        target_metrics={
            "clean_10": TargetMetrics(
                samples=30,
                positives=15,
                negatives=15,
                precision=0.7,
                recall=0.7,
                brier=0.2,
                roc_auc=0.7,
                false_positive_rate=0.3,
            )
        },
        validation_metrics=validation_metrics or {},
        artifact={
            "encoder": encoder.to_dict(),
            "targets": {
                "clean_10": {"kind": "constant", "probability": clean},
                "reach_5": {"kind": "constant", "probability": 0.9},
                "reach_10": {"kind": "constant", "probability": 0.7},
                "reach_20": {"kind": "constant", "probability": 0.5},
                "reach_30": {"kind": "constant", "probability": 0.3},
                "reach_40": {"kind": "constant", "probability": 0.2},
            },
        },
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


def bias_inputs(
    index: int,
    *,
    close: float,
    open_: float,
    session: SessionName,
    market_date: str = "2026-01-05",
    ema_fast: float | None = None,
    ema_slow: float | None = None,
    rsi: float | None = 50.0,
    htf: str | None = None,
    session_trend: str | None = None,
    regime: dict | None = None,
    sweep: dict | None = None,
    wick: dict | None = None,
) -> DailyBiasInputs:
    return DailyBiasInputs(
        symbol="XAUUSD",
        timeframe="M5",
        at=datetime(2026, 1, 5, 0, 0, tzinfo=UTC) + timedelta(minutes=5 * index),
        market_date=market_date,
        session=session,
        open=open_,
        high=max(open_, close) + 0.2,
        low=min(open_, close) - 0.2,
        close=close,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        previous_ema_fast=None if ema_fast is None else ema_fast - (close - open_) * 0.5,
        rsi=rsi,
        atr=2.0,
        regime=regime,
        htf_trend=htf,
        session_live_trend=session_trend,
        sweep=sweep,
        wick=wick,
    )


def drift_day(agent, *, per_bar: float, bars: int = 120, start_price: float = 2000.0, **kw):
    """Feed ``bars`` candles with a constant drift through ASIA/LONDON/NEW_YORK."""
    price = start_price
    snap = None
    for index in range(bars):
        session = (
            SessionName.ASIA
            if index < bars // 3
            else SessionName.LONDON
            if index < 2 * bars // 3
            else SessionName.NEW_YORK
        )
        opened, closed = price, price + per_bar
        snap = agent.observe(
            bias_inputs(
                index,
                close=closed,
                open_=opened,
                session=session,
                ema_fast=closed - per_bar,
                ema_slow=closed - 4 * per_bar,
                rsi=50 + (20 if per_bar > 0 else -20 if per_bar < 0 else 0),
                htf="bullish" if per_bar > 0 else "bearish" if per_bar < 0 else "sideways",
                session_trend="up" if per_bar > 0 else "down" if per_bar < 0 else "flat",
                regime={
                    "regime": "trending" if per_bar else "ranging",
                    "volatility_state": "normal",
                    "structure_state": "trend" if per_bar else "range",
                },
                **kw,
            )
        )
        price = closed
    return snap
