"""Shared models for Aureon's logical agents 12-16.

These models carry decisions, not broker actions. They are intentionally explicit about
scores versus probabilities: a confluence score is the number of supporting facts under
the current rules, never a claimed win probability.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from aureon.models.base import AureonModel, UtcDatetime
from aureon.models.enums import Direction, Timeframe, TrendBias


class HtfState(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    MIXED = "mixed"
    NEUTRAL = "neutral"


class DirectorState(StrEnum):
    IGNORE = "ignore"
    WAIT = "wait"
    WATCH = "watch"
    FORMING = "forming"
    READY = "ready"
    INVALIDATED = "invalidated"


class RiskVerdict(StrEnum):
    ALLOW = "allow"
    ALLOW_REDUCED = "allow_reduced"
    VETO = "veto"
    INCOMPLETE = "incomplete"


class ExpansionPhase(StrEnum):
    WATCH = "watch"
    ARMED = "armed"
    ENTRY_WINDOW = "entry_window"
    EXPANDING = "expanding"
    MISSED = "missed"


class ExpansionFamily(StrEnum):
    REVERSAL = "reversal_expansion"
    CONTINUATION = "trend_continuation"
    BREAKOUT = "breakout_expansion"
    UNKNOWN = "unknown"


class ManagementAction(StrEnum):
    HOLD = "hold"
    PROTECT = "protect"
    HANDOFF_RUNNER = "handoff_runner"
    TRAIL = "trail"
    TIGHTEN = "tighten"
    EXIT = "exit"


class HigherTimeframeAssessment(AureonModel):
    state: HtfState = HtfState.NEUTRAL
    dominant_bias: TrendBias = TrendBias.SIDEWAYS
    reads: dict[str, str] = Field(default_factory=dict)
    bullish_count: int = Field(default=0, ge=0)
    bearish_count: int = Field(default=0, ge=0)
    sideways_count: int = Field(default=0, ge=0)
    available_count: int = Field(default=0, ge=0)
    strongest_available: Timeframe | None = None
    evidence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class DirectorDecision(AureonModel):
    state: DirectorState = DirectorState.WAIT
    direction: Direction | None = None
    primary_target_move: float = Field(default=10.0, gt=0)
    supporting: int = Field(default=0, ge=0)
    opposing: int = Field(default=0, ge=0)
    neutral: int = Field(default=0, ge=0)
    evidence: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    trigger_required: str | None = None
    rule_version: str = "DIRECTOR_V1"
    as_of: UtcDatetime | None = None


class RiskAssessment(AureonModel):
    verdict: RiskVerdict = RiskVerdict.INCOMPLETE
    direction: Direction | None = None
    entry_price: float | None = None
    primary_target_move: float = Field(default=10.0, gt=0)
    stop_distance: float | None = Field(default=None, ge=0)
    clear_room: float | None = Field(default=None, ge=0)
    open_positions: int | None = Field(default=None, ge=0)
    daily_realized_pnl: float | None = None
    evidence: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    rule_version: str = "RISK_V1"


class TradeManagementDecision(AureonModel):
    action: ManagementAction = ManagementAction.HOLD
    current_move: float
    peak_move: float
    giveback: float = Field(ge=0)
    primary_target_move: float = Field(default=10.0, gt=0)
    target_reached: bool = False
    protected_move: float | None = None
    trail_price: float | None = None
    reason: tuple[str, ...] = ()
    close_conditions: tuple[str, ...] = ()
    rule_version: str = "TRADE_MANAGER_V1"


class GuardianDecision(AureonModel):
    action: ManagementAction = ManagementAction.HOLD
    active: bool = False
    current_move: float
    peak_move: float
    giveback: float = Field(ge=0)
    protected_move: float | None = None
    trail_price: float | None = None
    continuation_score: int = Field(default=0, ge=0)
    continuation_total: int = Field(default=0, ge=0)
    evidence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    close_conditions: tuple[str, ...] = ()
    emergency: bool = False
    rule_version: str = "PROFIT_GUARDIAN_V1"


class ExpansionEntryCandidate(AureonModel):
    style: str
    price: float
    rationale: str
    valid: bool = True


class ExpansionOpportunity(AureonModel):
    phase: ExpansionPhase = ExpansionPhase.WATCH
    family: ExpansionFamily = ExpansionFamily.UNKNOWN
    direction: Direction | None = None
    strength: int = Field(default=0, ge=0)
    strength_total: int = Field(default=0, ge=0)
    move_from_anchor: float = 0.0
    anchor_price: float | None = None
    expansion_threshold: float = Field(default=10.0, gt=0)
    earliest_entry: ExpansionEntryCandidate | None = None
    confirmation_entry: ExpansionEntryCandidate | None = None
    pullback_entry: ExpansionEntryCandidate | None = None
    preferred_zone_low: float | None = None
    preferred_zone_high: float | None = None
    invalidation_reference: float | None = None
    evidence: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    signature: str | None = None
    as_of: UtcDatetime | None = None
    rule_version: str = "EXPANSION_OPPORTUNITY_V1"
