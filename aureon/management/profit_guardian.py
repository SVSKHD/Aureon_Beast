"""Logical Agent 16: profit guardian after the primary +10 move.

Tracks the runner and makes every trail/exit reason explicit. It never sends the close
itself; a future management executor can consume EXIT after applying the existing live
account and reconciliation gates.
"""

from __future__ import annotations

from aureon.models.agent_decision import GuardianDecision, ManagementAction
from aureon.models.enums import Direction


class ProfitGuardianAgent:
    agent_name = "profit_guardian"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        primary_target_move: float = 10.0,
        minimum_lock: float = 4.0,
        trail_fraction_of_peak: float = 0.55,
        tighten_fraction_of_peak: float = 0.70,
        emergency_giveback: float = 8.0,
        tighten_giveback: float = 5.0,
    ) -> None:
        if primary_target_move <= 0 or minimum_lock < 0:
            raise ValueError("target and lock must be non-negative")
        if not 0 < trail_fraction_of_peak <= tighten_fraction_of_peak < 1:
            raise ValueError("trail fractions must satisfy 0 < trail <= tighten < 1")
        self.primary_target_move = primary_target_move
        self.minimum_lock = minimum_lock
        self.trail_fraction_of_peak = trail_fraction_of_peak
        self.tighten_fraction_of_peak = tighten_fraction_of_peak
        self.emergency_giveback = emergency_giveback
        self.tighten_giveback = tighten_giveback

    def assess(
        self,
        *,
        direction: Direction,
        entry_price: float,
        current_price: float,
        peak_price: float,
        market_health: dict[str, bool | None] | None = None,
    ) -> GuardianDecision:
        current_move = (current_price - entry_price) * direction.sign
        peak_move = max(0.0, (peak_price - entry_price) * direction.sign)
        giveback = max(0.0, peak_move - current_move)
        active = peak_move >= self.primary_target_move
        health = market_health or {}

        positives = [name for name, value in health.items() if value is True]
        negatives = [name for name, value in health.items() if value is False]
        total = len([value for value in health.values() if value is not None])
        score = len(positives)

        if not active:
            return GuardianDecision(
                action=ManagementAction.HOLD,
                active=False,
                current_move=current_move,
                peak_move=peak_move,
                giveback=giveback,
                continuation_score=score,
                continuation_total=total,
                evidence=tuple(f"{name}: healthy" for name in positives),
                warnings=("primary target not reached; guardian on standby",),
            )

        emergency = (
            giveback >= self.emergency_giveback
            and (len(negatives) >= 2 or current_move <= self.minimum_lock)
        )
        if emergency:
            action = ManagementAction.EXIT
            fraction = self.tighten_fraction_of_peak
        elif giveback >= self.tighten_giveback or len(negatives) >= 2:
            action = ManagementAction.TIGHTEN
            fraction = self.tighten_fraction_of_peak
        else:
            action = ManagementAction.TRAIL
            fraction = self.trail_fraction_of_peak

        desired_protection = max(self.minimum_lock, peak_move * fraction)
        if action is ManagementAction.EXIT:
            # EXIT is a market-action recommendation, so "protected" means what is still
            # available now rather than a stop level above/below the executable quote.
            protected_move = max(0.0, current_move)
        else:
            # A stop beyond the current executable price would immediately trigger or be
            # rejected. Leave a small price-unit buffer and never claim more protection
            # than the market currently offers.
            executable_cap = max(0.0, current_move - 0.25)
            protected_move = min(desired_protection, executable_cap)
        trail_price = entry_price + direction.sign * protected_move

        evidence = tuple(f"{name}: healthy" for name in positives)
        warnings = tuple(f"{name}: weakening" for name in negatives)
        close_conditions = (
            f"emergency giveback >= {self.emergency_giveback:g} with >=2 weakening conditions",
            f"hard protected move <= {protected_move:g}",
            "structure/momentum failure supplied by market-health inputs",
        )

        return GuardianDecision(
            action=action,
            active=True,
            current_move=current_move,
            peak_move=peak_move,
            giveback=giveback,
            protected_move=protected_move,
            trail_price=trail_price,
            continuation_score=score,
            continuation_total=total,
            evidence=evidence,
            warnings=warnings,
            close_conditions=close_conditions,
            emergency=emergency,
        )
