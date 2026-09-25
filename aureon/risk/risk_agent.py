"""Logical Agent 13: pre-trade risk assessment.

This module is pure. Account P&L/exposure are explicit inputs; missing account context
returns INCOMPLETE rather than pretending market context alone is a risk approval.
"""

from __future__ import annotations

from dataclasses import dataclass

from aureon.models.agent_decision import RiskAssessment, RiskVerdict
from aureon.models.enums import Direction


@dataclass(frozen=True)
class RiskInputs:
    direction: Direction | None
    entry_price: float | None
    primary_target_move: float = 10.0
    stop_price: float | None = None
    nearest_obstacle_price: float | None = None
    open_positions: int | None = None
    max_open_positions: int = 1
    daily_realized_pnl: float | None = None
    daily_loss_limit: float | None = None
    volatility_regime: str | None = None


class RiskAgent:
    agent_name = "risk"
    agent_version = "1.0.0"

    def assess(self, inputs: RiskInputs) -> RiskAssessment:
        missing: list[str] = []
        blockers: list[str] = []
        evidence: list[str] = []

        if inputs.direction is None:
            blockers.append("no proposed direction")
        if inputs.entry_price is None:
            missing.append("entry_price")
        if inputs.open_positions is None:
            missing.append("open_positions")
        if inputs.daily_loss_limit is not None and inputs.daily_realized_pnl is None:
            missing.append("daily_realized_pnl")

        stop_distance = None
        if inputs.entry_price is not None and inputs.stop_price is not None:
            stop_distance = abs(inputs.entry_price - inputs.stop_price)
            evidence.append(f"stop distance {stop_distance:g}")

        clear_room = None
        if (
            inputs.entry_price is not None
            and inputs.nearest_obstacle_price is not None
            and inputs.direction is not None
        ):
            signed = (
                inputs.nearest_obstacle_price - inputs.entry_price
                if inputs.direction is Direction.BUY
                else inputs.entry_price - inputs.nearest_obstacle_price
            )
            clear_room = max(0.0, signed)
            evidence.append(f"clear room {clear_room:g}")
            if clear_room < inputs.primary_target_move:
                blockers.append(
                    f"only {clear_room:g} clear room before obstacle; "
                    f"primary target needs {inputs.primary_target_move:g}"
                )

        if inputs.open_positions is not None:
            evidence.append(
                f"open positions {inputs.open_positions}/{inputs.max_open_positions}"
            )
            if inputs.open_positions >= inputs.max_open_positions:
                blockers.append("maximum open-position exposure reached")

        if (
            inputs.daily_loss_limit is not None
            and inputs.daily_realized_pnl is not None
            and inputs.daily_realized_pnl <= -abs(inputs.daily_loss_limit)
        ):
            blockers.append("daily loss limit reached")

        if inputs.volatility_regime in {"volatile_chop", "structurally_messy"}:
            blockers.append(f"market regime is {inputs.volatility_regime}")

        if blockers:
            verdict = RiskVerdict.VETO
        elif missing:
            verdict = RiskVerdict.INCOMPLETE
        elif stop_distance is not None and stop_distance > inputs.primary_target_move * 2:
            verdict = RiskVerdict.ALLOW_REDUCED
            evidence.append("wide stop relative to primary target")
        else:
            verdict = RiskVerdict.ALLOW

        return RiskAssessment(
            verdict=verdict,
            direction=inputs.direction,
            entry_price=inputs.entry_price,
            primary_target_move=inputs.primary_target_move,
            stop_distance=stop_distance,
            clear_room=clear_room,
            open_positions=inputs.open_positions,
            daily_realized_pnl=inputs.daily_realized_pnl,
            evidence=tuple(evidence),
            blockers=tuple(blockers),
            missing=tuple(missing),
        )
