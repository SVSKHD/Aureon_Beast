"""Build the six-agent confluence snapshot from observer-owned facts.

The score is intentionally simple and inspectable: one point for each specialist reading aligned
with the setup's direction, divided by the fixed six-agent roster. It is an alignment meter, not
a probability model. Discord only renders the frozen result.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aureon.models.confluence import (
    AGENT_CONFLUENCE_ROSTER,
    ALIGN_ALIGNED,
    ALIGN_NEUTRAL,
    ALIGN_OPPOSED,
    AgentConfluence,
    AgentConfluenceVote,
)
from aureon.models.enums import DirectionContext, TrendBias

RSI_MIDPOINT = 50.0


def build_agent_confluence(
    inputs: Any,
    target: DirectionContext,
    *,
    previous: AgentConfluence | None = None,
) -> AgentConfluence:
    """Freeze the six specialist reads relative to one setup direction.

    EMA, RSI and session trend are refreshed from the observer's current indicator/state reads.
    Wick, liquidity and breakout are event-driven agents, so their latest non-neutral reading is
    retained until that agent emits again. This keeps a sweep that started a setup visible on the
    later confirmation card instead of pretending the sweep never happened.
    """
    old = {
        vote.agent_name: vote
        for vote in ((previous.votes if previous is not None else ()) or ())
    }

    votes = (
        _ema_vote(inputs, target),
        _rsi_vote(inputs, target),
        _trend_vote(inputs, target),
        _event_vote("wick", inputs, target, old.get("wick"), _wick_stance),
        _event_vote(
            "liquidity", inputs, target, old.get("liquidity"), _liquidity_stance
        ),
        _event_vote(
            "breakout", inputs, target, old.get("breakout"), _breakout_stance
        ),
    )
    assert tuple(vote.agent_name for vote in votes) == AGENT_CONFLUENCE_ROSTER

    aligned = sum(vote.alignment == ALIGN_ALIGNED for vote in votes)
    opposed = sum(vote.alignment == ALIGN_OPPOSED for vote in votes)
    neutral = sum(vote.alignment == ALIGN_NEUTRAL for vote in votes)
    return AgentConfluence(
        target=target,
        confidence_pct=round(aligned / len(AGENT_CONFLUENCE_ROSTER) * 100),
        aligned_count=aligned,
        opposed_count=opposed,
        neutral_count=neutral,
        votes=votes,
    )


def _vote(
    agent: str,
    stance: DirectionContext,
    target: DirectionContext,
    observation: str,
) -> AgentConfluenceVote:
    return AgentConfluenceVote(
        agent_name=agent,
        stance=stance,
        alignment=_alignment(stance, target),
        observation=observation,
    )


def _alignment(stance: DirectionContext, target: DirectionContext) -> str:
    if target is DirectionContext.NEUTRAL or stance is DirectionContext.NEUTRAL:
        return ALIGN_NEUTRAL
    return ALIGN_ALIGNED if stance is target else ALIGN_OPPOSED


def _ema_vote(inputs: Any, target: DirectionContext) -> AgentConfluenceVote:
    fast = getattr(inputs, "ema_fast", None)
    slow = getattr(inputs, "ema_slow", None)
    if fast is None or slow is None:
        # A fresh cross detection can still exist in a synthetic/replay test whose SetupInputs
        # omitted the continuous indicator read.
        detected = _detections(inputs, "ema_cross")
        stance = _single_stance(detected, _ema_detection_stance)
        observation = _events(detected) if detected else "EMA read unavailable"
        return _vote("ema_cross", stance, target, observation)

    if fast > slow:
        stance = DirectionContext.BULLISH
        relation = "fast above slow"
    elif fast < slow:
        stance = DirectionContext.BEARISH
        relation = "fast below slow"
    else:
        stance = DirectionContext.NEUTRAL
        relation = "fast equals slow"
    detected = _detections(inputs, "ema_cross")
    prefix = f"{_events(detected)} · " if detected else ""
    return _vote(
        "ema_cross",
        stance,
        target,
        f"{prefix}{relation} ({fast:.2f}/{slow:.2f})",
    )


def _rsi_vote(inputs: Any, target: DirectionContext) -> AgentConfluenceVote:
    value = getattr(inputs, "rsi", None)
    if value is None:
        return _vote("rsi", DirectionContext.NEUTRAL, target, "RSI unavailable")
    value = float(value)
    if value > RSI_MIDPOINT:
        stance = DirectionContext.BULLISH
        label = "above 50 midpoint"
    elif value < RSI_MIDPOINT:
        stance = DirectionContext.BEARISH
        label = "below 50 midpoint"
    else:
        stance = DirectionContext.NEUTRAL
        label = "at 50 midpoint"
    return _vote("rsi", stance, target, f"{value:.1f} · {label}")


def _trend_vote(inputs: Any, target: DirectionContext) -> AgentConfluenceVote:
    trend = getattr(inputs, "trend", TrendBias.SIDEWAYS)
    if trend is TrendBias.BULLISH:
        stance = DirectionContext.BULLISH
    elif trend is TrendBias.BEARISH:
        stance = DirectionContext.BEARISH
    else:
        stance = DirectionContext.NEUTRAL
    label = getattr(trend, "value", str(trend))
    return _vote("session_trend", stance, target, f"session trend {label}")


def _event_vote(
    agent: str,
    inputs: Any,
    target: DirectionContext,
    previous: AgentConfluenceVote | None,
    mapper: Callable[[Any], DirectionContext],
) -> AgentConfluenceVote:
    found = _detections(inputs, agent)
    if found:
        stance = _single_stance(found, mapper)
        return _vote(agent, stance, target, _events(found))
    if previous is not None:
        # Recompute alignment against the target rather than copying it. The target is stable for
        # a setup today, but this keeps the helper correct if the model is reused elsewhere.
        return _vote(agent, previous.stance, target, previous.observation)
    return _vote(agent, DirectionContext.NEUTRAL, target, "no event yet")


def _detections(inputs: Any, agent: str) -> list[Any]:
    return [
        detection
        for detection in (getattr(inputs, "detections", ()) or ())
        if getattr(detection, "agent_name", None) == agent
    ]


def _events(detections: list[Any]) -> str:
    keys = [str(getattr(detection, "event_key", "?")) for detection in detections]
    return ", ".join(keys[:3]) + (f" +{len(keys) - 3} more" if len(keys) > 3 else "")


def _single_stance(
    detections: list[Any],
    mapper: Callable[[Any], DirectionContext],
) -> DirectionContext:
    stances = {mapper(detection) for detection in detections}
    stances.discard(DirectionContext.NEUTRAL)
    return next(iter(stances)) if len(stances) == 1 else DirectionContext.NEUTRAL


def _direction_field(detection: Any) -> DirectionContext | None:
    direction = getattr(detection, "direction", None)
    value = getattr(direction, "value", direction)
    if value == "buy":
        return DirectionContext.BULLISH
    if value == "sell":
        return DirectionContext.BEARISH
    return None


def _ema_detection_stance(detection: Any) -> DirectionContext:
    key = str(getattr(detection, "event_key", "")).lower()
    if "bull" in key:
        return DirectionContext.BULLISH
    if "bear" in key:
        return DirectionContext.BEARISH
    return _direction_field(detection) or DirectionContext.NEUTRAL


def _wick_stance(detection: Any) -> DirectionContext:
    key = str(getattr(detection, "event_key", "")).lower()
    if "lower_rejection" in key:
        return DirectionContext.BULLISH
    if "upper_rejection" in key:
        return DirectionContext.BEARISH
    return DirectionContext.NEUTRAL


def _liquidity_stance(detection: Any) -> DirectionContext:
    explicit = _direction_field(detection)
    if explicit is not None:
        return explicit
    # Liquidity event_key names the SWEEP direction; the implied reversal is the opposite.
    key = str(getattr(detection, "event_key", "")).lower()
    if key.startswith("up|"):
        return DirectionContext.BEARISH
    if key.startswith("down|"):
        return DirectionContext.BULLISH
    return DirectionContext.NEUTRAL


def _breakout_stance(detection: Any) -> DirectionContext:
    explicit = _direction_field(detection)
    if explicit is not None:
        return explicit
    key = str(getattr(detection, "event_key", "")).lower()
    if key.startswith("up|"):
        return DirectionContext.BULLISH
    if key.startswith("down|"):
        return DirectionContext.BEARISH
    return DirectionContext.NEUTRAL
