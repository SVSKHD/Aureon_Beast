"""Per-symbol V3 movement target ladders.

Targets are price-distance units, not account-currency P/L. XAUUSD keeps Aureon's
research ladder including the practical +3 target. XAGUSD uses a 1/100 scaled
ladder. Other approved symbols fall back to the same ladder expressed in broker
points so the outcome tracker never blindly applies gold-dollar distances.
"""
from __future__ import annotations

from dataclasses import dataclass

XAUUSD_TARGETS: tuple[float, ...] = (3.0, 5.0, 10.0, 20.0, 30.0, 40.0)
XAGUSD_TARGETS: tuple[float, ...] = (0.03, 0.05, 0.10, 0.20, 0.30, 0.40)
GENERIC_POINT_MULTIPLIERS: tuple[float, ...] = (
    300.0,
    500.0,
    1000.0,
    2000.0,
    3000.0,
    4000.0,
)


@dataclass(frozen=True)
class SymbolTargetLadder:
    symbol: str
    targets: tuple[float, ...]
    source: str


def _canonical(symbol: str) -> str:
    raw = symbol.upper()
    if raw.startswith("XAUUSD"):
        return "XAUUSD"
    if raw.startswith("XAGUSD"):
        return "XAGUSD"
    return raw


def target_ladder_for_symbol(
    symbol: str,
    *,
    point_size: float | None = None,
) -> SymbolTargetLadder:
    canonical = _canonical(symbol)
    if canonical == "XAUUSD":
        return SymbolTargetLadder(canonical, XAUUSD_TARGETS, "xau_v3")
    if canonical == "XAGUSD":
        return SymbolTargetLadder(canonical, XAGUSD_TARGETS, "xag_v3_scaled")

    if point_size is None or point_size <= 0:
        raise ValueError(
            f"{symbol} has no explicit V3 target ladder and no positive point_size"
        )
    targets = tuple(
        round(float(point_size) * multiplier, 10)
        for multiplier in GENERIC_POINT_MULTIPLIERS
    )
    return SymbolTargetLadder(canonical, targets, "broker_point_scaled")
