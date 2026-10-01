"""Deterministic historical EMA sequence labeler for Aureon V1.5 research.

Phase-1 only: this module creates research labels.  It does not train, score,
promote, shadow, or execute any model.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from aureon.models.base import to_utc
from aureon.models.enums import Direction
from aureon.models.sequence_v1 import (
    EMASequenceLabel,
    EMASequenceOutcome,
    EMASequenceRecord,
    EMASequenceSnapshot,
    ObservableCandidateSnapshot,
    CandidateOutcome,
    TargetLadder,
)

TARGETS = (6.0, 10.0, 20.0, 30.0, 40.0)


@dataclass(frozen=True)
class EMASequenceConfig:
    horizon_bars: int = 144
    continuation_move: float = 6.0
    pullback_min_move: float = 2.0
    deep_pullback_move: float = 6.0
    failure_move: float = 6.0

    def __post_init__(self) -> None:
        if self.horizon_bars < 1:
            raise ValueError("horizon_bars must be >= 1")
        for name in (
            "continuation_move",
            "pullback_min_move",
            "deep_pullback_move",
            "failure_move",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")


class EMASequenceLabeler:
    """Label full post-cross paths without leaking future data into the snapshot."""

    def __init__(self, config: EMASequenceConfig | None = None) -> None:
        self.config = config or EMASequenceConfig()

    def label(
        self,
        *,
        cross_detection: Any,
        frozen_context: dict[str, Any],
        future_candles: list[Any],
        observable_states: list[dict[str, Any]] | None = None,
    ) -> EMASequenceRecord:
        if getattr(cross_detection, "agent_name", None) != "ema_cross":
            raise ValueError("EMASequenceLabeler requires an ema_cross detection")
        if len(future_candles) < self.config.horizon_bars:
            raise ValueError("full configured future horizon is required")

        snapshot = self._snapshot(cross_detection, frozen_context)
        future = future_candles[: self.config.horizon_bars]
        first_future = to_utc(future[0].open_time.utc)
        if first_future <= snapshot.timestamp:
            raise ValueError("future candles must be strictly after the frozen cross timestamp")

        label = self._label_path(snapshot, future, observable_states or [])
        return EMASequenceRecord(snapshot=snapshot, label=label)

    def _snapshot(self, detection: Any, context: dict[str, Any]) -> EMASequenceSnapshot:
        direction = detection.direction
        if direction not in {Direction.BUY, Direction.SELL}:
            raise ValueError("EMA cross detection must have a directional side")

        evidence = detection.evidence
        numeric = dict(getattr(evidence, "numeric", {}) or {})
        categorical = dict(getattr(evidence, "categorical", {}) or {})
        indicators = detection.indicators
        at = to_utc(detection.detected_at.utc)
        cross_price = float(detection.price)
        sequence_id = hashlib.sha256(
            f"{detection.symbol}|{detection.timeframe.value}|{at.isoformat()}|"
            f"{direction.value}|{cross_price:.8f}".encode("utf-8")
        ).hexdigest()[:24]

        ema_fast = float(indicators.ema["fast"])
        ema_slow = float(indicators.ema["slow"])
        return EMASequenceSnapshot(
            sequence_id=sequence_id,
            symbol=detection.symbol,
            timeframe=detection.timeframe,
            timestamp=at,
            direction=direction,
            cross_price=cross_price,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            ema_fast_slope=_float(numeric.get("fast_slope")),
            ema_slow_slope=_float(numeric.get("slow_slope")),
            ema_separation=_float(numeric.get("ema_gap")) or (ema_fast - ema_slow),
            ema_separation_change=_float(numeric.get("ema_gap_change")),
            rsi=_float(getattr(indicators, "rsi", None)),
            rsi_change=_float(context.get("rsi_change")),
            atr=_float(context.get("atr")),
            htf_alignment=_text(context.get("htf_alignment") or context.get("htf_trend")),
            daily_market_bias=_text(
                context.get("daily_bias_at_entry") or context.get("daily_market_bias")
            ),
            market_regime=_text(context.get("market_regime") or context.get("regime")),
            volatility_regime=_text(context.get("volatility_regime")),
            session=_text(context.get("session") or context.get("current_session")),
            wick_state=_text(context.get("wick_state")),
            liquidity_state=_text(context.get("liquidity_state")),
            breakout_state=_text(context.get("breakout_state")),
            market_structure=_text(
                context.get("market_structure_state") or context.get("structure_state")
            ),
            participation=_text(
                context.get("participation_state") or context.get("volume_participation")
            ),
            agent_direction=_text(categorical.get("cross_direction")),
            agent_confidence=_float(context.get("agent_confidence")),
            agent_states=dict(context.get("agent_states") or {}),
            context=dict(context),
        )

    def _label_path(
        self, snapshot: EMASequenceSnapshot, future: list[Any], observable_states: list[dict[str, Any]]
    ) -> EMASequenceLabel:
        favourable_moves: list[float] = []
        adverse_moves: list[float] = []

        for bar in future:
            if snapshot.direction is Direction.BUY:
                favourable = max(0.0, float(bar.high) - snapshot.cross_price)
                adverse = max(0.0, snapshot.cross_price - float(bar.low))
            else:
                favourable = max(0.0, snapshot.cross_price - float(bar.low))
                adverse = max(0.0, float(bar.high) - snapshot.cross_price)
            favourable_moves.append(favourable)
            adverse_moves.append(adverse)

        max_favourable = max(favourable_moves, default=0.0)
        max_adverse = max(adverse_moves, default=0.0)
        first_cont = _first_reach(favourable_moves, self.config.continuation_move)
        first_pull = _first_reach(adverse_moves, self.config.pullback_min_move)
        first_fail = _first_reach(adverse_moves, self.config.failure_move)

        ambiguous = False
        if first_cont is not None and first_pull is not None and first_cont == first_pull:
            bar = future[first_cont - 1]
            if _same_bar_can_hit_both(
                snapshot.direction,
                snapshot.cross_price,
                bar,
                self.config.continuation_move,
                self.config.pullback_min_move,
            ):
                ambiguous = True

        if ambiguous:
            outcome = EMASequenceOutcome.AMBIGUOUS_PATH
        elif first_cont is not None and (first_pull is None or first_cont < first_pull):
            outcome = EMASequenceOutcome.IMMEDIATE_CONTINUATION
        elif first_pull is not None:
            post_pull_max = max(favourable_moves[first_pull:], default=0.0)
            if post_pull_max >= self.config.continuation_move:
                pullback_move = max(adverse_moves[: max(first_pull, 1)])
                outcome = (
                    EMASequenceOutcome.DEEP_PULLBACK_THEN_CONTINUATION
                    if pullback_move >= self.config.deep_pullback_move
                    else EMASequenceOutcome.PULLBACK_THEN_CONTINUATION
                )
            elif first_fail is not None or max_adverse >= self.config.failure_move:
                outcome = EMASequenceOutcome.FAILED_DIRECTION
            else:
                outcome = EMASequenceOutcome.AMBIGUOUS_PATH
        elif first_fail is not None:
            outcome = EMASequenceOutcome.FAILED_DIRECTION
        else:
            outcome = EMASequenceOutcome.AMBIGUOUS_PATH

        pullback_bars = first_pull
        pullback_move = 0.0
        if first_pull is not None:
            pullback_move = max(adverse_moves[:first_pull], default=0.0)

        continuation_start = 0 if first_pull is None else first_pull
        continuation_values = favourable_moves[continuation_start:]
        counter_values = adverse_moves[: first_cont or len(adverse_moves)]

        counter = self._candidate(
            snapshot, future, observable_states, kind="counter_move",
            wanted=_opposite(snapshot.direction), start_bar=first_pull or 1,
        )
        exhaustion = None
        continuation_candidate = None
        if counter is not None:
            counter_index = _candidate_index(future, counter.timestamp)
            exhaustion = self._candidate(
                snapshot, future, observable_states, kind="exhaustion",
                wanted=snapshot.direction, start_bar=counter_index + 2,
            )
            if exhaustion is not None:
                exhaustion_index = _candidate_index(future, exhaustion.timestamp)
                continuation_candidate = self._candidate(
                    snapshot, future, observable_states, kind="continuation",
                    wanted=snapshot.direction, start_bar=exhaustion_index + 1,
                ) or exhaustion

        counter_outcome = (
            _candidate_outcome(counter, future) if counter is not None else None
        )
        continuation_outcome = (
            _candidate_outcome(continuation_candidate, future)
            if continuation_candidate is not None else None
        )

        return EMASequenceLabel(
            outcome=outcome,
            horizon_bars=self.config.horizon_bars,
            pullback_move=pullback_move,
            pullback_bars=pullback_bars,
            continuation_move=max(continuation_values, default=0.0),
            max_favourable_move=max_favourable,
            max_adverse_move=max_adverse,
            continuation=_ladder(continuation_values),
            counter_move=_ladder(counter_values),
            counter_move_candidate=counter,
            counter_move_outcome=counter_outcome,
            exhaustion_candidate=exhaustion,
            continuation_candidate=continuation_candidate,
            continuation_candidate_outcome=continuation_outcome,
            path_ambiguous=ambiguous,
            diagnostics={
                "first_continuation_bar": first_cont,
                "first_pullback_bar": first_pull,
                "first_failure_bar": first_fail,
                "thresholds": {
                    "continuation_move": self.config.continuation_move,
                    "pullback_min_move": self.config.pullback_min_move,
                    "deep_pullback_move": self.config.deep_pullback_move,
                    "failure_move": self.config.failure_move,
                },
            },
        )



    def _candidate(
        self, snapshot: EMASequenceSnapshot, future: list[Any],
        states: list[dict[str, Any]], *, kind: str, wanted: Direction, start_bar: int,
    ) -> ObservableCandidateSnapshot | None:
        """First independently supported candidate using only that candle's state."""
        for index in range(max(0, start_bar - 1), min(len(future), len(states))):
            state = states[index] or {}
            try:
                evidence = state["detections"] or []
            except KeyError:
                evidence = []
            supporting = [
                item for item in evidence
                if _direction_value(item.get("direction")) is wanted
                and item.get("agent") != "ema_cross"
            ]
            # A price move alone is deliberately insufficient. At least one
            # independent deterministic directional observation is required.
            if not supporting:
                continue
            bar = future[index]
            timestamp = to_utc(bar.open_time.utc)
            entry = float(getattr(bar, "close"))
            cid = hashlib.sha256(
                f"{snapshot.sequence_id}|{kind}|{timestamp.isoformat()}|{wanted.value}|{entry:.8f}".encode()
            ).hexdigest()[:24]
            safe_context = {
                key: value for key, value in state.items()
                if key not in {"future", "outcome", "mfe", "mae", "targets"}
            }
            return ObservableCandidateSnapshot(
                candidate_id=cid, timestamp=timestamp, entry_price=entry,
                direction=wanted, candidate_type=kind,
                evidence_agents=sorted({str(item.get("agent")) for item in supporting}),
                evidence={"detections": supporting}, context=safe_context,
            )
        return None

def _first_reach(values: list[float], threshold: float) -> int | None:
    if threshold <= 0:
        return 1 if values else None
    for offset, value in enumerate(values, start=1):
        if value >= threshold:
            return offset
    return None


def _ladder(values: Iterable[float]) -> TargetLadder:
    sequence = list(values)
    hits: dict[float, int | None] = {}
    for target in TARGETS:
        hits[target] = _first_reach(sequence, target)
    return TargetLadder(
        reached_6=hits[6.0] is not None,
        reached_10=hits[10.0] is not None,
        reached_20=hits[20.0] is not None,
        reached_30=hits[30.0] is not None,
        reached_40=hits[40.0] is not None,
        bars_to_6=hits[6.0],
        bars_to_10=hits[10.0],
        bars_to_20=hits[20.0],
        bars_to_30=hits[30.0],
        bars_to_40=hits[40.0],
    )


def _same_bar_can_hit_both(
    direction: Direction,
    entry: float,
    bar: Any,
    continuation: float,
    pullback: float,
) -> bool:
    if direction is Direction.BUY:
        return float(bar.high) >= entry + continuation and float(bar.low) <= entry - pullback
    return float(bar.low) <= entry - continuation and float(bar.high) >= entry + pullback



def _opposite(direction: Direction) -> Direction:
    return Direction.SELL if direction is Direction.BUY else Direction.BUY


def _direction_value(value: Any) -> Direction | None:
    text = str(getattr(value, "value", value)).lower()
    if text in {"buy", "bullish", "long"}:
        return Direction.BUY
    if text in {"sell", "bearish", "short"}:
        return Direction.SELL
    return None


def _candidate_index(future: list[Any], timestamp: Any) -> int:
    at = to_utc(timestamp)
    for index, bar in enumerate(future):
        if to_utc(bar.open_time.utc) == at:
            return index
    raise ValueError("candidate timestamp is outside the sequence horizon")


def _candidate_outcome(candidate: ObservableCandidateSnapshot, future: list[Any]) -> CandidateOutcome:
    start = _candidate_index(future, candidate.timestamp) + 1
    bars = future[start:]
    favourable: list[float] = []
    adverse: list[float] = []
    for bar in bars:
        if candidate.direction is Direction.BUY:
            favourable.append(max(0.0, float(bar.high) - candidate.entry_price))
            adverse.append(max(0.0, candidate.entry_price - float(bar.low)))
        else:
            favourable.append(max(0.0, candidate.entry_price - float(bar.low)))
            adverse.append(max(0.0, float(bar.high) - candidate.entry_price))
    return CandidateOutcome(
        mfe=max(favourable, default=0.0),
        mae=max(adverse, default=0.0),
        targets=_ladder(favourable),
        bars_observed=len(bars),
    )


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))
