"""A setup-outcome agent that records a favourable $6 XAUUSD price move.

This is deliberately NOT a directional detection agent and is not part of the six-agent
confluence score. It answers one research question after a setup has linked a real detection:

    From the first linked detection price, did price later travel 6.0 quote-price units in
    the setup's direction?

For XAUUSD a 6.0 quote-price move is the requested "$6 move" (for example 2650 -> 2656).
The agent observes candle high/low, not only closes, so a move that was reached intrabar is not
lost. It records facts only; it never creates a trade or changes setup lifecycle state.

The reference is persisted as a setup event before it is evaluated. A process restart therefore
cannot silently choose a newer detection as the reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aureon.models.enums import DirectionContext, SetupEventType
from aureon.models.setup import Setup

DEFAULT_FAVOURABLE_MOVE_PRICE = 6.0


@dataclass(frozen=True)
class MoveObservation:
    event_type: SetupEventType
    reason: str
    detail: dict[str, str]


class SixDollarMoveAgent:
    """Track the first linked detection of every setup until a +6 favourable move."""

    agent_name = "six_dollar_move"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        setup_repository: Any,
        detection_lookup: Any,
        target_price: float = DEFAULT_FAVOURABLE_MOVE_PRICE,
    ) -> None:
        if target_price <= 0:
            raise ValueError("target_price must be positive")
        self.setup_repository = setup_repository
        self.detection_lookup = detection_lookup
        self.target_price = float(target_price)

    def observe(self, setup: Setup, inputs: Any) -> MoveObservation | None:
        """Return the one observation that should be written on this candle, if any."""
        if setup.direction_context is DirectionContext.NEUTRAL:
            return None

        events = self.setup_repository.events.for_setup(setup.setup_id)
        if any(
            event.event_type is SetupEventType.FAVOURABLE_MOVE_6_REACHED
            for event in events
        ):
            return None

        tracking = next(
            (
                event
                for event in events
                if event.event_type is SetupEventType.FAVOURABLE_MOVE_6_TRACKING
            ),
            None,
        )
        if tracking is None:
            return self._start_tracking(setup)

        snapshot = tracking.context_snapshot
        try:
            reference_price = float(snapshot["reference_price"])
        except (KeyError, TypeError, ValueError):
            return None
        detection_id = snapshot.get("reference_detection_id", "")

        bullish = setup.direction_context is DirectionContext.BULLISH
        extreme = inputs.candle.high if bullish else inputs.candle.low
        excursion = (
            float(extreme) - reference_price
            if bullish
            else reference_price - float(extreme)
        )
        if excursion + 1e-12 < self.target_price:
            return None

        threshold_price = (
            reference_price + self.target_price
            if bullish
            else reference_price - self.target_price
        )
        return MoveObservation(
            event_type=SetupEventType.FAVOURABLE_MOVE_6_REACHED,
            reason=(
                f"price reached a favourable {self.target_price:g} move from the "
                "frozen linked-detection reference"
            ),
            detail={
                "agent": self.agent_name,
                "agent_version": self.agent_version,
                "reference_detection_id": detection_id,
                "reference_price": f"{reference_price:.5f}",
                "target_move_price": f"{self.target_price:.5f}",
                "threshold_price": f"{threshold_price:.5f}",
                "observed_extreme": f"{float(extreme):.5f}",
                "favourable_excursion": f"{excursion:.5f}",
                "source_timeframe": setup.timeframe.value,
            },
        )

    def _start_tracking(self, setup: Setup) -> MoveObservation | None:
        if not setup.linked_detection_ids:
            return None
        detection_id = setup.linked_detection_ids[0]
        detection = self.detection_lookup(detection_id)
        if detection is None:
            return None
        if detection.symbol.upper() != setup.symbol.upper():
            return None

        reference_price = float(detection.price)
        bullish = setup.direction_context is DirectionContext.BULLISH
        threshold_price = (
            reference_price + self.target_price
            if bullish
            else reference_price - self.target_price
        )
        return MoveObservation(
            event_type=SetupEventType.FAVOURABLE_MOVE_6_TRACKING,
            reason="frozen first linked detection as the $6-move reference",
            detail={
                "agent": self.agent_name,
                "agent_version": self.agent_version,
                "reference_detection_id": detection_id,
                "reference_price": f"{reference_price:.5f}",
                "target_move_price": f"{self.target_price:.5f}",
                "threshold_price": f"{threshold_price:.5f}",
                "source_timeframe": setup.timeframe.value,
            },
        )
