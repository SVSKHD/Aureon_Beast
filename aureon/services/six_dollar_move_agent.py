"""Setup outcome agent for favourable $6 / $20 / $40 XAUUSD price moves.

The agent freezes the first linked detection as the reference, then records milestone events
when price travels 6, 20 and 40 quote-price units in the setup direction. It is research-only:
it never votes in confluence, never creates a trade and never changes setup lifecycle state.

For XAUUSD, a 6.0 quote-price move means e.g. 2650 -> 2656.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aureon.models.enums import DirectionContext, SetupEventType
from aureon.models.setup import Setup

DEFAULT_FAVOURABLE_MOVE_TARGETS = (6.0, 20.0, 40.0)

_EVENT_FOR_TARGET: dict[float, SetupEventType] = {
    6.0: SetupEventType.FAVOURABLE_MOVE_6_REACHED,
    20.0: SetupEventType.FAVOURABLE_MOVE_20_REACHED,
    40.0: SetupEventType.FAVOURABLE_MOVE_40_REACHED,
}


@dataclass(frozen=True)
class MoveObservation:
    event_type: SetupEventType
    reason: str
    detail: dict[str, str]


class FavourableMoveAgent:
    """Track the first linked detection and the 6/20/40 favourable move ladder."""

    agent_name = "favourable_move"
    agent_version = "2.0.0"

    def __init__(
        self,
        *,
        setup_repository: Any,
        detection_lookup: Any,
        targets: tuple[float, ...] = DEFAULT_FAVOURABLE_MOVE_TARGETS,
    ) -> None:
        normalized = tuple(sorted({float(target) for target in targets}))
        if not normalized or any(target <= 0 for target in normalized):
            raise ValueError("targets must contain positive values")
        unsupported = [target for target in normalized if target not in _EVENT_FOR_TARGET]
        if unsupported:
            raise ValueError(f"unsupported move targets: {unsupported}")
        self.setup_repository = setup_repository
        self.detection_lookup = detection_lookup
        self.targets = normalized

    def observe(self, setup: Setup, inputs: Any) -> tuple[MoveObservation, ...]:
        """Return zero or more milestone observations reached on this candle."""
        if setup.direction_context is DirectionContext.NEUTRAL:
            return ()

        events = self.setup_repository.events.for_setup(setup.setup_id)
        tracking = next(
            (
                event
                for event in events
                if event.event_type is SetupEventType.FAVOURABLE_MOVE_6_TRACKING
            ),
            None,
        )
        if tracking is None:
            started = self._start_tracking(setup)
            return () if started is None else (started,)

        snapshot = tracking.context_snapshot or {}
        try:
            reference_price = float(snapshot["reference_price"])
        except (KeyError, TypeError, ValueError):
            return ()

        detection_id = snapshot.get("reference_detection_id", "")
        bullish = setup.direction_context is DirectionContext.BULLISH
        extreme = float(inputs.candle.high if bullish else inputs.candle.low)
        excursion = (
            extreme - reference_price
            if bullish
            else reference_price - extreme
        )

        seen = {event.event_type for event in events}
        observations: list[MoveObservation] = []
        for target in self.targets:
            event_type = _EVENT_FOR_TARGET[target]
            if event_type in seen or excursion + 1e-12 < target:
                continue
            threshold_price = (
                reference_price + target
                if bullish
                else reference_price - target
            )
            observations.append(
                MoveObservation(
                    event_type=event_type,
                    reason=(
                        f"price reached a favourable {target:g} move from the "
                        "frozen linked-detection reference"
                    ),
                    detail={
                        "agent": self.agent_name,
                        "agent_version": self.agent_version,
                        "reference_detection_id": detection_id,
                        "reference_price": f"{reference_price:.5f}",
                        "target_move_price": f"{target:.5f}",
                        "threshold_price": f"{threshold_price:.5f}",
                        "observed_extreme": f"{extreme:.5f}",
                        "favourable_excursion": f"{excursion:.5f}",
                        "source_timeframe": setup.timeframe.value,
                    },
                )
            )
        return tuple(observations)

    def _start_tracking(self, setup: Setup) -> MoveObservation | None:
        if not setup.linked_detection_ids:
            return None
        detection_id = setup.linked_detection_ids[0]
        detection = self.detection_lookup(detection_id)
        if detection is None or detection.symbol.upper() != setup.symbol.upper():
            return None

        reference_price = float(detection.price)
        bullish = setup.direction_context is DirectionContext.BULLISH
        thresholds = {
            str(int(target)): (
                reference_price + target
                if bullish
                else reference_price - target
            )
            for target in self.targets
        }
        return MoveObservation(
            event_type=SetupEventType.FAVOURABLE_MOVE_6_TRACKING,
            reason="frozen first linked detection as the favourable-move reference",
            detail={
                "agent": self.agent_name,
                "agent_version": self.agent_version,
                "reference_detection_id": detection_id,
                "reference_price": f"{reference_price:.5f}",
                "target_moves": ",".join(f"{target:g}" for target in self.targets),
                "threshold_6": f"{thresholds.get('6', reference_price):.5f}",
                "threshold_20": f"{thresholds.get('20', reference_price):.5f}",
                "threshold_40": f"{thresholds.get('40', reference_price):.5f}",
                "source_timeframe": setup.timeframe.value,
            },
        )


# Compatibility for the already-open #38/#39 code path.
SixDollarMoveAgent = FavourableMoveAgent
