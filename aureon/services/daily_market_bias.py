"""Daily Market Bias Agent (V1): deterministic, chronological, never predictive.

It answers, at each closed candle, "what kind of trading day is developing, which
direction currently deserves preference, which session is producing the strongest
opportunity, and is the market trending, ranging, reversing or becoming mixed?"

## Rules it lives by

* **Chronological.** The state evolves one closed candle at a time. A candle only ever
  moves the state forward; no future candle influences an earlier classification, and a
  restart replays the same candles into the same state.
* **Consumes, never recomputes.** EMA, RSI, ATR, regime, journey, HTF, session trend,
  wick/liquidity/breakout and participation reads are supplied by the observer's
  existing agents. This agent only tracks the day's and each session's OHLC range, which
  is bookkeeping rather than an indicator.
* **Strength, score, quality.** No value here is a probability. Whether "bullish London
  + BUY" produces more clean_10 outcomes is for the V1 model to learn from history.
* **No best session is hard-coded.** ``best_session_so_far`` is whichever session has
  shown the highest opportunity quality *today*; it is a description of the day, not a
  rule about the market.
* **No broker, no orders.** It lives on the observation side and the boundary tests keep
  it there.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from aureon.models.base import to_utc
from aureon.models.enums import Direction, SessionName
from aureon.models.market_bias import (
    DailyBiasState,
    DailyMarketBiasSnapshot,
    ReversalRisk,
    SessionBiasRead,
    TrendQuality,
    VolatilityState,
)

log = logging.getLogger(__name__)

AGENT_NAME = "daily_market_bias"
AGENT_VERSION = "1.0.0"


@dataclass(frozen=True)
class DailyBiasPolicy:
    """Every threshold that shapes a classification. Stored on each snapshot's provenance."""

    strong_strength: float = 0.60
    directional_strength: float = 0.25
    mixed_agreement_below: float = 0.60
    mixed_min_component_strength: float = 0.35
    reversal_session_strength: float = 0.35
    reversal_daily_strength: float = 0.25
    session_smoothing_bars: int = 12
    daily_smoothing_bars: int = 36
    range_scale_atr: float = 3.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "strong_strength": self.strong_strength,
            "directional_strength": self.directional_strength,
            "mixed_agreement_below": self.mixed_agreement_below,
            "mixed_min_component_strength": self.mixed_min_component_strength,
            "reversal_session_strength": self.reversal_session_strength,
            "reversal_daily_strength": self.reversal_daily_strength,
            "session_smoothing_bars": self.session_smoothing_bars,
            "daily_smoothing_bars": self.daily_smoothing_bars,
            "range_scale_atr": self.range_scale_atr,
        }


@dataclass(frozen=True)
class DailyBiasInputs:
    """One closed candle plus the observer's already-computed context for it.

    Everything optional degrades gracefully: a missing read contributes nothing rather
    than a guessed value, and the snapshot records which components were available.
    """

    symbol: str
    timeframe: str
    at: datetime
    market_date: str
    session: SessionName
    open: float
    high: float
    low: float
    close: float
    ema_fast: float | None = None
    ema_slow: float | None = None
    previous_ema_fast: float | None = None
    rsi: float | None = None
    previous_rsi: float | None = None
    atr: float | None = None
    regime: dict[str, Any] | None = None
    journey: dict[str, Any] | None = None
    htf_trend: str | None = None
    session_live_trend: str | None = None
    wick: dict[str, Any] | None = None
    sweep: dict[str, Any] | None = None
    breakout: dict[str, Any] | None = None
    participation: dict[str, Any] | None = None


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _sign_text(value: Any, positive: set[str], negative: set[str]) -> float:
    text = str(getattr(value, "value", value) or "").lower()
    if text in positive:
        return 1.0
    if text in negative:
        return -1.0
    return 0.0


@dataclass
class _SessionAccumulator:
    session: SessionName
    started_at: datetime
    open: float
    high: float
    low: float
    close: float
    bars: int = 0
    score: float = 0.0
    peak_abs_score: float = 0.0
    made_new_day_high: bool = False
    made_new_day_low: bool = False
    breakout_seen: bool = False
    read: SessionBiasRead | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.value,
            "started_at": self.started_at.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "bars": self.bars,
            "score": self.score,
            "peak_abs_score": self.peak_abs_score,
            "made_new_day_high": self.made_new_day_high,
            "made_new_day_low": self.made_new_day_low,
            "breakout_seen": self.breakout_seen,
            "read": None if self.read is None else self.read.model_dump(mode="json"),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> _SessionAccumulator:
        read = payload.get("read")
        return cls(
            session=SessionName(payload["session"]),
            started_at=to_utc(datetime.fromisoformat(payload["started_at"])),
            open=float(payload["open"]),
            high=float(payload["high"]),
            low=float(payload["low"]),
            close=float(payload["close"]),
            bars=int(payload.get("bars", 0)),
            score=float(payload.get("score", 0.0)),
            peak_abs_score=float(payload.get("peak_abs_score", 0.0)),
            made_new_day_high=bool(payload.get("made_new_day_high")),
            made_new_day_low=bool(payload.get("made_new_day_low")),
            breakout_seen=bool(payload.get("breakout_seen")),
            read=None if read is None else SessionBiasRead.model_validate(read),
        )


@dataclass
class _DayState:
    market_date: str
    day_open: float
    day_high: float
    day_low: float
    daily_score: float = 0.0
    bars: int = 0
    current: _SessionAccumulator | None = None
    completed: dict[str, _SessionAccumulator] = field(default_factory=dict)
    transitions: list[str] = field(default_factory=list)
    last_snapshot: DailyMarketBiasSnapshot | None = None
    previous_day_bias: DailyBiasState | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_date": self.market_date,
            "day_open": self.day_open,
            "day_high": self.day_high,
            "day_low": self.day_low,
            "daily_score": self.daily_score,
            "bars": self.bars,
            "current": None if self.current is None else self.current.to_dict(),
            "completed": {key: value.to_dict() for key, value in self.completed.items()},
            "transitions": list(self.transitions),
            "last_snapshot": (
                None if self.last_snapshot is None else self.last_snapshot.model_dump(mode="json")
            ),
            "previous_day_bias": (
                None if self.previous_day_bias is None else self.previous_day_bias.value
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> _DayState:
        current = payload.get("current")
        snapshot = payload.get("last_snapshot")
        previous = payload.get("previous_day_bias")
        return cls(
            market_date=str(payload["market_date"]),
            day_open=float(payload["day_open"]),
            day_high=float(payload["day_high"]),
            day_low=float(payload["day_low"]),
            daily_score=float(payload.get("daily_score", 0.0)),
            bars=int(payload.get("bars", 0)),
            current=None if current is None else _SessionAccumulator.from_dict(current),
            completed={
                key: _SessionAccumulator.from_dict(value)
                for key, value in (payload.get("completed") or {}).items()
            },
            transitions=[str(one) for one in payload.get("transitions") or []],
            last_snapshot=(
                None if snapshot is None else DailyMarketBiasSnapshot.model_validate(snapshot)
            ),
            previous_day_bias=None if previous is None else DailyBiasState(previous),
        )


class DailyMarketBiasAgent:
    """One chronological bias tracker per (symbol, timeframe) stream."""

    agent_name = AGENT_NAME
    agent_version = AGENT_VERSION

    def __init__(self, policy: DailyBiasPolicy | None = None) -> None:
        self.policy = policy or DailyBiasPolicy()
        self._streams: dict[str, _DayState] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    @staticmethod
    def _key(symbol: str, timeframe: str) -> str:
        return f"{symbol.upper()}|{timeframe}"

    def current(self, symbol: str, timeframe: str) -> DailyMarketBiasSnapshot | None:
        state = self._streams.get(self._key(symbol, timeframe))
        return None if state is None else state.last_snapshot

    def observe(self, inputs: DailyBiasInputs) -> DailyMarketBiasSnapshot:
        """Fold one closed candle into the stream and return the new frozen snapshot."""
        at = to_utc(inputs.at)
        key = self._key(inputs.symbol, inputs.timeframe)
        state = self._streams.get(key)

        if state is not None and state.last_snapshot is not None:
            if at <= state.last_snapshot.timestamp:
                # A replayed or duplicated candle cannot move the clock backwards. Return
                # what was already known rather than rewriting history.
                return state.last_snapshot

        if state is None or state.market_date != inputs.market_date:
            # Day reset. The finished day's final state is kept as previous_day_bias so
            # the first candles of the new day still carry yesterday's character.
            previous_day = (
                state.last_snapshot.daily_bias
                if state is not None and state.last_snapshot is not None
                else None
            )
            state = _DayState(
                market_date=inputs.market_date,
                day_open=inputs.open,
                day_high=inputs.high,
                day_low=inputs.low,
                previous_day_bias=previous_day,
            )
            self._streams[key] = state

        previous_snapshot = state.last_snapshot
        made_new_high = inputs.high > state.day_high
        made_new_low = inputs.low < state.day_low
        state.day_high = max(state.day_high, inputs.high)
        state.day_low = min(state.day_low, inputs.low)
        state.bars += 1

        # Session reset: finalise the previous accumulator, record its transition label.
        current = state.current
        if current is None or current.session is not inputs.session:
            if current is not None:
                current.read = self._finalise_session(current, complete=True)
                state.completed[current.session.value] = current
                state.transitions.append(current.read.label)
            current = _SessionAccumulator(
                session=inputs.session,
                started_at=at,
                open=inputs.open,
                high=inputs.high,
                low=inputs.low,
                close=inputs.close,
            )
            state.current = current
        else:
            current.high = max(current.high, inputs.high)
            current.low = min(current.low, inputs.low)
            current.close = inputs.close
        current.bars += 1
        current.made_new_day_high = current.made_new_day_high or made_new_high
        current.made_new_day_low = current.made_new_day_low or made_new_low

        components, available = self._components(
            inputs, state, made_new_high=made_new_high, made_new_low=made_new_low
        )
        if inputs.breakout and inputs.breakout.get("direction"):
            current.breakout_seen = True

        weights = {
            "ema_relation": 1.0,
            "ema_slope": 0.75,
            "price_vs_day_open": 1.0,
            "price_vs_previous_close": 0.5,
            "rsi": 0.5,
            "session_trend": 0.75,
            "htf": 1.0,
            "structure": 0.75,
            "participation": 0.5,
            "breakout": 0.5,
        }
        total_weight = sum(weights[name] for name in available) or 1.0
        raw_score = sum(weights[name] * components[name] for name in available) / total_weight
        raw_score = _clip(raw_score)

        # Smooth chronologically: an EMA over the candles seen so far. The first candle of
        # a session (or day) seeds the average rather than being damped toward zero.
        session_alpha = 2.0 / (self.policy.session_smoothing_bars + 1)
        daily_alpha = 2.0 / (self.policy.daily_smoothing_bars + 1)
        current.score = (
            raw_score
            if current.bars == 1
            else current.score + session_alpha * (raw_score - current.score)
        )
        state.daily_score = (
            raw_score
            if state.bars == 1
            else state.daily_score + daily_alpha * (raw_score - state.daily_score)
        )
        current.peak_abs_score = max(current.peak_abs_score, abs(current.score))

        agreement = self._agreement(components, available, raw_score)
        mean_component_strength = (
            sum(abs(components[name]) for name in available) / len(available) if available else 0.0
        )

        regime = inputs.regime or {}
        volatility_state = self._volatility_state(regime)
        session_read = self._session_read(
            current,
            inputs,
            agreement=agreement,
            mean_component_strength=mean_component_strength,
            volatility_state=volatility_state,
            previous_session=self._previous_session(state),
            at=at,
            complete=False,
        )
        current.read = session_read

        daily_bias = self._classify(
            state.daily_score,
            agreement=agreement,
            mean_component_strength=mean_component_strength,
            opposing_score=current.score,
            opposing_strength=abs(current.score),
        )
        daily_strength = _clip(abs(state.daily_score), 0.0, 1.0)
        preferred = self._preferred_direction(daily_bias, state.daily_score, current.score)
        trend_quality = self._trend_quality(regime, daily_strength)
        reversal_risk = session_read.reversal_risk
        opportunity = self._opportunity(
            daily_strength, trend_quality, volatility_state, reversal_risk
        )

        completed_reads = {
            name: acc.read for name, acc in state.completed.items() if acc.read is not None
        }
        all_reads = dict(completed_reads)
        all_reads[current.session.value] = session_read
        best = max(
            all_reads.values(),
            key=lambda read: (read.opportunity_quality, read.bars),
            default=None,
        )

        previous_session = self._previous_session(state)
        previous_read = (
            state.completed[previous_session.value].read
            if previous_session is not None and previous_session.value in state.completed
            else None
        )

        bias_changed = (
            previous_snapshot is not None and previous_snapshot.daily_bias is not daily_bias
        )
        change_reason = None
        if bias_changed and previous_snapshot is not None:
            change_reason = self._change_reason(
                previous_snapshot, components, available, daily_bias
            )

        chain = list(state.transitions) + [session_read.label]
        snapshot = DailyMarketBiasSnapshot(
            symbol=inputs.symbol.upper(),
            timeframe=inputs.timeframe,
            market_date=inputs.market_date,
            timestamp=at,
            daily_bias=daily_bias,
            daily_bias_strength=daily_strength,
            daily_score=_clip(state.daily_score),
            preferred_direction=preferred,
            current_session=inputs.session,
            session_bias=session_read.bias,
            session_bias_strength=session_read.bias_strength,
            session_score=session_read.score,
            trend_quality=trend_quality,
            volatility_state=volatility_state,
            opportunity_quality=opportunity,
            reversal_risk=reversal_risk,
            asia_bias=self._slot(all_reads, SessionName.ASIA)[0],
            asia_strength=self._slot(all_reads, SessionName.ASIA)[1],
            london_bias=self._slot(all_reads, SessionName.LONDON)[0],
            london_strength=self._slot(all_reads, SessionName.LONDON)[1],
            new_york_bias=self._slot(all_reads, SessionName.NEW_YORK)[0],
            new_york_strength=self._slot(all_reads, SessionName.NEW_YORK)[1],
            best_session_so_far=None if best is None else best.session,
            best_direction_so_far=None if best is None else best.preferred_direction,
            previous_session=previous_session,
            previous_session_bias=None if previous_read is None else previous_read.bias,
            previous_day_bias=state.previous_day_bias,
            bias_changed=bias_changed,
            bias_change_reason=change_reason,
            session_transition_state="->".join(chain),
            transitions=tuple(state.transitions),
            sessions=all_reads,
            day_open=state.day_open,
            day_high=state.day_high,
            day_low=state.day_low,
            components={name: round(components[name], 4) for name in available},
            agent_version=self.agent_version,
        )
        state.last_snapshot = snapshot
        return snapshot

    # ── Persistence (restart safety) ──────────────────────────────────────────

    def state_dict(self) -> dict[str, Any]:
        return {
            "agent_version": self.agent_version,
            "policy": self.policy.as_dict(),
            "streams": {key: value.to_dict() for key, value in self._streams.items()},
        }

    def restore(self, payload: dict[str, Any] | None) -> int:
        """Load a previously saved state. Returns the number of streams restored."""
        if not payload or payload.get("agent_version") != self.agent_version:
            return 0
        restored = 0
        for key, value in (payload.get("streams") or {}).items():
            try:
                self._streams[key] = _DayState.from_dict(value)
                restored += 1
            except Exception:  # noqa: BLE001 - a corrupt stream restarts from the next candle
                log.exception("daily bias stream %s could not be restored", key)
        return restored

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _components(
        self,
        inputs: DailyBiasInputs,
        state: _DayState,
        *,
        made_new_high: bool,
        made_new_low: bool,
    ) -> tuple[dict[str, float], list[str]]:
        components: dict[str, float] = {}
        available: list[str] = []
        scale = self._scale(inputs, state)

        if inputs.ema_fast is not None and inputs.ema_slow is not None:
            gap = inputs.ema_fast - inputs.ema_slow
            components["ema_relation"] = (
                _clip(gap / scale) if scale > 0 else (1.0 if gap > 0 else -1.0 if gap < 0 else 0.0)
            )
            available.append("ema_relation")
        if inputs.ema_fast is not None and inputs.previous_ema_fast is not None and scale > 0:
            slope = inputs.ema_fast - inputs.previous_ema_fast
            components["ema_slope"] = _clip(slope / (scale * 0.25))
            available.append("ema_slope")
        if scale > 0:
            components["price_vs_day_open"] = _clip((inputs.close - state.day_open) / scale)
            available.append("price_vs_day_open")
        journey = inputs.journey or {}
        levels = journey.get("levels") if isinstance(journey.get("levels"), dict) else journey
        previous_close = levels.get("previous_day_close") if isinstance(levels, dict) else None
        if previous_close is not None and scale > 0:
            components["price_vs_previous_close"] = _clip(
                (inputs.close - float(previous_close)) / scale
            )
            available.append("price_vs_previous_close")
        if inputs.rsi is not None:
            components["rsi"] = _clip((float(inputs.rsi) - 50.0) / 25.0)
            available.append("rsi")
        if inputs.session_live_trend in {"up", "down", "flat"}:
            components["session_trend"] = {"up": 1.0, "down": -1.0, "flat": 0.0}[
                inputs.session_live_trend
            ]
            available.append("session_trend")
        if inputs.htf_trend:
            value = _sign_text(inputs.htf_trend, {"bullish", "up"}, {"bearish", "down"})
            if value != 0.0 or str(inputs.htf_trend).lower() in {"sideways", "neutral", "mixed"}:
                components["htf"] = value
                available.append("htf")

        structure = 0.0
        day_range = state.day_high - state.day_low
        if day_range > 0:
            location = (inputs.close - state.day_low) / day_range
            structure += location - 0.5  # -0.5 .. +0.5
        if made_new_high:
            structure += 0.5
        if made_new_low:
            structure -= 0.5
        components["structure"] = _clip(structure)
        available.append("structure")

        participation = inputs.participation or {}
        impulse = participation.get("price_impulse") if isinstance(participation, dict) else None
        if impulse in {"bullish", "bearish"}:
            components["participation"] = 1.0 if impulse == "bullish" else -1.0
            available.append("participation")
        breakout = inputs.breakout or {}
        breakout_direction = breakout.get("direction") if isinstance(breakout, dict) else None
        if breakout_direction:
            components["breakout"] = _sign_text(
                breakout_direction, {"buy", "bullish", "up"}, {"sell", "bearish", "down"}
            )
            available.append("breakout")
        return components, available

    def _scale(self, inputs: DailyBiasInputs, state: _DayState) -> float:
        if inputs.atr is not None and inputs.atr > 0:
            return float(inputs.atr) * self.policy.range_scale_atr
        day_range = state.day_high - state.day_low
        if day_range > 0:
            return day_range
        bar_range = inputs.high - inputs.low
        return bar_range * self.policy.range_scale_atr if bar_range > 0 else 0.0

    @staticmethod
    def _agreement(components: dict[str, float], available: list[str], score: float) -> float:
        voting = [components[name] for name in available if abs(components[name]) >= 0.15]
        if not voting or score == 0.0:
            return 1.0
        sign = 1.0 if score > 0 else -1.0
        agreeing = sum(1 for value in voting if (value > 0) == (sign > 0))
        return agreeing / len(voting)

    def _classify(
        self,
        score: float,
        *,
        agreement: float,
        mean_component_strength: float,
        opposing_score: float | None = None,
        opposing_strength: float = 0.0,
        allow_reversal: bool = True,
    ) -> DailyBiasState:
        strength = abs(score)
        policy = self.policy
        if (
            agreement < policy.mixed_agreement_below
            and strength < policy.strong_strength
            and mean_component_strength >= policy.mixed_min_component_strength
        ):
            return DailyBiasState.MIXED
        if (
            allow_reversal
            and opposing_score is not None
            and strength >= policy.reversal_daily_strength
            and opposing_strength >= policy.reversal_session_strength
            and (opposing_score > 0) != (score > 0)
        ):
            return DailyBiasState.REVERSAL
        if strength >= policy.strong_strength:
            return DailyBiasState.STRONG_BULLISH if score > 0 else DailyBiasState.STRONG_BEARISH
        if strength >= policy.directional_strength:
            return DailyBiasState.BULLISH if score > 0 else DailyBiasState.BEARISH
        return DailyBiasState.NEUTRAL

    @staticmethod
    def _preferred_direction(
        bias: DailyBiasState, daily_score: float, session_score: float
    ) -> Direction | None:
        if bias is DailyBiasState.REVERSAL:
            return Direction.BUY if session_score > 0 else Direction.SELL
        if bias.sign > 0:
            return Direction.BUY
        if bias.sign < 0:
            return Direction.SELL
        return None

    @staticmethod
    def _volatility_state(regime: dict[str, Any]) -> VolatilityState:
        text = str(regime.get("volatility_state") or "").lower()
        flags = regime.get("flags") if isinstance(regime.get("flags"), dict) else {}
        if text == "compressed" or flags.get("is_compressed"):
            return VolatilityState.COMPRESSING
        if text == "expanded" or flags.get("is_expanding"):
            return VolatilityState.EXPANDING
        if text == "normal":
            return VolatilityState.NORMAL
        return VolatilityState.UNKNOWN

    @staticmethod
    def _trend_quality(regime: dict[str, Any], strength: float) -> TrendQuality:
        regime_name = str(regime.get("regime") or regime.get("state") or "").lower()
        structure = str(regime.get("structure_state") or "").lower()
        if strength < 0.15:
            return TrendQuality.NONE
        if regime_name in {"trending", "trend_expansion"} or structure == "trend":
            return TrendQuality.STRONG if strength >= 0.5 else TrendQuality.MODERATE
        if regime_name in {"volatile_chop", "structurally_messy"} or structure == "chop":
            return TrendQuality.WEAK
        if strength >= 0.6:
            return TrendQuality.MODERATE
        return TrendQuality.WEAK

    @staticmethod
    def _opportunity(
        strength: float,
        trend_quality: TrendQuality,
        volatility_state: VolatilityState,
        reversal_risk: ReversalRisk,
    ) -> float:
        value = strength * 0.5
        value += {
            TrendQuality.STRONG: 0.30,
            TrendQuality.MODERATE: 0.15,
            TrendQuality.WEAK: 0.0,
            TrendQuality.NONE: 0.0,
        }[trend_quality]
        value += {
            VolatilityState.EXPANDING: 0.20,
            VolatilityState.NORMAL: 0.10,
            VolatilityState.COMPRESSING: 0.0,
            VolatilityState.UNKNOWN: 0.05,
        }[volatility_state]
        value -= {ReversalRisk.LOW: 0.0, ReversalRisk.MEDIUM: 0.1, ReversalRisk.HIGH: 0.25}[
            reversal_risk
        ]
        return round(_clip(value, 0.0, 1.0), 4)

    def _reversal_risk(
        self, inputs: DailyBiasInputs, score: float, previous_session: SessionBiasRead | None
    ) -> ReversalRisk:
        risk = 0
        if inputs.rsi is not None:
            if (score > 0 and inputs.rsi >= 75) or (score < 0 and inputs.rsi <= 25):
                risk += 2
            elif (score > 0 and inputs.rsi >= 65) or (score < 0 and inputs.rsi <= 35):
                risk += 1
        sweep = inputs.sweep or {}
        level = str(sweep.get("level_type") or "").lower() if isinstance(sweep, dict) else ""
        if level:
            if score > 0 and "high" in level:
                risk += 1
            if score < 0 and "low" in level:
                risk += 1
        wick = inputs.wick or {}
        wick_direction = str(wick.get("direction") or "").lower() if isinstance(wick, dict) else ""
        if wick_direction and abs(score) >= self.policy.directional_strength:
            if (score > 0 and wick_direction in {"sell", "bearish", "down"}) or (
                score < 0 and wick_direction in {"buy", "bullish", "up"}
            ):
                risk += 1
        if previous_session is not None and previous_session.bias.is_directional:
            if abs(score) >= self.policy.reversal_session_strength and (
                (score > 0) != (previous_session.bias.sign > 0)
            ):
                risk += 1
        if risk >= 2:
            return ReversalRisk.HIGH
        if risk == 1:
            return ReversalRisk.MEDIUM
        return ReversalRisk.LOW

    def _session_read(
        self,
        acc: _SessionAccumulator,
        inputs: DailyBiasInputs,
        *,
        agreement: float,
        mean_component_strength: float,
        volatility_state: VolatilityState,
        previous_session: SessionName | None,
        at: datetime,
        complete: bool,
    ) -> SessionBiasRead:
        previous_read = None
        state = self._streams.get(self._key(inputs.symbol, inputs.timeframe))
        if state is not None and previous_session is not None:
            previous_acc = state.completed.get(previous_session.value)
            previous_read = previous_acc.read if previous_acc is not None else None
        opposing = None if previous_read is None else previous_read.score
        bias = self._classify(
            acc.score,
            agreement=agreement,
            mean_component_strength=mean_component_strength,
            opposing_score=opposing,
            opposing_strength=abs(opposing) if opposing is not None else 0.0,
            allow_reversal=previous_read is not None and previous_read.bias.is_directional,
        )
        # A session that opposes the prior session is a reversal only when the prior
        # session was itself directional; otherwise it is simply the day's first direction.
        if bias is DailyBiasState.REVERSAL and (
            previous_read is None or not previous_read.bias.is_directional
        ):
            bias = self._classify(
                acc.score,
                agreement=agreement,
                mean_component_strength=mean_component_strength,
                allow_reversal=False,
            )
        strength = _clip(abs(acc.score), 0.0, 1.0)
        preferred = self._preferred_direction(bias, acc.score, acc.score)
        trend_quality = self._trend_quality(inputs.regime or {}, strength)
        reversal_risk = self._reversal_risk(inputs, acc.score, previous_read)
        opportunity = self._opportunity(strength, trend_quality, volatility_state, reversal_risk)
        label = self._label(acc, bias, previous_read, inputs.regime or {})
        return SessionBiasRead(
            session=acc.session,
            bias=bias,
            bias_strength=strength,
            score=_clip(acc.score),
            preferred_direction=preferred,
            trend_quality=trend_quality,
            volatility_state=volatility_state,
            opportunity_quality=opportunity,
            reversal_risk=reversal_risk,
            label=label,
            bars=acc.bars,
            session_open=acc.open,
            session_high=acc.high,
            session_low=acc.low,
            session_close=acc.close,
            complete=complete,
            started_at=acc.started_at,
            updated_at=at,
        )

    def _finalise_session(self, acc: _SessionAccumulator, *, complete: bool) -> SessionBiasRead:
        read = acc.read
        if read is None:
            return SessionBiasRead(
                session=acc.session, label=f"{acc.session.value.upper()}_UNKNOWN"
            )
        return read.model_copy(update={"complete": complete})

    @staticmethod
    def _label(
        acc: _SessionAccumulator,
        bias: DailyBiasState,
        previous: SessionBiasRead | None,
        regime: dict[str, Any],
    ) -> str:
        session = acc.session.value.upper()
        if bias is DailyBiasState.REVERSAL:
            return f"{session}_REVERSAL"
        if bias is DailyBiasState.MIXED:
            return f"{session}_MIXED"
        if not bias.is_directional:
            regime_name = str(regime.get("regime") or regime.get("state") or "").lower()
            if (
                regime_name in {"range_compression"}
                or str(regime.get("volatility_state")) == "compressed"
            ):
                return f"{session}_COMPRESSION"
            return f"{session}_RANGE"
        side = "BULLISH" if bias.sign > 0 else "BEARISH"
        if (
            previous is not None
            and previous.bias.is_directional
            and previous.bias.sign == bias.sign
        ):
            kind = "EXTENSION" if acc.peak_abs_score > previous.bias_strength else "CONTINUATION"
        elif (
            acc.breakout_seen
            or acc.made_new_day_high
            and bias.sign > 0
            or acc.made_new_day_low
            and bias.sign < 0
        ):
            kind = "BREAKOUT"
        else:
            kind = "TREND"
        return f"{session}_{side}_{kind}"

    @staticmethod
    def _slot(
        reads: dict[str, SessionBiasRead], session: SessionName
    ) -> tuple[DailyBiasState, float]:
        read = reads.get(session.value)
        if read is None:
            return DailyBiasState.NEUTRAL, 0.0
        return read.bias, read.bias_strength

    @staticmethod
    def _previous_session(state: _DayState) -> SessionName | None:
        if not state.completed:
            return None
        latest = max(state.completed.values(), key=lambda acc: acc.started_at)
        return latest.session

    @staticmethod
    def _change_reason(
        previous: DailyMarketBiasSnapshot,
        components: dict[str, float],
        available: list[str],
        new_bias: DailyBiasState,
    ) -> str:
        deltas = {name: components[name] - previous.components.get(name, 0.0) for name in available}
        if not deltas:
            return f"{previous.daily_bias.value} -> {new_bias.value}"
        driver, delta = max(deltas.items(), key=lambda item: abs(item[1]))
        direction = "rose" if delta > 0 else "fell"
        return (
            f"{previous.daily_bias.value} -> {new_bias.value}: "
            f"{driver} {direction} {abs(delta):.2f}"
        )


class DailyBiasStateStore:
    """Atomic JSON persistence of the agent's streams, for restart recovery.

    Same shape as ``ObserverState``: written to a temporary file and renamed over the old
    one, so a crash mid-write leaves the previous state rather than half a file.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("daily bias state at %s is unreadable; starting fresh", self.path)
            return None

    def save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", dir=str(self.path.parent), delete=False, encoding="utf-8", suffix=".tmp"
        )
        try:
            handle.write(json.dumps(payload, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(handle.name, self.path)
        except BaseException:
            handle.close()
            Path(handle.name).unlink(missing_ok=True)
            raise
