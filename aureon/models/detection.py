"""Detections: what an agent observed on one closed candle (§11-§13, §19).

Two invariants from CLAUDE.md are enforced in this file:

* **Detections are immutable.** The model is frozen, so a detection cannot be
  edited after the fact -- not by a later candle, not by an evaluator, not by a
  review. Outcomes live in ``detection_evaluations``.
* **No field may encode future information.** This is the subtler one and it is
  what keeps the whole research loop honest. If a detection could carry "this one
  worked", every count computed from detections would be contaminated by
  hindsight, and the reached-N statistics would measure the evaluator rather than
  the strategy. A detection records only what was knowable at the candle close
  that produced it. ``tests/unit/test_detection_immutability.py`` asserts this
  structurally so a well-meaning future field cannot slip in.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field

from aureon.models.base import AureonDocument, AureonModel, MarketTime
from aureon.models.enums import Direction, SessionName, Timeframe


class IndicatorSnapshot(AureonModel):
    """Indicator values AT the closed candle (§13, §14).

    Stored as values, not labels: "rsi was 61.4" survives a later change to
    whatever we currently call overbought, whereas a stored label does not.
    """

    ema: dict[str, float] = Field(
        default_factory=dict,
        description='Named EMA values, e.g. {"fast": 2401.2, "slow": 2399.8}.',
    )
    rsi: float | None = Field(default=None, description="RSI, when warmed up.")
    extras: dict[str, float] = Field(
        default_factory=dict, description="Agent-specific numeric context."
    )


class SessionContext(AureonModel):
    """Which session the candle closed in (§18).

    ``session_config_version`` is stamped because session boundaries are config
    (decision 7). Without it, a later boundary change would silently reinterpret
    old detections, and a session comparison across a config change would be
    meaningless rather than merely wrong.
    """

    session: SessionName
    session_config_version: int


class CandleContext(AureonModel):
    """What the engine hands an agent alongside the candle window (§79).

    Everything an agent needs that is *not* derivable from the price window
    itself. Agents are pure functions of ``(window, ctx)``: given the same two,
    an agent must return the same detections, which is precisely what the
    replay/live parity test checks.
    """

    account_scope: str
    symbol: str
    timeframe: Timeframe
    market_tz: str
    closed_at: MarketTime = Field(description="The candle's close instant.")
    candle_open_time: MarketTime
    session: SessionContext
    sequence_today: int = Field(
        ge=1, description="Nth detection-eligible candle in the broker day."
    )
    sequence_session: int = Field(ge=1, description="Nth within the session.")


class Detection(AureonDocument):
    """An immutable observation. Never an instruction to trade.

    The name of the central invariant: *a detection never creates a trade*. This
    document has no field that an executor reads, and the boundary tests prevent
    any observer-side module from even importing the execution package.
    """

    # Frozen: any "correction" is a new detection over a new candle, never an
    # edit. validate_assignment would only report the mutation; frozen forbids it.
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    detection_id: str = Field(description="Deterministic id from identity.detection_id.")
    account_scope: str

    symbol: str
    timeframe: Timeframe

    agent_name: str
    agent_version: str
    agent_params_snapshot: dict[str, object] = Field(
        default_factory=dict,
        description="Full agent parameters at observation time (§84).",
    )

    event_key: str = Field(
        description='Agent-specific event, e.g. "bullish" or "up|previous_day_high".'
    )
    direction: Direction | None = Field(
        default=None,
        description="Direction, where the event has one. Context-only agents omit it.",
    )

    detected_at: MarketTime = Field(description="Candle CLOSE: when this became known.")
    candle_open_time: MarketTime

    price: float = Field(description="Candle close price at detection.")
    indicators: IndicatorSnapshot = Field(default_factory=IndicatorSnapshot)
    session: SessionContext
    levels: dict[str, float] = Field(
        default_factory=dict,
        description="Numeric levels involved (swept level, broken level, ...).",
    )

    sequence_today: int = Field(ge=1)
    sequence_session: int = Field(ge=1)

    @property
    def is_context_only(self) -> bool:
        """Whether this detection carries no tradeable direction (§14, §16)."""
        return self.direction is None
