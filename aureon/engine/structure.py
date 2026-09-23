"""Confirmed swing-structure labels for chart/research context.

A swing at index i is confirmed only after ``strength`` later closed bars exist. This is
important for live use: the latest candles are never labelled with retrospective pivots that
were not knowable at the time.

Labels compare each confirmed swing with the previous confirmed swing of the same kind:
- HH: higher high
- LH: lower high
- HL: higher low
- LL: lower low
- SH/SL: first confirmed swing high/low, where no comparison exists yet

Equal pivots are labelled EH/EL rather than forced into a directional structure.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

DEFAULT_SWING_STRENGTH = 2


@dataclass(frozen=True)
class StructurePoint:
    at: datetime
    price: float
    label: str
    kind: str


def structure_points(
    candles: Sequence[Any], *, strength: int = DEFAULT_SWING_STRENGTH
) -> tuple[StructurePoint, ...]:
    """Return only confirmed swing points, with no look-ahead beyond closed bars."""
    if strength < 1:
        raise ValueError("strength must be >= 1")
    if len(candles) < 2 * strength + 1:
        return ()

    points: list[StructurePoint] = []
    previous_high: float | None = None
    previous_low: float | None = None

    for i in range(strength, len(candles) - strength):
        here = candles[i]
        window = candles[i - strength : i + strength + 1]

        is_high = all(here.high >= other.high for other in window) and any(
            here.high > other.high for other in window
        )
        is_low = all(here.low <= other.low for other in window) and any(
            here.low < other.low for other in window
        )

        at = getattr(getattr(here, "open_time", None), "utc", None)
        if at is None:
            at = getattr(here, "at")

        if is_high:
            if previous_high is None:
                label = "SH"
            elif here.high > previous_high:
                label = "HH"
            elif here.high < previous_high:
                label = "LH"
            else:
                label = "EH"
            points.append(StructurePoint(at=at, price=float(here.high), label=label, kind="high"))
            previous_high = float(here.high)

        if is_low:
            if previous_low is None:
                label = "SL"
            elif here.low > previous_low:
                label = "HL"
            elif here.low < previous_low:
                label = "LL"
            else:
                label = "EL"
            points.append(StructurePoint(at=at, price=float(here.low), label=label, kind="low"))
            previous_low = float(here.low)

    return tuple(sorted(points, key=lambda point: (point.at, point.kind)))


def labels_by_time(
    candles: Sequence[Any], *, strength: int = DEFAULT_SWING_STRENGTH
) -> dict[datetime, tuple[str, ...]]:
    grouped: dict[datetime, list[str]] = {}
    for point in structure_points(candles, strength=strength):
        grouped.setdefault(point.at, []).append(point.label)
    return {at: tuple(labels) for at, labels in grouped.items()}


__all__ = [
    "DEFAULT_SWING_STRENGTH",
    "StructurePoint",
    "labels_by_time",
    "structure_points",
]
