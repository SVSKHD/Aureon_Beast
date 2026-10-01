"""Stateful V3 EMA movement journey and outcome tracker.

This service groups related EMA events into one directional journey and measures
what price did after every anchor. It is deterministic over detections + candles.

Important: movement is research movement from the frozen detection price. It is
not an executable fill model; spread/slippage/reference-price work is handled by
later V3 tasks.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime

from aureon.models.base import to_utc
from aureon.models.detection import Detection
from aureon.models.ema_journey_v3 import (
    EMA_MOVEMENT_TARGETS,
    EMAAnchorOutcome,
    EMAAnchorType,
    EMAJourneyAnchor,
    EMAMovementJourney,
    JourneyEndReason,
    JourneyStatus,
    TargetOutcome,
)
from aureon.models.enums import Direction
from aureon.models.market import Candle

EMA_ANCHOR_AGENTS: dict[str, EMAAnchorType] = {
    "ema200_pre_cross": EMAAnchorType.PRE_CROSS,
    "ema_cross": EMAAnchorType.EMA20_50_CROSS,
    "ema200_cross": EMAAnchorType.EMA200_CROSS,
}
CONFIRMED_CROSS_AGENTS = frozenset({"ema_cross", "ema200_cross"})


class EMAMovementJourneyTracker:
    """Track linked EMA journeys and their future price movement."""

    def __init__(
        self,
        *,
        repository: object | None = None,
        max_horizon_bars: int = 96,
        max_link_bars: int = 96,
        gap_tolerance_bars: int = 2,
    ) -> None:
        if max_horizon_bars < 1:
            raise ValueError("max_horizon_bars must be positive")
        if max_link_bars < 1:
            raise ValueError("max_link_bars must be positive")
        if gap_tolerance_bars < 1:
            raise ValueError("gap_tolerance_bars must be positive")
        self.repository = repository
        self.max_horizon_bars = max_horizon_bars
        self.max_link_bars = max_link_bars
        self.gap_tolerance_bars = gap_tolerance_bars
        self._active: dict[tuple[str, str], EMAMovementJourney] = {}

    def restore(self, journeys: list[EMAMovementJourney]) -> None:
        """Restore open journeys after restart."""
        for journey in journeys:
            if journey.status is JourneyStatus.OPEN:
                self._active[(journey.symbol, journey.timeframe.value)] = journey

    def on_detection(self, detection: Detection) -> EMAMovementJourney | None:
        anchor_type = EMA_ANCHOR_AGENTS.get(detection.agent_name)
        if anchor_type is None or detection.direction is None:
            return None

        key = (detection.symbol, detection.timeframe.value)
        current = self._active.get(key)

        if current is not None and self._is_opposite_confirmed(current, detection):
            self._close(
                current,
                at=detection.detected_at.utc,
                reason=JourneyEndReason.OPPOSITE_CONFIRMED_CROSS,
            )
            current = None

        if current is not None and not self._can_link(current, detection):
            self._close(
                current,
                at=detection.detected_at.utc,
                reason=JourneyEndReason.MAX_HORIZON,
            )
            current = None

        if current is None:
            current = self._new_journey(detection)
            self._active[key] = current

        if detection.direction is not current.direction:
            # Opposite pre-pressure does not kill a confirmed journey; it begins no
            # new journey until a confirmed opposite cross arrives.
            return None

        if any(a.detection_id == detection.detection_id for a in current.anchors):
            return current

        movement = self._directional_move(
            current.direction, current.start_price, detection.price
        )
        anchor = EMAJourneyAnchor(
            detection_id=detection.detection_id,
            anchor_type=anchor_type,
            direction=detection.direction,
            detected_at=detection.detected_at.utc,
            price=detection.price,
            movement_from_journey_start=movement,
            outcome=self._empty_outcome(),
        )
        current.anchors = tuple((*current.anchors, anchor))
        self._save(current)
        return current

    def on_closed_candle(self, candle: Candle) -> list[EMAMovementJourney]:
        key = (candle.symbol, candle.timeframe.value)
        journey = self._active.get(key)
        if journey is None or journey.status is not JourneyStatus.OPEN:
            return []

        # Never count the event candle itself as future movement.
        if candle.close_time <= journey.started_at:
            return []

        if candle.open_time.market_date != journey.market_date:
            self._close(
                journey,
                at=candle.open_time.utc,
                reason=JourneyEndReason.MARKET_DAY_CHANGE,
            )
            return [journey]

        if (
            journey.last_candle_close_at is not None
            and (
                candle.open_time.utc - to_utc(journey.last_candle_close_at)
            ).total_seconds()
            > candle.timeframe.seconds * self.gap_tolerance_bars
        ):
            self._close(
                journey,
                at=candle.open_time.utc,
                reason=JourneyEndReason.DATA_GAP,
                invalid=True,
            )
            return [journey]

        journey.bars_observed += 1
        journey.last_candle_close_at = candle.close_time
        favourable, adverse = self._candle_moves(
            journey.direction,
            journey.start_price,
            candle,
        )
        journey.max_favourable_move = max(journey.max_favourable_move, favourable)
        journey.max_adverse_move = max(journey.max_adverse_move, adverse)

        updated_anchors: list[EMAJourneyAnchor] = []
        for anchor in journey.anchors:
            if anchor.outcome.completed or candle.close_time <= anchor.detected_at:
                updated_anchors.append(anchor)
                continue
            outcome = self._advance_anchor(anchor, candle)
            updated_anchors.append(anchor.model_copy(update={"outcome": outcome}))
        journey.anchors = tuple(updated_anchors)

        if journey.bars_observed >= self.max_horizon_bars:
            self._close(
                journey,
                at=candle.close_time,
                reason=JourneyEndReason.MAX_HORIZON,
            )
        else:
            self._save(journey)
        return [journey]

    def active_for(self, symbol: str, timeframe: str) -> EMAMovementJourney | None:
        return self._active.get((symbol, timeframe))

    def _new_journey(self, detection: Detection) -> EMAMovementJourney:
        journey_id = self._journey_id(detection)
        journey = EMAMovementJourney(
            journey_id=journey_id,
            account_scope=detection.account_scope,
            symbol=detection.symbol,
            timeframe=detection.timeframe,
            direction=detection.direction,
            market_date=detection.detected_at.market_date,
            started_at=detection.detected_at.utc,
            start_price=detection.price,
            anchors=(),
        )
        self._save(journey)
        return journey

    def _can_link(self, journey: EMAMovementJourney, detection: Detection) -> bool:
        if detection.direction is not journey.direction:
            return False
        if detection.detected_at.market_date != journey.market_date:
            return False
        seconds = (
            detection.detected_at.utc - to_utc(journey.started_at)
        ).total_seconds()
        return 0 <= seconds <= detection.timeframe.seconds * self.max_link_bars

    @staticmethod
    def _is_opposite_confirmed(
        journey: EMAMovementJourney,
        detection: Detection,
    ) -> bool:
        return (
            detection.agent_name in CONFIRMED_CROSS_AGENTS
            and detection.direction is not None
            and detection.direction is not journey.direction
        )

    def _advance_anchor(
        self,
        anchor: EMAJourneyAnchor,
        candle: Candle,
    ) -> EMAAnchorOutcome:
        outcome = anchor.outcome.model_copy(deep=True)
        outcome.bars_observed += 1
        favourable, adverse = self._candle_moves(anchor.direction, anchor.price, candle)
        outcome.mfe = max(outcome.mfe, favourable)
        outcome.mae = max(outcome.mae, adverse)

        elapsed = max(
            0.0,
            (candle.close_time - to_utc(anchor.detected_at)).total_seconds(),
        )
        targets = dict(outcome.targets)
        for target in EMA_MOVEMENT_TARGETS:
            key = self._target_key(target)
            existing = targets.get(key, TargetOutcome())
            if not existing.reached and favourable >= target:
                targets[key] = TargetOutcome(
                    reached=True,
                    bars_to=outcome.bars_observed,
                    seconds_to=elapsed,
                )
            elif key not in targets:
                targets[key] = existing
        outcome.targets = targets

        if outcome.bars_observed >= self.max_horizon_bars:
            outcome.completed = True
            outcome.end_reason = JourneyEndReason.MAX_HORIZON
            outcome.completed_at = candle.close_time
        return outcome

    def _close(
        self,
        journey: EMAMovementJourney,
        *,
        at: datetime,
        reason: JourneyEndReason,
        invalid: bool = False,
    ) -> None:
        updated: list[EMAJourneyAnchor] = []
        for anchor in journey.anchors:
            if anchor.outcome.completed:
                updated.append(anchor)
                continue
            outcome = anchor.outcome.model_copy(
                update={
                    "completed": True,
                    "end_reason": reason,
                    "completed_at": to_utc(at),
                }
            )
            updated.append(anchor.model_copy(update={"outcome": outcome}))
        journey.anchors = tuple(updated)
        journey.status = JourneyStatus.INVALID if invalid else JourneyStatus.CLOSED
        journey.ended_at = to_utc(at)
        journey.end_reason = reason
        self._save(journey)
        self._active.pop((journey.symbol, journey.timeframe.value), None)

    def _empty_outcome(self) -> EMAAnchorOutcome:
        return EMAAnchorOutcome(
            targets={
                self._target_key(target): TargetOutcome()
                for target in EMA_MOVEMENT_TARGETS
            }
        )

    @staticmethod
    def _candle_moves(
        direction: Direction,
        reference: float,
        candle: Candle,
    ) -> tuple[float, float]:
        if direction is Direction.BUY:
            favourable = max(0.0, candle.high - reference)
            adverse = max(0.0, reference - candle.low)
        else:
            favourable = max(0.0, reference - candle.low)
            adverse = max(0.0, candle.high - reference)
        return favourable, adverse

    @staticmethod
    def _directional_move(direction: Direction, start: float, end: float) -> float:
        return end - start if direction is Direction.BUY else start - end

    @staticmethod
    def _target_key(target: float) -> str:
        return f"{int(target) if target.is_integer() else target:g}"

    @staticmethod
    def _journey_id(detection: Detection) -> str:
        raw = (
            f"{detection.account_scope}|{detection.symbol}|"
            f"{detection.timeframe.value}|{detection.direction.value}|"
            f"{detection.detection_id}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _save(self, journey: EMAMovementJourney) -> None:
        if self.repository is not None:
            self.repository.upsert(journey)
