"""The agent contract (§79).

An agent is a **pure function of ``(window, ctx)``**. Same window, same context,
same parameters means the same detections -- no clock reads, no Firestore lookups,
no state carried between calls, no random numbers. That purity is exactly what the
replay/live parity test verifies (§82), and it is what makes a detection
reproducible months later from the candle that produced it.

An agent also cannot trade. There is no broker on this interface, the boundary
tests forbid ``aureon.agents`` from importing ``aureon.execution``, and a detection
has no field an executor reads.

## The window contract

``window`` is a pandas DataFrame of CLOSED candles, oldest first, with:

* a UTC ``DatetimeIndex`` of candle **open** times, and
* columns ``open``, ``high``, ``low``, ``close``, ``tick_volume``, ``real_volume``.

The last row is the candle that just closed -- the one being evaluated. Its
**length is fixed by the engine and is part of the parity contract**: the recursive
indicators depend on how much history they are given, so a shorter window in live
than in replay would move crossovers onto different candles. See
``aureon.engine.indicators``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from aureon.models.base import MarketTime
from aureon.models.detection import AgentEvidence, CandleContext, Detection, IndicatorSnapshot
from aureon.models.enums import Direction
from aureon.models.identity import detection_id

WINDOW_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "real_volume",
)


class BaseAgent(ABC):
    """Base class for every detection agent."""

    #: Stable identifier, stored on every detection. Never rename a shipped agent:
    #: the name is part of the detection id, so renaming re-keys history.
    agent_name: str = "base"

    #: Semantic version of the agent's LOGIC. Bump it when the detection rule
    #: changes. It is PART OF THE DETECTION ID (§12, decision 97), so a version bump
    #: forks history: the new version's detections sit beside the old version's for
    #: the same candles rather than replacing them. Never bump it for a change that
    #: was meant to correct history in place.
    agent_version: str = "0.0.0"

    @abstractmethod
    def params_snapshot(self) -> dict[str, object]:
        """Every parameter that affects this agent's output (§84).

        Stored verbatim on each detection. The test of completeness: could someone
        reproduce this detection from the candle plus this dict alone? If a
        parameter is missing, the detection is not reproducible, and a later tuning
        change becomes impossible to account for.
        """

    @abstractmethod
    def on_closed_candle(self, window: pd.DataFrame, ctx: CandleContext) -> list[Detection]:
        """Evaluate the newly-closed candle (the last row of ``window``).

        Returns zero or more detections. Most candles produce none.
        """

    # ── Helpers for subclasses ────────────────────────────────────────────────

    def min_window(self) -> int:
        """Bars this agent needs before it can emit anything.

        The engine uses the largest ``min_window()`` across its agents to size the
        rolling window, so an agent that under-reports would be handed too little
        history and would compute subtly wrong indicators rather than fail loudly.
        """
        return 1

    def build_detection(
        self,
        *,
        ctx: CandleContext,
        event_key: str,
        price: float,
        direction: Direction | None = None,
        indicators: IndicatorSnapshot | None = None,
        levels: dict[str, float] | None = None,
        evidence: AgentEvidence | None = None,
    ) -> Detection:
        """Assemble a Detection with a correct, deterministic id.

        Centralised so no agent can compute an id from a different component tuple.
        Two agents disagreeing on that would produce colliding or duplicated
        documents, which is the kind of bug that only shows up as odd counts weeks
        later.
        """
        candle_open = ctx.candle_open_time
        return Detection(
            detection_id=detection_id(
                account_scope=ctx.account_scope,
                symbol=ctx.symbol,
                timeframe=ctx.timeframe.value,
                # The CLOSE, not the open: a detection becomes known when the candle
                # closes, and §12 keys it on that instant.
                candle_close=ctx.closed_at.utc,
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                event_key=event_key,
            ),
            account_scope=ctx.account_scope,
            symbol=ctx.symbol,
            timeframe=ctx.timeframe,
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            agent_params_snapshot=self.params_snapshot(),
            event_key=event_key,
            direction=direction,
            detected_at=ctx.closed_at,
            candle_open_time=candle_open,
            price=price,
            indicators=indicators or IndicatorSnapshot(),
            session=ctx.session,
            levels=levels or {},
            evidence=evidence or AgentEvidence(),
            sequence_today=ctx.sequence_today,
            sequence_session=ctx.sequence_session,
        )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{type(self).__name__}({self.agent_name} v{self.agent_version})"


def validate_window(window: pd.DataFrame) -> None:
    """Check a window matches the contract, with an actionable message.

    Called by agents before computing. A malformed window would otherwise surface
    as a KeyError or a silently wrong indicator deep inside pandas.
    """
    missing = [c for c in WINDOW_COLUMNS if c not in window.columns]
    if missing:
        raise ValueError(f"window is missing columns {missing}; got {list(window.columns)}")
    if not isinstance(window.index, pd.DatetimeIndex):
        raise ValueError(f"window index must be a DatetimeIndex, got {type(window.index).__name__}")
    if window.index.tz is None:
        raise ValueError("window index must be tz-aware (UTC)")
    if not window.index.is_monotonic_increasing:
        raise ValueError("window must be sorted oldest-first")


def window_market_time(window: pd.DataFrame, market_tz: str, position: int = -1) -> MarketTime:
    """MarketTime of the candle at ``position`` in the window."""
    return MarketTime.from_utc(window.index[position].to_pydatetime(), market_tz)
