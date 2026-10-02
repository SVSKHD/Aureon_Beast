"""Aureon V4 journey derivation and canonical movement examples (TODO 001-020).

Everything in this module is derived from already-observed V3 anchors.  It never
looks at candles that were unavailable at an anchor timestamp.
"""
from __future__ import annotations

from aureon.models.base import to_utc
from aureon.models.ema_journey_v3 import (
    EMAAnchorType,
    EMAJourneyAnchor,
    EMAMovementJourney,
    JourneyEndReason,
    JourneyStatus,
)
from aureon.models.ema_journey_v4 import (
    PreCrossFailureType,
    V4JourneyEvent,
    V4JourneyPhase,
    V4JourneySnapshot,
    V4MovementFeatures,
    V4PreCrossExample,
    V4PreCrossLabels,
    V4RemainingMovementExample,
    V4RemainingMovementLabels,
)

_CONFIRMED = {EMAAnchorType.EMA20_50_CROSS, EMAAnchorType.EMA200_CROSS}


def event_sequence(journey: EMAMovementJourney) -> tuple[V4JourneyEvent, ...]:
    """Stable event order for pressure -> 20/50 -> 200 sequences."""
    return tuple(
        V4JourneyEvent(
            index=index,
            event_type=anchor.anchor_type.value,
            detected_at=anchor.detected_at,
            price=anchor.price,
            movement_from_start=anchor.movement_from_journey_start,
        )
        for index, anchor in enumerate(journey.anchors)
    )


def _pre_and_first_cross(
    journey: EMAMovementJourney,
) -> tuple[EMAJourneyAnchor | None, EMAJourneyAnchor | None]:
    pre = next(
        (a for a in journey.anchors if a.anchor_type is EMAAnchorType.PRE_CROSS),
        None,
    )
    cross = next((a for a in journey.anchors if a.anchor_type in _CONFIRMED), None)
    return pre, cross


def pre_cross_metrics(journey: EMAMovementJourney) -> tuple[float, int, float]:
    """Movement and elapsed bars from first pressure to first confirmed cross."""
    pre, cross = _pre_and_first_cross(journey)
    if pre is None or cross is None:
        return 0.0, 0, 0.0
    seconds = max(0.0, (to_utc(cross.detected_at) - to_utc(pre.detected_at)).total_seconds())
    bars = int(round(seconds / max(1, journey.timeframe.seconds)))
    movement = max(0.0, cross.movement_from_journey_start - pre.movement_from_journey_start)
    return movement, bars, seconds


def cross_order(journey: EMAMovementJourney) -> str:
    kinds = [
        a.anchor_type.value
        for a in journey.anchors
        if a.anchor_type in _CONFIRMED
    ]
    return "->".join(kinds) if kinds else "NONE"


def derive_phase(journey: EMAMovementJourney) -> V4JourneyPhase:
    if journey.status is JourneyStatus.INVALID:
        return V4JourneyPhase.INVALID
    if journey.status is JourneyStatus.CLOSED:
        return V4JourneyPhase.CLOSED
    confirmed = [a for a in journey.anchors if a.anchor_type in _CONFIRMED]
    if not confirmed:
        return V4JourneyPhase.PRE_CROSS
    if any(a.outcome.mfe > 0 for a in confirmed):
        return V4JourneyPhase.EXPANSION
    return V4JourneyPhase.CONFIRMED_CROSS


def build_v4_journey_snapshot(journey: EMAMovementJourney) -> V4JourneySnapshot:
    movement, bars, seconds = pre_cross_metrics(journey)
    return V4JourneySnapshot(
        journey_id=journey.journey_id,
        symbol=journey.symbol,
        timeframe=journey.timeframe,
        direction=journey.direction,
        phase=derive_phase(journey),
        event_sequence=event_sequence(journey),
        pre_cross_movement=movement,
        pre_cross_bars=bars,
        pre_cross_seconds=seconds,
        movement_consumed_at_latest_cross=journey.movement_consumed_before_latest_cross,
        cross_order=cross_order(journey),
    )


def _pre_cross_failure(journey: EMAMovementJourney) -> PreCrossFailureType:
    if journey.has_confirmed_cross:
        return PreCrossFailureType.NONE
    if journey.status is JourneyStatus.INVALID:
        return PreCrossFailureType.INVALID
    if journey.end_reason is JourneyEndReason.PRE_CROSS_EXPIRED:
        return PreCrossFailureType.EXPIRED
    if journey.end_reason is JourneyEndReason.MARKET_DAY_CHANGE:
        return PreCrossFailureType.MARKET_DAY_CHANGE
    if journey.end_reason is JourneyEndReason.DATA_GAP:
        return PreCrossFailureType.DATA_GAP
    return PreCrossFailureType.NO_CONFIRMED_CROSS


def movement_features(
    journey: EMAMovementJourney,
    anchor: EMAJourneyAnchor,
) -> V4MovementFeatures:
    movement, bars, seconds = pre_cross_metrics(journey)
    return V4MovementFeatures(
        anchor_type=anchor.anchor_type.value,
        base=anchor.features,
        movement_consumed=max(0.0, anchor.movement_from_journey_start),
        pre_cross_movement=movement,
        pre_cross_bars=bars,
        pre_cross_seconds=seconds,
        cross_order=cross_order(journey),
    )


def pre_cross_example(journey: EMAMovementJourney) -> V4PreCrossExample | None:
    """One pressure example; failed pressure is retained rather than discarded."""
    pre, cross = _pre_and_first_cross(journey)
    if pre is None:
        return None

    bars_to_cross: int | None = None
    if cross is not None:
        seconds = max(
            0.0,
            (to_utc(cross.detected_at) - to_utc(pre.detected_at)).total_seconds(),
        )
        bars_to_cross = max(1, int(round(seconds / max(1, journey.timeframe.seconds))))

    return V4PreCrossExample(
        journey_id=journey.journey_id,
        detection_id=pre.detection_id,
        symbol=journey.symbol,
        timeframe=journey.timeframe,
        direction=journey.direction,
        market_date=journey.market_date,
        features=movement_features(journey, pre),
        labels=V4PreCrossLabels(
            cross_within_1=bars_to_cross is not None and bars_to_cross <= 1,
            cross_within_3=bars_to_cross is not None and bars_to_cross <= 3,
            cross_within_6=bars_to_cross is not None and bars_to_cross <= 6,
            cross_within_12=bars_to_cross is not None and bars_to_cross <= 12,
            bars_to_cross=bars_to_cross,
            failure_type=_pre_cross_failure(journey),
        ),
    )


def _reached(anchor: EMAJourneyAnchor, key: str) -> bool:
    row = anchor.outcome.targets.get(key)
    return bool(row and row.reached)


def remaining_movement_examples(
    journey: EMAMovementJourney,
) -> list[V4RemainingMovementExample]:
    """Future movement measured from each confirmed cross, not journey start."""
    rows: list[V4RemainingMovementExample] = []
    for anchor in journey.anchors:
        if anchor.anchor_type not in _CONFIRMED:
            continue
        if not anchor.outcome.completed or not anchor.outcome.valid:
            continue
        rows.append(
            V4RemainingMovementExample(
                journey_id=journey.journey_id,
                detection_id=anchor.detection_id,
                symbol=journey.symbol,
                timeframe=journey.timeframe,
                direction=journey.direction,
                market_date=journey.market_date,
                features=movement_features(journey, anchor),
                labels=V4RemainingMovementLabels(
                    reached_3=_reached(anchor, "3"),
                    reached_5=_reached(anchor, "5"),
                    reached_10=_reached(anchor, "10"),
                    reached_20=_reached(anchor, "20"),
                    reached_30=_reached(anchor, "30"),
                    reached_40=_reached(anchor, "40"),
                    remaining_mfe=anchor.outcome.mfe,
                    remaining_mae=anchor.outcome.mae,
                ),
            )
        )
    return rows


def placement_comparison(journeys: list[EMAMovementJourney]) -> dict[str, dict[str, float | int]]:
    """Compare only placements the market actually exposed."""
    buckets: dict[str, list[V4RemainingMovementExample]] = {}
    for journey in journeys:
        for row in remaining_movement_examples(journey):
            buckets.setdefault(row.features.anchor_type, []).append(row)

    result: dict[str, dict[str, float | int]] = {}
    for anchor_type, rows in buckets.items():
        n = len(rows)
        result[anchor_type] = {
            "samples": n,
            "mean_movement_consumed": sum(r.features.movement_consumed for r in rows) / n,
            "mean_remaining_mfe": sum(r.labels.remaining_mfe for r in rows) / n,
            "mean_remaining_mae": sum(r.labels.remaining_mae for r in rows) / n,
            "reach_3_rate": sum(r.labels.reached_3 for r in rows) / n,
            "reach_5_rate": sum(r.labels.reached_5 for r in rows) / n,
            "reach_10_rate": sum(r.labels.reached_10 for r in rows) / n,
        }
    return result
