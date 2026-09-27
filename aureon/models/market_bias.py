"""Daily market bias contracts (V1).

A deterministic description of what kind of trading day is developing, which direction
currently deserves preference and which session is producing the strongest opportunity.
Every value is a **strength**, **score** or **quality** -- never a probability. Nothing
here is statistically calibrated; the V1 entry model learns from these labels whether
any of them actually predicts a clean move.

Every snapshot represents information available AT THAT TIME. The agent that produces
it only ever sees closed candles in chronological order and never looks ahead.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, Field

from aureon.models.base import AureonModel, UtcDatetime
from aureon.models.enums import Direction, SessionName


class DailyBiasState(StrEnum):
    """What the day (or a session) currently looks like."""

    STRONG_BULLISH = "strong_bullish"
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"
    STRONG_BEARISH = "strong_bearish"
    MIXED = "mixed"
    REVERSAL = "reversal"

    @property
    def sign(self) -> int:
        if self in {DailyBiasState.STRONG_BULLISH, DailyBiasState.BULLISH}:
            return 1
        if self in {DailyBiasState.STRONG_BEARISH, DailyBiasState.BEARISH}:
            return -1
        return 0

    @property
    def is_directional(self) -> bool:
        return self.sign != 0


class TrendQuality(StrEnum):
    NONE = "none"
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


class VolatilityState(StrEnum):
    COMPRESSING = "compressing"
    NORMAL = "normal"
    EXPANDING = "expanding"
    UNKNOWN = "unknown"


class ReversalRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SessionBiasRead(AureonModel):
    """One session's contextual read, as of the last closed candle inside it."""

    model_config = ConfigDict(frozen=True)

    session: SessionName
    bias: DailyBiasState = DailyBiasState.NEUTRAL
    bias_strength: float = Field(default=0.0, ge=0, le=1)
    score: float = Field(default=0.0, ge=-1, le=1, description="Signed directional score.")
    preferred_direction: Direction | None = None
    trend_quality: TrendQuality = TrendQuality.NONE
    volatility_state: VolatilityState = VolatilityState.UNKNOWN
    opportunity_quality: float = Field(default=0.0, ge=0, le=1)
    reversal_risk: ReversalRisk = ReversalRisk.LOW
    label: str = Field(
        default="",
        description="Transition label such as LONDON_BULLISH_BREAKOUT; the memory V2 will read.",
    )
    bars: int = Field(default=0, ge=0)
    session_open: float | None = None
    session_high: float | None = None
    session_low: float | None = None
    session_close: float | None = None
    complete: bool = False
    started_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None


class DailyMarketBiasSnapshot(AureonModel):
    """The frozen daily/session context at one instant.

    Frozen on purpose: a setup snapshot embeds one of these and must never be edited by a
    later candle. ``timestamp`` is the close of the candle that produced it.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: str
    market_date: str
    timestamp: UtcDatetime

    daily_bias: DailyBiasState = DailyBiasState.NEUTRAL
    daily_bias_strength: float = Field(default=0.0, ge=0, le=1)
    daily_score: float = Field(default=0.0, ge=-1, le=1)
    preferred_direction: Direction | None = None

    current_session: SessionName = SessionName.OFF
    session_bias: DailyBiasState = DailyBiasState.NEUTRAL
    session_bias_strength: float = Field(default=0.0, ge=0, le=1)
    session_score: float = Field(default=0.0, ge=-1, le=1)

    trend_quality: TrendQuality = TrendQuality.NONE
    volatility_state: VolatilityState = VolatilityState.UNKNOWN
    opportunity_quality: float = Field(default=0.0, ge=0, le=1)
    reversal_risk: ReversalRisk = ReversalRisk.LOW

    asia_bias: DailyBiasState = DailyBiasState.NEUTRAL
    asia_strength: float = Field(default=0.0, ge=0, le=1)
    london_bias: DailyBiasState = DailyBiasState.NEUTRAL
    london_strength: float = Field(default=0.0, ge=0, le=1)
    new_york_bias: DailyBiasState = DailyBiasState.NEUTRAL
    new_york_strength: float = Field(default=0.0, ge=0, le=1)

    best_session_so_far: SessionName | None = None
    best_direction_so_far: Direction | None = None

    previous_session: SessionName | None = None
    previous_session_bias: DailyBiasState | None = None
    previous_day_bias: DailyBiasState | None = None

    bias_changed: bool = False
    bias_change_reason: str | None = None

    session_transition_state: str = Field(
        default="",
        description=(
            "Chronological chain of session labels, e.g. ASIA_RANGE->LONDON_BULLISH_BREAKOUT."
        ),
    )
    transitions: tuple[str, ...] = Field(
        default=(),
        description="Completed-session labels so far today, oldest first.",
    )
    sessions: dict[str, SessionBiasRead] = Field(default_factory=dict)

    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    components: dict[str, float] = Field(
        default_factory=dict,
        description="Signed per-input contributions behind the score, for audit.",
    )
    agent_version: str = "1.0.0"

    def aligned(self, direction: Direction) -> bool | None:
        """Whether a trade direction agrees with the daily bias; None when the day is undirected."""
        sign = self.daily_bias.sign
        if self.daily_bias is DailyBiasState.REVERSAL and self.preferred_direction is not None:
            return self.preferred_direction is direction
        if sign == 0:
            return None
        return sign == direction.sign

    def session_aligned(self, direction: Direction) -> bool | None:
        sign = self.session_bias.sign
        if sign == 0:
            return None
        return sign == direction.sign

    def as_feature_context(self) -> dict[str, object]:
        """The flat keys the V1 feature builder freezes at setup time."""
        return {
            "daily_bias_at_entry": self.daily_bias.value,
            "daily_bias_strength_at_entry": self.daily_bias_strength,
            "current_session": self.current_session.value,
            "session_bias_at_entry": self.session_bias.value,
            "session_bias_strength_at_entry": self.session_bias_strength,
            "preferred_direction_at_entry": (
                None if self.preferred_direction is None else self.preferred_direction.value
            ),
            "trend_quality_at_entry": self.trend_quality.value,
            "volatility_state_at_entry": self.volatility_state.value,
            "opportunity_quality_at_entry": self.opportunity_quality,
            "reversal_risk_at_entry": self.reversal_risk.value,
            "previous_session_bias": (
                None if self.previous_session_bias is None else self.previous_session_bias.value
            ),
            "session_transition_state": self.session_transition_state,
            "best_session_so_far": (
                None if self.best_session_so_far is None else self.best_session_so_far.value
            ),
            "best_direction_so_far": (
                None if self.best_direction_so_far is None else self.best_direction_so_far.value
            ),
            "asia_bias": self.asia_bias.value,
            "london_bias": self.london_bias.value,
            "new_york_bias": self.new_york_bias.value,
        }
