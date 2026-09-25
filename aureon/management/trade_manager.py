"""Logical Agent 14: management from entry until the +10 primary move.

The output is an action proposal. It never calls a broker.
"""

from __future__ import annotations

from aureon.models.agent_decision import ManagementAction, TradeManagementDecision
from aureon.models.enums import Direction


class TradeManagementAgent:
    agent_name = "trade_manager"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        primary_target_move: float = 10.0,
        protect_after_move: float = 6.0,
        protect_fraction: float = 0.25,
    ) -> None:
        if primary_target_move <= 0:
            raise ValueError("primary_target_move must be positive")
        if not 0 <= protect_fraction < 1:
            raise ValueError("protect_fraction must be in [0,1)")
        self.primary_target_move = primary_target_move
        self.protect_after_move = protect_after_move
        self.protect_fraction = protect_fraction

    def assess(
        self,
        *,
        direction: Direction,
        entry_price: float,
        current_price: float,
        peak_price: float,
    ) -> TradeManagementDecision:
        current_move = (current_price - entry_price) * direction.sign
        peak_move = max(0.0, (peak_price - entry_price) * direction.sign)
        giveback = max(0.0, peak_move - current_move)
        target_reached = peak_move >= self.primary_target_move

        protected_move = None
        trail_price = None
        reasons: list[str] = []
        close_conditions = (
            "hard invalidation or broker SL",
            "primary target handoff to Profit Guardian",
        )

        if target_reached:
            action = ManagementAction.HANDOFF_RUNNER
            protected_move = max(0.0, self.primary_target_move * self.protect_fraction)
            trail_price = entry_price + direction.sign * protected_move
            reasons.append("primary +10 move reached; hand off runner management")
        elif peak_move >= self.protect_after_move:
            action = ManagementAction.PROTECT
            protected_move = max(0.0, peak_move * self.protect_fraction)
            trail_price = entry_price + direction.sign * protected_move
            reasons.append("trade has meaningful favourable excursion; begin protection")
        else:
            action = ManagementAction.HOLD
            reasons.append("primary target not reached and protection threshold not reached")

        return TradeManagementDecision(
            action=action,
            current_move=current_move,
            peak_move=peak_move,
            giveback=giveback,
            primary_target_move=self.primary_target_move,
            target_reached=target_reached,
            protected_move=protected_move,
            trail_price=trail_price,
            reason=tuple(reasons),
            close_conditions=close_conditions,
        )
