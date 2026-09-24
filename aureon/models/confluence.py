"""Frozen six-agent confluence carried by a setup.

This is deliberately an ALIGNMENT metric, not a probability of success. Each of the six
specialist readings is reduced to bullish/bearish/neutral context and compared with the setup's
own direction_context. Discord renders this stored snapshot; it never recomputes it.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from aureon.models.base import AureonModel
from aureon.models.enums import DirectionContext

AGENT_CONFLUENCE_ROSTER: tuple[str, ...] = (
    "ema_cross",
    "rsi",
    "session_trend",
    "wick",
    "liquidity",
    "breakout",
)

ALIGN_ALIGNED = "aligned"
ALIGN_OPPOSED = "opposed"
ALIGN_NEUTRAL = "neutral"
ALIGNMENTS = {ALIGN_ALIGNED, ALIGN_OPPOSED, ALIGN_NEUTRAL}


class AgentConfluenceVote(AureonModel):
    """One specialist reading as it existed when the setup was last updated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_name: str
    stance: DirectionContext = DirectionContext.NEUTRAL
    alignment: str = Field(default=ALIGN_NEUTRAL)
    observation: str = "no current evidence"

    @model_validator(mode="after")
    def _known_alignment(self) -> "AgentConfluenceVote":
        if self.alignment not in ALIGNMENTS:
            raise ValueError(f"unknown agent alignment {self.alignment!r}")
        return self


class AgentConfluence(AureonModel):
    """Six-agent alignment snapshot.

    confidence_pct means aligned agents / 6. It is intentionally NOT a win rate,
    forecast probability, or execution trigger. Opposed and neutral counts are stored beside it
    so a 67% reading is inspectable rather than a magic number.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_version: str = "1.0.0"
    target: DirectionContext = DirectionContext.NEUTRAL
    confidence_pct: int = Field(default=0, ge=0, le=100)
    aligned_count: int = Field(default=0, ge=0, le=6)
    opposed_count: int = Field(default=0, ge=0, le=6)
    neutral_count: int = Field(default=0, ge=0, le=6)
    votes: tuple[AgentConfluenceVote, ...] = ()

    @model_validator(mode="after")
    def _counts_match_votes(self) -> "AgentConfluence":
        # Empty is the backwards-compatible value for setups written before this feature.
        if not self.votes:
            if any(
                (self.confidence_pct, self.aligned_count, self.opposed_count, self.neutral_count)
            ):
                raise ValueError("an empty confluence snapshot must have zero counts")
            return self

        names = tuple(vote.agent_name for vote in self.votes)
        if names != AGENT_CONFLUENCE_ROSTER:
            raise ValueError(
                "agent confluence votes must use the fixed six-agent roster in canonical order"
            )

        aligned = sum(v.alignment == ALIGN_ALIGNED for v in self.votes)
        opposed = sum(v.alignment == ALIGN_OPPOSED for v in self.votes)
        neutral = sum(v.alignment == ALIGN_NEUTRAL for v in self.votes)
        if (aligned, opposed, neutral) != (
            self.aligned_count,
            self.opposed_count,
            self.neutral_count,
        ):
            raise ValueError("agent confluence counts do not match the stored votes")

        expected = round(aligned / len(AGENT_CONFLUENCE_ROSTER) * 100)
        if self.confidence_pct != expected:
            raise ValueError(
                f"agent confluence confidence must be aligned/6 ({expected}%), "
                f"got {self.confidence_pct}%"
            )
        return self
