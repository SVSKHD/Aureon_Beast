"""Aureon V1.5 EMA sequence research contracts.

These models are intentionally separate from the V1 training contract.  They are
research/label artifacts only and MUST NOT be consumed by the live execution path
or by model training until the Phase-2 evidence gate is passed.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, Field

from aureon.models.base import AureonModel, UtcDatetime
from aureon.models.enums import Direction, Timeframe

SEQUENCE_SCHEMA_V1 = "AUREON_EMA_SEQUENCE_V1"


class EMASequenceOutcome(StrEnum):
    IMMEDIATE_CONTINUATION = "IMMEDIATE_CONTINUATION"
    PULLBACK_THEN_CONTINUATION = "PULLBACK_THEN_CONTINUATION"
    DEEP_PULLBACK_THEN_CONTINUATION = "DEEP_PULLBACK_THEN_CONTINUATION"
    FAILED_DIRECTION = "FAILED_DIRECTION"
    AMBIGUOUS_PATH = "AMBIGUOUS_PATH"


class TargetLadder(AureonModel):
    model_config = ConfigDict(frozen=True)

    reached_6: bool = False
    reached_10: bool = False
    reached_20: bool = False
    reached_30: bool = False
    reached_40: bool = False
    bars_to_6: int | None = Field(default=None, ge=1)
    bars_to_10: int | None = Field(default=None, ge=1)
    bars_to_20: int | None = Field(default=None, ge=1)
    bars_to_30: int | None = Field(default=None, ge=1)
    bars_to_40: int | None = Field(default=None, ge=1)


class EMASequenceSnapshot(AureonModel):
    """Facts frozen on the crossover candle only."""

    model_config = ConfigDict(extra="allow", frozen=True)

    sequence_schema: str = SEQUENCE_SCHEMA_V1
    sequence_id: str
    symbol: str
    timeframe: Timeframe
    timestamp: UtcDatetime
    direction: Direction
    cross_price: float

    ema_fast: float
    ema_slow: float
    ema_fast_slope: float | None = None
    ema_slow_slope: float | None = None
    ema_separation: float | None = None
    ema_separation_change: float | None = None

    rsi: float | None = None
    rsi_change: float | None = None
    atr: float | None = Field(default=None, ge=0)

    htf_alignment: str | None = None
    daily_market_bias: str | None = None
    market_regime: str | None = None
    volatility_regime: str | None = None
    session: str | None = None
    wick_state: str | None = None
    liquidity_state: str | None = None
    breakout_state: str | None = None
    market_structure: str | None = None
    participation: str | None = None

    agent_direction: str | None = None
    agent_confidence: float | None = None
    agent_states: dict = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)


class EMASequenceLabel(AureonModel):
    """Future-only research label for one frozen crossover snapshot."""

    model_config = ConfigDict(frozen=True)

    sequence_schema: str = SEQUENCE_SCHEMA_V1
    outcome: EMASequenceOutcome
    horizon_bars: int = Field(ge=1)

    pullback_move: float = Field(default=0.0, ge=0)
    pullback_bars: int | None = Field(default=None, ge=1)
    continuation_move: float = Field(default=0.0, ge=0)
    max_favourable_move: float = Field(default=0.0, ge=0)
    max_adverse_move: float = Field(default=0.0, ge=0)

    continuation: TargetLadder = Field(default_factory=TargetLadder)
    counter_move: TargetLadder = Field(default_factory=TargetLadder)

    path_ambiguous: bool = False
    diagnostics: dict = Field(default_factory=dict)


class EMASequenceRecord(AureonModel):
    """Complete research row: frozen snapshot plus future-only label."""

    model_config = ConfigDict(frozen=True)

    sequence_schema: str = SEQUENCE_SCHEMA_V1
    snapshot: EMASequenceSnapshot
    label: EMASequenceLabel
