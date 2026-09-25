"""Logical Agent 17: Expansion Opportunity Agent.

Finds the beginning and re-entry windows of unusually large directional moves. It does
not execute. Its main product is training evidence: which observable entry window existed
before a move expanded, and how far price subsequently travelled.

Three entry candidates are recorded deliberately:
- earliest: aggressive first reclaim/break;
- confirmation: evidence has aligned;
- pullback: first retracement into the fast-EMA / trigger zone.

That lets review compare entry quality without rewriting history after the move is known.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from aureon.models.agent_decision import (
    ExpansionEntryCandidate,
    ExpansionFamily,
    ExpansionOpportunity,
    ExpansionPhase,
    HigherTimeframeAssessment,
    HtfState,
)
from aureon.models.enums import Direction


@dataclass(frozen=True)
class ExpansionInputs:
    frame: pd.DataFrame
    price: float
    ema_fast: float | None
    ema_slow: float | None
    previous_ema_fast: float | None
    previous_ema_slow: float | None
    rsi: float | None
    atr: float | None
    journey: dict[str, object] | None = None
    regime: dict[str, object] | None = None
    participation: dict[str, object] | None = None
    htf: HigherTimeframeAssessment | None = None
    candle_signals: tuple[tuple[str, Direction | None], ...] = ()
    as_of: object | None = None


class ExpansionOpportunityAgent:
    agent_name = "expansion_opportunity"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        primary_move: float = 10.0,
        atr_multiple: float = 2.5,
        lookback_bars: int = 36,
        entry_zone_atr: float = 0.35,
        min_strength_for_entry: int = 5,
    ) -> None:
        self.primary_move = primary_move
        self.atr_multiple = atr_multiple
        self.lookback_bars = lookback_bars
        self.entry_zone_atr = entry_zone_atr
        self.min_strength_for_entry = min_strength_for_entry

    def assess(self, inputs: ExpansionInputs) -> ExpansionOpportunity:
        frame = inputs.frame.iloc[-self.lookback_bars:]
        if len(frame) < 8:
            return ExpansionOpportunity(
                expansion_threshold=self.primary_move,
                evidence=("insufficient recent candles",),
                as_of=inputs.as_of,
            )

        direction = self._direction(inputs)
        if direction is None:
            return ExpansionOpportunity(
                expansion_threshold=self._threshold(inputs),
                evidence=("no directional expansion hypothesis yet",),
                as_of=inputs.as_of,
            )

        threshold = self._threshold(inputs)
        lows = frame["low"].astype(float)
        highs = frame["high"].astype(float)
        closes = frame["close"].astype(float)
        anchor = float(lows.min()) if direction is Direction.BUY else float(highs.max())
        move = (inputs.price - anchor) * direction.sign

        family = self._family(inputs, direction)
        evidence, blockers = self._evidence(inputs, direction, family)
        strength = len(evidence)
        total = 8

        fast = inputs.ema_fast
        zone_pad = (inputs.atr or threshold / 2.5) * self.entry_zone_atr
        zone_center = fast if fast is not None else inputs.price
        zone_low = zone_center - zone_pad
        zone_high = zone_center + zone_pad

        # Earliest observable candidate: first structural reclaim/break event on this candle.
        trigger_names = {
            name for name, side in inputs.candle_signals if side is direction
        }
        earliest = None
        if trigger_names & {"liquidity", "wick", "breakout"}:
            earliest = ExpansionEntryCandidate(
                style="earliest",
                price=inputs.price,
                rationale="first directional structure event while expansion evidence is forming",
            )

        confirmation = None
        if strength >= self.min_strength_for_entry and not blockers:
            confirmation = ExpansionEntryCandidate(
                style="confirmation",
                price=inputs.price,
                rationale=f"{strength}/{total} observable expansion conditions aligned",
            )

        pullback = None
        if (
            strength >= self.min_strength_for_entry
            and fast is not None
            and float(frame["low"].iloc[-1]) <= zone_high
            and float(frame["high"].iloc[-1]) >= zone_low
        ):
            pullback = ExpansionEntryCandidate(
                style="pullback",
                price=float(fast),
                rationale="first retracement into the fast-EMA expansion zone",
            )

        if move >= threshold:
            phase = ExpansionPhase.EXPANDING
        elif pullback is not None or confirmation is not None:
            phase = ExpansionPhase.ENTRY_WINDOW
        elif strength >= max(3, self.min_strength_for_entry - 2):
            phase = ExpansionPhase.ARMED
        else:
            phase = ExpansionPhase.WATCH

        # If price has already run most of the threshold without an entry candidate,
        # explicitly mark it missed rather than encouraging a chase.
        if move >= threshold * 0.75 and earliest is None and confirmation is None and pullback is None:
            phase = ExpansionPhase.MISSED
            blockers.append("move already extended; do not manufacture a late entry")

        invalidation = (
            float(frame["low"].iloc[-3:].min())
            if direction is Direction.BUY
            else float(frame["high"].iloc[-3:].max())
        )

        signature = "|".join(
            [
                family.value,
                direction.value,
                _regime(inputs.regime),
                _participation(inputs.participation),
                _htf(inputs.htf),
            ]
        )

        return ExpansionOpportunity(
            phase=phase,
            family=family,
            direction=direction,
            strength=strength,
            strength_total=total,
            move_from_anchor=move,
            anchor_price=anchor,
            expansion_threshold=threshold,
            earliest_entry=earliest,
            confirmation_entry=confirmation,
            pullback_entry=pullback,
            preferred_zone_low=zone_low if strength >= 3 else None,
            preferred_zone_high=zone_high if strength >= 3 else None,
            invalidation_reference=invalidation,
            evidence=tuple(evidence),
            blockers=tuple(blockers),
            signature=signature,
            as_of=inputs.as_of,
        )

    def _threshold(self, inputs: ExpansionInputs) -> float:
        dynamic = (inputs.atr or 0.0) * self.atr_multiple
        return max(self.primary_move, dynamic)

    def _direction(self, inputs: ExpansionInputs) -> Direction | None:
        buy = sell = 0

        if inputs.ema_fast is not None and inputs.ema_slow is not None:
            if inputs.ema_fast > inputs.ema_slow:
                buy += 2
            elif inputs.ema_fast < inputs.ema_slow:
                sell += 2

        if inputs.previous_ema_fast is not None and inputs.ema_fast is not None:
            if inputs.ema_fast > inputs.previous_ema_fast:
                buy += 1
            elif inputs.ema_fast < inputs.previous_ema_fast:
                sell += 1

        if inputs.rsi is not None:
            if inputs.rsi >= 55:
                buy += 1
            elif inputs.rsi <= 45:
                sell += 1

        for _name, side in inputs.candle_signals:
            if side is Direction.BUY:
                buy += 1
            elif side is Direction.SELL:
                sell += 1

        if inputs.htf is not None:
            if inputs.htf.state is HtfState.BULLISH:
                buy += 1
            elif inputs.htf.state is HtfState.BEARISH:
                sell += 1

        if buy == sell:
            return None
        return Direction.BUY if buy > sell else Direction.SELL

    def _family(self, inputs: ExpansionInputs, direction: Direction) -> ExpansionFamily:
        signals = {name for name, side in inputs.candle_signals if side is direction}
        opposite_liquidity = any(
            name == "liquidity" and side is direction for name, side in inputs.candle_signals
        )
        if opposite_liquidity or "wick" in signals:
            return ExpansionFamily.REVERSAL
        if "breakout" in signals:
            return ExpansionFamily.BREAKOUT
        if inputs.ema_fast is not None and inputs.ema_slow is not None:
            return ExpansionFamily.CONTINUATION
        return ExpansionFamily.UNKNOWN

    def _evidence(
        self,
        inputs: ExpansionInputs,
        direction: Direction,
        family: ExpansionFamily,
    ) -> tuple[list[str], list[str]]:
        evidence: list[str] = []
        blockers: list[str] = []

        if inputs.ema_fast is not None and inputs.ema_slow is not None:
            aligned = (
                inputs.ema_fast > inputs.ema_slow
                if direction is Direction.BUY
                else inputs.ema_fast < inputs.ema_slow
            )
            if aligned:
                evidence.append("EMA fast/slow aligned")

        if inputs.previous_ema_fast is not None and inputs.ema_fast is not None:
            slope_ok = (
                inputs.ema_fast > inputs.previous_ema_fast
                if direction is Direction.BUY
                else inputs.ema_fast < inputs.previous_ema_fast
            )
            if slope_ok:
                evidence.append("fast EMA slope supports expansion")

        if inputs.rsi is not None:
            momentum = inputs.rsi >= 55 if direction is Direction.BUY else inputs.rsi <= 45
            if momentum:
                evidence.append("RSI momentum supportive")

        if inputs.htf is not None:
            aligned = (
                inputs.htf.state is HtfState.BULLISH
                if direction is Direction.BUY
                else inputs.htf.state is HtfState.BEARISH
            )
            if aligned:
                evidence.append("higher timeframe aligned")
            elif inputs.htf.state in {HtfState.BULLISH, HtfState.BEARISH}:
                blockers.append("higher timeframe opposes expansion")

        regime = _regime(inputs.regime)
        if regime in {"trending", "trend_expansion", "expansion"}:
            evidence.append(f"regime {regime}")
        elif regime in {"volatile_chop", "structurally_messy"}:
            blockers.append(f"regime {regime}")

        participation = _participation(inputs.participation)
        if participation in {"expanding", "abnormal_expansion"}:
            evidence.append(f"participation {participation}")

        signals = {name for name, side in inputs.candle_signals if side is direction}
        if signals & {"liquidity", "wick", "breakout"}:
            evidence.append("fresh structure trigger")

        if family is ExpansionFamily.REVERSAL:
            evidence.append("reversal expansion family")
        elif family is ExpansionFamily.CONTINUATION:
            evidence.append("continuation expansion family")
        elif family is ExpansionFamily.BREAKOUT:
            evidence.append("breakout expansion family")

        return evidence, blockers


def _regime(block: dict[str, object] | None) -> str:
    if not block:
        return "unknown"
    return str(block.get("regime") or "unknown")


def _participation(block: dict[str, object] | None) -> str:
    if not block:
        return "unknown"
    return str(block.get("state") or "unknown")


def _htf(htf: HigherTimeframeAssessment | None) -> str:
    return "unknown" if htf is None else htf.state.value
