"""Aureon V4 Gold pullback, re-entry and phase-volume tracking (TODO 021-040).

The tracker consumes only closed candles plus indicator values already available at
that candle.  Re-entry observations are research anchors, never execution requests.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from aureon.models.base import to_utc
from aureon.models.ema_journey_v3 import EMAAnchorType, EMAJourneyAnchor, EMAMovementJourney
from aureon.models.ema_journey_v4 import (
    V4PullbackClass,
    V4PullbackState,
    V4PullbackStatus,
    V4ReentryObservation,
    V4RetestKind,
    V4VolumeTrajectory,
)
from aureon.models.enums import Direction
from aureon.models.market import Candle

_CONFIRMED = {EMAAnchorType.EMA20_50_CROSS, EMAAnchorType.EMA200_CROSS}


@dataclass
class _JourneyRuntime:
    anchor_detection_id: str
    origin_price: float
    extreme_price: float
    previous_close: float | None = None
    volume: V4VolumeTrajectory = field(default_factory=V4VolumeTrajectory)
    pullback: V4PullbackState | None = None
    reentries: list[V4ReentryObservation] = field(default_factory=list)


class V4PullbackTracker:
    """Track expansion -> pullback -> research re-entry from one V4 EMA journey."""

    def __init__(
        self,
        *,
        minimum_expansion: float = 3.0,
        minimum_pullback: float = 0.5,
        pullback_atr_fraction: float = 0.25,
        shallow_fraction: float = 0.25,
        deep_fraction: float = 0.50,
        structure_failure_fraction: float = 0.75,
        retest_atr_tolerance: float = 0.10,
        reentry_failure_move: float = 3.0,
    ) -> None:
        if minimum_expansion <= 0 or minimum_pullback <= 0:
            raise ValueError("movement thresholds must be positive")
        if not 0 < shallow_fraction < deep_fraction < structure_failure_fraction <= 1:
            raise ValueError("pullback fraction thresholds are incoherent")
        self.minimum_expansion = float(minimum_expansion)
        self.minimum_pullback = float(minimum_pullback)
        self.pullback_atr_fraction = float(pullback_atr_fraction)
        self.shallow_fraction = float(shallow_fraction)
        self.deep_fraction = float(deep_fraction)
        self.structure_failure_fraction = float(structure_failure_fraction)
        self.retest_atr_tolerance = float(retest_atr_tolerance)
        self.reentry_failure_move = float(reentry_failure_move)
        self._runtime: dict[str, _JourneyRuntime] = {}

    def on_closed_candle(
        self,
        journey: EMAMovementJourney,
        candle: Candle,
        *,
        ema20: float | None,
        ema50: float | None,
        ema200: float | None,
        atr: float | None,
        structure_level: float | None = None,
    ) -> V4PullbackState | None:
        """Advance one journey using facts from this closed candle only."""
        anchor = self._latest_confirmed(journey)
        if anchor is None or candle.close_time <= to_utc(anchor.detected_at):
            return self.active_pullback(journey.journey_id)

        runtime = self._runtime.get(journey.journey_id)
        if runtime is None or runtime.anchor_detection_id != anchor.detection_id:
            runtime = self._new_runtime(journey, anchor)
            self._runtime[journey.journey_id] = runtime

        # Outcomes are updated before a possible new observation.  The candle that
        # creates a re-entry observation is therefore never also its first future bar.
        self._advance_reentries(runtime, candle)

        previous_extreme = runtime.extreme_price
        runtime.extreme_price = self._favourable_extreme(
            journey.direction,
            runtime.extreme_price,
            candle,
        )
        expansion = self._directional_move(
            journey.direction,
            runtime.origin_price,
            runtime.extreme_price,
        )

        if runtime.pullback is None:
            runtime.volume.observe_expansion(candle.tick_volume)
            depth_close = self._pullback_from_extreme(
                journey.direction,
                runtime.extreme_price,
                candle.close,
            )
            threshold = max(
                self.minimum_pullback,
                (float(atr) * self.pullback_atr_fraction)
                if atr is not None and atr > 0
                else 0.0,
            )
            # A new high/low on this candle is expansion, not a pullback merely
            # because the candle also has a wick back from that new extreme.
            expanded_this_bar = runtime.extreme_price != previous_extreme
            if (
                expansion >= self.minimum_expansion
                and depth_close >= threshold
                and not expanded_this_bar
            ):
                runtime.pullback = self._start_pullback(
                    journey,
                    candle,
                    runtime,
                    expansion=expansion,
                    atr=atr,
                    ema20=ema20,
                    ema50=ema50,
                    ema200=ema200,
                    structure_level=structure_level,
                )
        else:
            self._advance_pullback(
                journey,
                candle,
                runtime,
                atr=atr,
                ema20=ema20,
                ema50=ema50,
                ema200=ema200,
                structure_level=structure_level,
            )

        runtime.previous_close = candle.close
        return runtime.pullback

    def close_journey(self, journey: EMAMovementJourney) -> None:
        """Finalize unresolved research anchors when the parent journey closes."""
        runtime = self._runtime.get(journey.journey_id)
        if runtime is None:
            return
        if runtime.pullback is not None and runtime.pullback.status is V4PullbackStatus.ACTIVE:
            runtime.pullback.status = V4PullbackStatus.CLOSED
        for observation in runtime.reentries:
            if observation.outcome.completed:
                continue
            if not observation.outcome.reached_3:
                observation.outcome.failed = True
                observation.outcome.failure_reason = "journey_closed_without_plus_3"
            observation.outcome.completed = True

    def active_pullback(self, journey_id: str) -> V4PullbackState | None:
        runtime = self._runtime.get(journey_id)
        return None if runtime is None else runtime.pullback

    def reentries_for(self, journey_id: str) -> list[V4ReentryObservation]:
        runtime = self._runtime.get(journey_id)
        return [] if runtime is None else list(runtime.reentries)

    def volume_for(self, journey_id: str) -> V4VolumeTrajectory | None:
        runtime = self._runtime.get(journey_id)
        return None if runtime is None else runtime.volume.model_copy(deep=True)

    def _new_runtime(
        self,
        journey: EMAMovementJourney,
        anchor: EMAJourneyAnchor,
    ) -> _JourneyRuntime:
        pre = next(
            (row for row in journey.anchors if row.anchor_type is EMAAnchorType.PRE_CROSS),
            None,
        )
        volume = V4VolumeTrajectory(
            pre_cross_3bar_mean=(
                pre.features.pre_cross_volume_3bar_mean if pre is not None else None
            ),
            pre_cross_5bar_mean=(
                pre.features.pre_cross_volume_5bar_mean if pre is not None else None
            ),
            cross_tick_volume=anchor.features.tick_volume,
            cross_volume_ratio=anchor.features.volume_ratio_to_median,
        )
        return _JourneyRuntime(
            anchor_detection_id=anchor.detection_id,
            origin_price=anchor.price,
            extreme_price=anchor.price,
            volume=volume,
        )

    def _start_pullback(
        self,
        journey: EMAMovementJourney,
        candle: Candle,
        runtime: _JourneyRuntime,
        *,
        expansion: float,
        atr: float | None,
        ema20: float | None,
        ema50: float | None,
        ema200: float | None,
        structure_level: float | None,
    ) -> V4PullbackState:
        depth = self._pullback_depth(journey.direction, runtime.extreme_price, candle)
        fraction = depth / expansion if expansion > 0 else 0.0
        classification = self._classify(fraction)
        retests = self._retests(
            candle,
            ema20=ema20,
            ema50=ema50,
            ema200=ema200,
            atr=atr,
            structure_level=structure_level,
        )
        structure_intact = self._structure_intact(
            journey.direction,
            candle,
            expansion_fraction=fraction,
            structure_level=structure_level,
        )
        return V4PullbackState(
            pullback_id=self._pullback_id(journey.journey_id, candle),
            journey_id=journey.journey_id,
            symbol=journey.symbol,
            timeframe=journey.timeframe,
            direction=journey.direction,
            started_at=candle.close_time,
            start_price=candle.close,
            expansion_origin_price=runtime.origin_price,
            expansion_extreme_price=runtime.extreme_price,
            expansion_move=expansion,
            atr_at_start=atr,
            bars=1,
            depth=depth,
            depth_atr=(depth / atr if atr is not None and atr > 0 else None),
            retracement_fraction=fraction,
            classification=classification,
            retests=retests,
            structure_level=structure_level,
            structure_intact=structure_intact,
            ema_aligned=self._ema_aligned(
                journey.direction,
                candle.close,
                ema20=ema20,
                ema50=ema50,
                ema200=ema200,
            ),
            latest_price=candle.close,
            latest_at=candle.close_time,
            status=(
                V4PullbackStatus.ACTIVE
                if structure_intact
                else V4PullbackStatus.FAILED
            ),
        )

    def _advance_pullback(
        self,
        journey: EMAMovementJourney,
        candle: Candle,
        runtime: _JourneyRuntime,
        *,
        atr: float | None,
        ema20: float | None,
        ema50: float | None,
        ema200: float | None,
        structure_level: float | None,
    ) -> None:
        pullback = runtime.pullback
        assert pullback is not None
        if pullback.status in {
            V4PullbackStatus.FAILED,
            V4PullbackStatus.CONTINUED,
            V4PullbackStatus.CLOSED,
        }:
            return

        pullback.bars += 1
        depth = self._pullback_depth(
            journey.direction,
            pullback.expansion_extreme_price,
            candle,
        )
        pullback.depth = max(pullback.depth, depth)
        pullback.depth_atr = (
            pullback.depth / atr if atr is not None and atr > 0 else pullback.depth_atr
        )
        pullback.retracement_fraction = (
            pullback.depth / pullback.expansion_move
            if pullback.expansion_move > 0
            else 0.0
        )
        pullback.classification = self._classify(pullback.retracement_fraction)
        pullback.retests = tuple(
            dict.fromkeys(
                (
                    *pullback.retests,
                    *self._retests(
                        candle,
                        ema20=ema20,
                        ema50=ema50,
                        ema200=ema200,
                        atr=atr,
                        structure_level=structure_level,
                    ),
                )
            )
        )
        pullback.structure_intact = (
            pullback.structure_intact
            and self._structure_intact(
                journey.direction,
                candle,
                expansion_fraction=pullback.retracement_fraction,
                structure_level=structure_level,
            )
        )
        pullback.ema_aligned = self._ema_aligned(
            journey.direction,
            candle.close,
            ema20=ema20,
            ema50=ema50,
            ema200=ema200,
        )
        pullback.latest_price = candle.close
        pullback.latest_at = candle.close_time

        if not pullback.structure_intact:
            pullback.status = V4PullbackStatus.FAILED
            return

        if (
            not runtime.reentries
            and pullback.ema_aligned
            and self._resumed(journey.direction, runtime.previous_close, candle.close)
        ):
            observation = self._make_reentry(
                journey,
                candle,
                pullback,
                runtime.volume,
                ema20=ema20,
                ema50=ema50,
                ema200=ema200,
                atr=atr,
            )
            runtime.reentries.append(observation)
            pullback.status = V4PullbackStatus.REENTRY_OBSERVED

    def _advance_reentries(self, runtime: _JourneyRuntime, candle: Candle) -> None:
        for observation in runtime.reentries:
            outcome = observation.outcome
            if outcome.completed or candle.close_time <= to_utc(observation.observed_at):
                continue
            outcome.bars_observed += 1
            favourable, adverse = self._candle_moves(
                observation.direction,
                observation.price,
                candle,
            )
            outcome.mfe = max(outcome.mfe, favourable)
            outcome.mae = max(outcome.mae, adverse)
            outcome.reached_3 = outcome.reached_3 or outcome.mfe >= 3.0
            outcome.reached_5 = outcome.reached_5 or outcome.mfe >= 5.0
            outcome.reached_10 = outcome.reached_10 or outcome.mfe >= 10.0
            if not outcome.reached_3 and outcome.mae >= self.reentry_failure_move:
                outcome.failed = True
                outcome.failure_reason = "adverse_before_plus_3"
                outcome.completed = True
                if runtime.pullback is not None:
                    runtime.pullback.status = V4PullbackStatus.FAILED
            elif outcome.reached_10:
                outcome.completed = True
                if runtime.pullback is not None:
                    runtime.pullback.status = V4PullbackStatus.CONTINUED

    def _make_reentry(
        self,
        journey: EMAMovementJourney,
        candle: Candle,
        pullback: V4PullbackState,
        volume: V4VolumeTrajectory,
        *,
        ema20: float | None,
        ema50: float | None,
        ema200: float | None,
        atr: float | None,
    ) -> V4ReentryObservation:
        # _advance_pullback requires alignment, so these cannot be None here.
        assert ema20 is not None and ema50 is not None
        digest = hashlib.sha256(
            f"{journey.journey_id}|{pullback.pullback_id}|{candle.close_time.isoformat()}".encode()
        ).hexdigest()[:20]
        return V4ReentryObservation(
            reentry_id=f"v4re_{digest}",
            pullback_id=pullback.pullback_id,
            journey_id=journey.journey_id,
            symbol=journey.symbol,
            timeframe=journey.timeframe,
            direction=journey.direction,
            observed_at=candle.close_time,
            price=candle.close,
            ema20=float(ema20),
            ema50=float(ema50),
            ema200=(float(ema200) if ema200 is not None else None),
            atr=atr,
            pullback_depth=pullback.depth,
            pullback_fraction=pullback.retracement_fraction,
            pullback_class=pullback.classification,
            structure_intact=pullback.structure_intact,
            ema_aligned=pullback.ema_aligned,
            retests=pullback.retests,
            volume=volume.model_copy(deep=True),
        )

    def _classify(self, fraction: float) -> V4PullbackClass:
        if fraction < self.shallow_fraction:
            return V4PullbackClass.SHALLOW
        if fraction <= self.deep_fraction:
            return V4PullbackClass.NORMAL
        return V4PullbackClass.DEEP

    def _structure_intact(
        self,
        direction: Direction,
        candle: Candle,
        *,
        expansion_fraction: float,
        structure_level: float | None,
    ) -> bool:
        if expansion_fraction >= self.structure_failure_fraction:
            return False
        if structure_level is None:
            return True
        if direction is Direction.BUY:
            return candle.low >= structure_level
        return candle.high <= structure_level

    def _ema_aligned(
        self,
        direction: Direction,
        price: float,
        *,
        ema20: float | None,
        ema50: float | None,
        ema200: float | None,
    ) -> bool:
        if ema20 is None or ema50 is None:
            return False
        if direction is Direction.BUY:
            return ema20 > ema50 and (ema200 is None or price > ema200)
        return ema20 < ema50 and (ema200 is None or price < ema200)

    def _retests(
        self,
        candle: Candle,
        *,
        ema20: float | None,
        ema50: float | None,
        ema200: float | None,
        atr: float | None,
        structure_level: float | None,
    ) -> tuple[V4RetestKind, ...]:
        tolerance = (
            max(0.0, float(atr)) * self.retest_atr_tolerance
            if atr is not None
            else 0.0
        )
        found: list[V4RetestKind] = []
        for value, kind in (
            (ema20, V4RetestKind.EMA20),
            (ema50, V4RetestKind.EMA50),
            (ema200, V4RetestKind.EMA200),
            (structure_level, V4RetestKind.STRUCTURE),
        ):
            if value is not None and self._touches(candle, float(value), tolerance):
                found.append(kind)
        return tuple(found)

    @staticmethod
    def _touches(candle: Candle, value: float, tolerance: float) -> bool:
        return candle.low - tolerance <= value <= candle.high + tolerance

    @staticmethod
    def _resumed(
        direction: Direction,
        previous_close: float | None,
        current_close: float,
    ) -> bool:
        if previous_close is None:
            return False
        return (
            current_close > previous_close
            if direction is Direction.BUY
            else current_close < previous_close
        )

    @staticmethod
    def _latest_confirmed(journey: EMAMovementJourney) -> EMAJourneyAnchor | None:
        return next(
            (
                anchor
                for anchor in reversed(journey.anchors)
                if anchor.anchor_type in _CONFIRMED
            ),
            None,
        )

    @staticmethod
    def _directional_move(direction: Direction, start: float, end: float) -> float:
        return max(0.0, end - start if direction is Direction.BUY else start - end)

    @staticmethod
    def _favourable_extreme(
        direction: Direction,
        current: float,
        candle: Candle,
    ) -> float:
        return (
            max(current, candle.high)
            if direction is Direction.BUY
            else min(current, candle.low)
        )

    @staticmethod
    def _pullback_from_extreme(
        direction: Direction,
        extreme: float,
        price: float,
    ) -> float:
        return max(0.0, extreme - price if direction is Direction.BUY else price - extreme)

    @staticmethod
    def _pullback_depth(
        direction: Direction,
        extreme: float,
        candle: Candle,
    ) -> float:
        return max(
            0.0,
            extreme - candle.low if direction is Direction.BUY else candle.high - extreme,
        )

    @staticmethod
    def _candle_moves(
        direction: Direction,
        reference: float,
        candle: Candle,
    ) -> tuple[float, float]:
        if direction is Direction.BUY:
            return max(0.0, candle.high - reference), max(0.0, reference - candle.low)
        return max(0.0, reference - candle.low), max(0.0, candle.high - reference)

    @staticmethod
    def _pullback_id(journey_id: str, candle: Candle) -> str:
        digest = hashlib.sha256(
            f"{journey_id}|{candle.close_time.isoformat()}|pullback".encode()
        ).hexdigest()[:20]
        return f"v4pb_{digest}"


def compare_cross_vs_reentry(
    *,
    cross_rows: list[dict[str, float | bool]],
    reentries: list[V4ReentryObservation],
) -> dict[str, dict[str, float | int]]:
    """Compare observed cross placement with observed pullback re-entry placement."""

    def _summary(rows: list[dict[str, float | bool]]) -> dict[str, float | int]:
        if not rows:
            return {"samples": 0}
        count = len(rows)
        return {
            "samples": count,
            "mean_mfe": sum(float(row["mfe"]) for row in rows) / count,
            "mean_mae": sum(float(row["mae"]) for row in rows) / count,
            "reach_3_rate": sum(bool(row["reach_3"]) for row in rows) / count,
            "reach_5_rate": sum(bool(row["reach_5"]) for row in rows) / count,
            "reach_10_rate": sum(bool(row["reach_10"]) for row in rows) / count,
        }

    reentry_rows = [
        {
            "mfe": row.outcome.mfe,
            "mae": row.outcome.mae,
            "reach_3": row.outcome.reached_3,
            "reach_5": row.outcome.reached_5,
            "reach_10": row.outcome.reached_10,
        }
        for row in reentries
        if row.outcome.completed
    ]
    return {
        "cross": _summary(cross_rows),
        "pullback_reentry": _summary(reentry_rows),
    }
