"""Aureon V3 EMA feature freeze, labels, model confidence and validation helpers."""
from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field

from aureon.models.base import AureonDocument, AureonModel, UtcDatetime, to_utc
from aureon.models.detection import Detection
from aureon.models.ema_journey_v3 import (
    EMA_FEATURE_SCHEMA_V3,
    EMA_LABEL_SCHEMA_V3,
    EMA_MODEL_SCHEMA_V3,
    AgentConfidenceSnapshot,
    EMAAnchorFeaturesV3,
    EMAJourneyAnchor,
    EMAMovementJourney,
)
from aureon.models.enums import Direction, Timeframe
from aureon.services.agent_consensus import build_agent_consensus


class EMAFailureType(StrEnum):
    NONE = "none"
    WHIPSAW = "whipsaw"
    FALSE_CROSS = "false_cross"
    LATE_CROSS = "late_cross"
    EXHAUSTED_MOVE = "exhausted_move"
    NO_CONTINUATION = "no_continuation"
    DEEP_PULLBACK = "deep_pullback"
    IMMEDIATE_REVERSAL = "immediate_reversal"


class EMAOutcomeV3(AureonModel):
    model_config = ConfigDict(frozen=True)

    label_schema: str = EMA_LABEL_SCHEMA_V3
    reached_5: bool
    reached_10: bool
    reached_20: bool
    reached_30: bool
    reached_40: bool
    clean_10: bool
    bars_to_5: int | None = None
    bars_to_10: int | None = None
    bars_to_20: int | None = None
    bars_to_30: int | None = None
    bars_to_40: int | None = None
    mfe: float = Field(ge=0)
    mae: float = Field(ge=0)
    failure_type: EMAFailureType = EMAFailureType.NONE
    resolved_at: UtcDatetime


class CanonicalEMAExampleV3(AureonDocument):
    example_id: str
    journey_id: str
    detection_id: str
    symbol: str
    timeframe: Timeframe
    direction: Direction
    market_date: str
    anchor_type: str
    feature_schema: str = EMA_FEATURE_SCHEMA_V3
    label_schema: str = EMA_LABEL_SCHEMA_V3
    features: EMAAnchorFeaturesV3
    movement_from_journey_start: float
    outcome: EMAOutcomeV3
    generated_at: UtcDatetime


class EMAModelConfidenceV3(AureonModel):
    """Historical model estimate. Separate from deterministic agent agreement."""

    model_id: str
    model_schema: str = EMA_MODEL_SCHEMA_V3
    sample_count: int = Field(ge=0)
    sufficient_data: bool
    out_of_distribution: bool = False
    probability_reach_5: float | None = Field(default=None, ge=0, le=1)
    probability_reach_10: float | None = Field(default=None, ge=0, le=1)
    probability_reach_20: float | None = Field(default=None, ge=0, le=1)
    probability_reach_30: float | None = Field(default=None, ge=0, le=1)
    probability_reach_40: float | None = Field(default=None, ge=0, le=1)
    probability_clean_10: float | None = Field(default=None, ge=0, le=1)
    expected_mfe: float | None = Field(default=None, ge=0)
    expected_mae: float | None = Field(default=None, ge=0)
    reason: str = ""


class EMAHoldoutDayV3(AureonDocument):
    holdout_id: str
    symbol: str
    market_date: str
    frozen_model_id: str | None
    status: str = "open"
    created_at: UtcDatetime
    scored_at: UtcDatetime | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


def freeze_anchor_features(
    detection: Detection,
    *,
    same_candle: list[Detection] | None = None,
) -> EMAAnchorFeaturesV3:
    """Freeze only facts knowable at the detection candle close."""
    evidence = detection.evidence
    numeric = evidence.numeric
    categorical = evidence.categorical
    ema = detection.indicators.ema or {}
    consensus = build_agent_consensus(detection, same_candle or [detection])

    internal_agents = {
        vote.agent: f"{vote.state}:{vote.event_key}"
        for vote in consensus.votes
    }
    agent_confidence = AgentConfidenceSnapshot(
        label=consensus.label,
        supportive=consensus.supportive,
        neutral=consensus.neutral,
        conflicting=consensus.conflicting,
        coverage=consensus.coverage,
        votes={vote.agent: vote.state for vote in consensus.votes},
    )

    seq = int(getattr(detection, "sequence_session", 0) or 0)
    if seq <= 24:
        session_phase = "EARLY"
    elif seq <= 72:
        session_phase = "MID"
    else:
        session_phase = "LATE"

    volatility_regime = "UNKNOWN"
    atr = None
    if detection.volatility is not None:
        atr = getattr(detection.volatility, "atr_14", None)
        volatility_regime = str(
            getattr(detection.volatility, "regime", None)
            or getattr(detection.volatility, "state", None)
            or "UNKNOWN"
        )

    htf_alignment = "UNKNOWN"
    if detection.mtf is not None:
        htf_alignment = str(
            getattr(detection.mtf, "alignment", None)
            or getattr(detection.mtf, "overall_alignment", None)
            or "UNKNOWN"
        )

    ema20 = ema.get("fast")
    ema50 = ema.get("slow")
    return EMAAnchorFeaturesV3(
        trend_direction=str(categorical.get("trend_direction", "UNKNOWN")),
        pattern=str(categorical.get("pre_cross_pattern", "UNKNOWN")),
        cross_quality=str(categorical.get("cross_quality", "UNKNOWN")),
        ema20=ema20,
        ema50=ema50,
        ema200=ema.get("ema200"),
        ema_gap=numeric.get("ema_gap") if "ema_gap" in numeric else (
            float(ema20) - float(ema50)
            if isinstance(ema20, (int, float)) and isinstance(ema50, (int, float))
            else None
        ),
        ema_gap_change=numeric.get("ema_gap_change"),
        ema_slope=numeric.get("fast_slope"),
        price_vs_ema200=str(
            categorical.get("price_relation")
            or categorical.get("ema200_context")
            or "UNKNOWN"
        ),
        ema20_50_relation=str(
            categorical.get("ema_relation")
            or categorical.get("ema20_50_context")
            or "UNKNOWN"
        ),
        rsi=detection.indicators.rsi,
        rsi_change=numeric.get("rsi_change"),
        atr=atr,
        volatility_regime=volatility_regime,
        tick_volume=numeric.get("tick_volume"),
        volume_ratio_to_median=numeric.get("tick_volume_ratio_to_median"),
        volume_percentile=numeric.get("tick_volume_percentile"),
        volume_state=str(categorical.get("volume_state", "UNKNOWN")),
        volume_price_alignment=str(
            categorical.get("volume_price_alignment", "UNKNOWN")
        ),
        pre_cross_volume_3bar_mean=numeric.get("tick_volume_3bar_mean"),
        pre_cross_volume_5bar_mean=numeric.get("tick_volume_5bar_mean"),
        spread_points=numeric.get("spread_points"),
        high_impact_news=bool(evidence.flags.get("high_impact_news", False)),
        news_event=categorical.get("news_event"),
        minutes_to_news=numeric.get("minutes_to_news"),
        session=detection.session.session.value,
        session_phase=session_phase,
        market_structure=str(
            categorical.get("market_structure")
            or categorical.get("structure")
            or "UNKNOWN"
        ),
        htf_alignment=htf_alignment,
        internal_agents=internal_agents,
        agent_confidence=agent_confidence,
    )


def classify_failure(anchor: EMAJourneyAnchor) -> EMAFailureType:
    """Outcome-only failure class. Never called before an anchor is complete."""
    outcome = anchor.outcome
    if not outcome.completed:
        raise ValueError("cannot classify an unresolved EMA anchor")
    if outcome.targets.get("10") and outcome.targets["10"].reached:
        if outcome.mae >= 10.0:
            return EMAFailureType.DEEP_PULLBACK
        return EMAFailureType.NONE
    if outcome.mfe < 3.0 and outcome.mae >= 10.0:
        return EMAFailureType.IMMEDIATE_REVERSAL
    if outcome.mfe < 5.0 and outcome.mae >= 5.0:
        return EMAFailureType.WHIPSAW
    if outcome.mfe < 5.0:
        return EMAFailureType.NO_CONTINUATION
    if anchor.movement_from_journey_start >= 20.0 and outcome.mfe < 10.0:
        return EMAFailureType.LATE_CROSS
    if outcome.mfe < 10.0:
        return EMAFailureType.EXHAUSTED_MOVE
    return EMAFailureType.FALSE_CROSS


def canonical_example(
    journey: EMAMovementJourney,
    anchor: EMAJourneyAnchor,
    *,
    generated_at: datetime,
    clean_max_mae: float = 7.0,
) -> CanonicalEMAExampleV3:
    if not anchor.outcome.completed or anchor.outcome.completed_at is None:
        raise ValueError("EMA anchor outcome must be complete before training")
    targets = anchor.outcome.targets
    reached = lambda key: bool(targets.get(key) and targets[key].reached)
    clean_10 = reached("10") and anchor.outcome.mae <= clean_max_mae
    outcome = EMAOutcomeV3(
        reached_5=reached("5"),
        reached_10=reached("10"),
        reached_20=reached("20"),
        reached_30=reached("30"),
        reached_40=reached("40"),
        clean_10=clean_10,
        bars_to_5=targets.get("5").bars_to if targets.get("5") else None,
        bars_to_10=targets.get("10").bars_to if targets.get("10") else None,
        bars_to_20=targets.get("20").bars_to if targets.get("20") else None,
        bars_to_30=targets.get("30").bars_to if targets.get("30") else None,
        bars_to_40=targets.get("40").bars_to if targets.get("40") else None,
        mfe=anchor.outcome.mfe,
        mae=anchor.outcome.mae,
        failure_type=classify_failure(anchor),
        resolved_at=to_utc(anchor.outcome.completed_at),
    )
    example_id = hashlib.sha256(
        (
            f"{journey.journey_id}|{anchor.detection_id}|"
            f"{EMA_FEATURE_SCHEMA_V3}|{EMA_LABEL_SCHEMA_V3}"
        ).encode()
    ).hexdigest()
    return CanonicalEMAExampleV3(
        example_id=example_id,
        journey_id=journey.journey_id,
        detection_id=anchor.detection_id,
        symbol=journey.symbol,
        timeframe=journey.timeframe,
        direction=journey.direction,
        market_date=journey.market_date,
        anchor_type=anchor.anchor_type.value,
        features=anchor.features,
        movement_from_journey_start=anchor.movement_from_journey_start,
        outcome=outcome,
        generated_at=to_utc(generated_at),
    )


def calibration_buckets(
    probabilities: list[float],
    labels: list[bool],
) -> dict[str, dict[str, float | int | None]]:
    if len(probabilities) != len(labels):
        raise ValueError("probabilities/labels length mismatch")
    buckets: dict[str, dict[str, float | int | None]] = {}
    for lo in range(0, 100, 10):
        hi = lo + 10
        idx = [
            i for i, p in enumerate(probabilities)
            if lo / 100 <= p < hi / 100 or (hi == 100 and p == 1.0)
        ]
        if not idx:
            buckets[f"{lo}-{hi}"] = {"n": 0, "predicted": None, "actual": None}
            continue
        buckets[f"{lo}-{hi}"] = {
            "n": len(idx),
            "predicted": sum(probabilities[i] for i in idx) / len(idx),
            "actual": sum(1 for i in idx if labels[i]) / len(idx),
        }
    return buckets
