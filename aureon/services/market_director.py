"""Logical Agent 15: Market Director.

Combines current closed-candle facts into WAIT/WATCH/FORMING/READY. It does not place
orders. Its score is confluence under a transparent rule, not a win probability.
"""

from __future__ import annotations

from dataclasses import dataclass

from aureon.models.agent_decision import (
    DirectorDecision,
    DirectorState,
    HigherTimeframeAssessment,
    HtfState,
)
from aureon.models.enums import Direction, TrendBias


@dataclass(frozen=True)
class DirectorInputs:
    price: float
    ema_fast: float | None = None
    ema_slow: float | None = None
    rsi: float | None = None
    session_trend: str | None = None
    journey: dict[str, object] | None = None
    regime: dict[str, object] | None = None
    participation: dict[str, object] | None = None
    htf: HigherTimeframeAssessment | None = None
    candle_signals: tuple[tuple[str, Direction | None], ...] = ()
    as_of: object | None = None


class MarketDirector:
    agent_name = "market_director"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        primary_target_move: float = 10.0,
        ready_support: int = 6,
        forming_support: int = 4,
        rsi_bullish: float = 52.0,
        rsi_bearish: float = 48.0,
    ) -> None:
        if primary_target_move <= 0:
            raise ValueError("primary_target_move must be positive")
        if ready_support <= forming_support:
            raise ValueError("ready_support must exceed forming_support")
        self.primary_target_move = primary_target_move
        self.ready_support = ready_support
        self.forming_support = forming_support
        self.rsi_bullish = rsi_bullish
        self.rsi_bearish = rsi_bearish

    def decide(self, inputs: DirectorInputs) -> DirectorDecision:
        direction = self._direction(inputs)
        if direction is None:
            return DirectorDecision(
                state=DirectorState.WAIT,
                primary_target_move=self.primary_target_move,
                evidence=("no directional majority yet",),
                trigger_required="wait for directional evidence to agree",
                as_of=inputs.as_of,
            )

        evidence: list[str] = []
        blockers: list[str] = []
        supporting = opposing = neutral = 0

        def vote(name: str, value: Direction | None, detail: str) -> None:
            nonlocal supporting, opposing, neutral
            if value is None:
                neutral += 1
                evidence.append(f"{name}: neutral ({detail})")
            elif value is direction:
                supporting += 1
                evidence.append(f"{name}: supports {direction.value} ({detail})")
            else:
                opposing += 1
                evidence.append(f"{name}: opposes {direction.value} ({detail})")

        ema_direction = None
        if inputs.ema_fast is not None and inputs.ema_slow is not None:
            if inputs.ema_fast > inputs.ema_slow:
                ema_direction = Direction.BUY
            elif inputs.ema_fast < inputs.ema_slow:
                ema_direction = Direction.SELL
        vote("EMA", ema_direction, "fast/slow relation")

        rsi_direction = None
        if inputs.rsi is not None:
            if inputs.rsi >= self.rsi_bullish:
                rsi_direction = Direction.BUY
            elif inputs.rsi <= self.rsi_bearish:
                rsi_direction = Direction.SELL
        vote("RSI", rsi_direction, f"{inputs.rsi}" if inputs.rsi is not None else "unknown")

        session_direction = {
            "up": Direction.BUY,
            "down": Direction.SELL,
        }.get(inputs.session_trend or "")
        vote("Session", session_direction, inputs.session_trend or "unknown")

        journey_direction = _journey_direction(inputs.journey)
        vote("Journey", journey_direction, _field(inputs.journey, "state", "unknown"))

        participation_direction = _participation_direction(inputs.participation)
        vote(
            "Participation",
            participation_direction,
            _field(inputs.participation, "price_impulse", "unknown"),
        )

        htf_direction = None
        if inputs.htf is not None:
            if inputs.htf.state is HtfState.BULLISH:
                htf_direction = Direction.BUY
            elif inputs.htf.state is HtfState.BEARISH:
                htf_direction = Direction.SELL
        vote("HTF", htf_direction, inputs.htf.state.value if inputs.htf else "unknown")

        for agent, signal_direction in inputs.candle_signals:
            vote(agent, signal_direction, "current-candle event")

        regime_name = _field(inputs.regime, "regime", "unknown")
        if regime_name in {"volatile_chop", "structurally_messy"}:
            blockers.append(f"market regime is {regime_name}")
        elif regime_name == "range_compression":
            blockers.append("market is compressed; breakout confirmation required")

        # HTF disagreement is context, not an automatic veto. A current reversal can be real,
        # but it must not receive READY while every available HTF opinion is opposite.
        if inputs.htf is not None:
            if (
                direction is Direction.BUY
                and inputs.htf.state is HtfState.BEARISH
            ) or (
                direction is Direction.SELL
                and inputs.htf.state is HtfState.BULLISH
            ):
                blockers.append("higher timeframes are against the proposed direction")

        has_structure_trigger = any(
            agent in {"breakout", "liquidity", "wick"} and signal is direction
            for agent, signal in inputs.candle_signals
        )

        if blockers:
            state = DirectorState.WATCH if supporting >= self.forming_support else DirectorState.WAIT
            trigger = blockers[0]
        elif supporting >= self.ready_support and has_structure_trigger:
            state = DirectorState.READY
            trigger = None
        elif supporting >= self.forming_support:
            state = DirectorState.FORMING
            trigger = "wait for breakout/liquidity/wick structure confirmation"
        elif supporting > opposing:
            state = DirectorState.WATCH
            trigger = "need stronger confluence"
        else:
            state = DirectorState.WAIT
            trigger = "directional evidence is not sufficiently aligned"

        return DirectorDecision(
            state=state,
            direction=direction,
            primary_target_move=self.primary_target_move,
            supporting=supporting,
            opposing=opposing,
            neutral=neutral,
            evidence=tuple(evidence),
            blockers=tuple(blockers),
            trigger_required=trigger,
            as_of=inputs.as_of,
        )

    @staticmethod
    def _direction(inputs: DirectorInputs) -> Direction | None:
        buy = sell = 0

        if inputs.ema_fast is not None and inputs.ema_slow is not None:
            if inputs.ema_fast > inputs.ema_slow:
                buy += 2
            elif inputs.ema_fast < inputs.ema_slow:
                sell += 2

        if inputs.rsi is not None:
            if inputs.rsi > 52:
                buy += 1
            elif inputs.rsi < 48:
                sell += 1

        for _agent, direction in inputs.candle_signals:
            if direction is Direction.BUY:
                buy += 1
            elif direction is Direction.SELL:
                sell += 1

        if buy == sell:
            return None
        return Direction.BUY if buy > sell else Direction.SELL


def _field(block: dict[str, object] | None, name: str, default: str) -> str:
    if not block:
        return default
    value = block.get(name)
    return default if value is None else str(value)


def _journey_direction(block: dict[str, object] | None) -> Direction | None:
    if not block:
        return None
    above_asia = (block.get("flags") or {}).get("above_asia_open") if isinstance(block.get("flags"), dict) else None
    above_close = (
        (block.get("flags") or {}).get("above_previous_close")
        if isinstance(block.get("flags"), dict)
        else None
    )
    if above_asia is True and above_close is True:
        return Direction.BUY
    if above_asia is False and above_close is False:
        return Direction.SELL
    return None


def _participation_direction(block: dict[str, object] | None) -> Direction | None:
    if not block:
        return None
    impulse = block.get("price_impulse")
    if impulse == "bullish":
        return Direction.BUY
    if impulse == "bearish":
        return Direction.SELL
    relation = block.get("vwap_relation")
    if relation == "above":
        return Direction.BUY
    if relation == "below":
        return Direction.SELL
    return None
