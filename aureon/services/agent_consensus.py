"""Agent consensus for EMA-cross notifications.

This is an agreement meter, not a probability model. It uses only detections that
were already knowable on the same closed candle as the trigger.
"""
from __future__ import annotations

from dataclasses import dataclass

from aureon.models.detection import Detection


@dataclass(frozen=True)
class AgentVote:
    agent: str
    state: str
    event_key: str


@dataclass(frozen=True)
class AgentConsensus:
    label: str
    supportive: int
    neutral: int
    conflicting: int
    coverage: int
    votes: tuple[AgentVote, ...]

    @property
    def meter(self) -> str:
        blocks = {
            "VERY HIGH": 9,
            "HIGH": 8,
            "MODERATE": 6,
            "LOW": 4,
            "CONFLICTED": 2,
            "INSUFFICIENT": 1,
        }.get(self.label, 1)
        return "█" * blocks + "░" * (10 - blocks)


def build_agent_consensus(
    trigger: Detection,
    same_candle: list[Detection],
) -> AgentConsensus:
    """Classify same-candle agent agreement with the EMA-cross direction."""
    if trigger.direction is None:
        return AgentConsensus("INSUFFICIENT", 0, 0, 0, 0, ())

    votes: list[AgentVote] = []
    seen_agents: set[str] = {trigger.agent_name}

    for detection in same_candle:
        if detection.detection_id == trigger.detection_id:
            continue
        if detection.agent_name in seen_agents:
            continue
        if detection.symbol != trigger.symbol or detection.timeframe != trigger.timeframe:
            continue
        if detection.detected_at.utc != trigger.detected_at.utc:
            continue

        seen_agents.add(detection.agent_name)
        if detection.direction is None:
            state = "neutral"
        elif detection.direction == trigger.direction:
            state = "supportive"
        else:
            state = "conflicting"
        votes.append(
            AgentVote(
                agent=detection.agent_name,
                state=state,
                event_key=detection.event_key,
            )
        )

    supportive = sum(vote.state == "supportive" for vote in votes)
    neutral = sum(vote.state == "neutral" for vote in votes)
    conflicting = sum(vote.state == "conflicting" for vote in votes)
    coverage = len(votes)
    label = _consensus_label(
        supportive=supportive,
        neutral=neutral,
        conflicting=conflicting,
    )
    return AgentConsensus(
        label=label,
        supportive=supportive,
        neutral=neutral,
        conflicting=conflicting,
        coverage=coverage,
        votes=tuple(votes),
    )


def _consensus_label(*, supportive: int, neutral: int, conflicting: int) -> str:
    opinionated = supportive + conflicting
    if opinionated < 2:
        return "INSUFFICIENT"
    if conflicting >= supportive and conflicting > 0:
        return "CONFLICTED"

    support_share = supportive / opinionated
    if supportive >= 4 and conflicting == 0 and support_share >= 0.80:
        return "VERY HIGH"
    if supportive >= 3 and support_share >= 0.67 and conflicting <= 1:
        return "HIGH"
    if supportive > conflicting:
        return "MODERATE"
    return "LOW"


def consensus_lines(consensus: AgentConsensus, *, max_agents: int = 7) -> list[str]:
    """Compact Discord-friendly lines, strongest signal first."""
    icon = {"supportive": "✅", "neutral": "➖", "conflicting": "❌"}
    ordered = sorted(
        consensus.votes,
        key=lambda vote: (
            {"supportive": 0, "conflicting": 1, "neutral": 2}[vote.state],
            vote.agent,
        ),
    )
    return [
        f"{icon[vote.state]} {vote.agent.replace('_', ' ')}"
        for vote in ordered[:max_agents]
    ]
