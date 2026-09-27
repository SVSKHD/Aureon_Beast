"""Deterministic live exit manager (V1).

The live counterpart of the backtester's trailing simulation. It wraps the existing
Agent 14 (``TradeManagementAgent``) and Agent 16 (``ProfitGuardianAgent``) proposals in a
durable lifecycle and adds the three things a proposal cannot carry on its own:

1. **A ratchet.** ``current_stop`` never loosens. Agent 16 recomputes its protection from
   the current quote every tick; the state remembers the best stop ever set.
2. **Priority.** HARD RISK, then STRUCTURAL INVALIDATION, then PROFIT PROTECTION /
   TRAILING. A trailing suggestion can never override a hard-risk exit, and no model
   output is consulted anywhere in here: the entry model is not an exit model.
3. **Memory.** MFE/MAE, the +5/+10/+20/+30/+40 ladder, the activation prices, the number
   of trail updates, the peak, and -- once closed -- the exit experience.

It is a **pure** state machine. ``assess`` takes the previous state and the current
quote and returns the new state plus a decision. It never calls a broker: the position
monitor persists the state and, when autonomous management is enabled, hands the
decision to the executor through a control request. Every phase change goes through
``assert_management_transition``.

+$5 is NOT a take-profit. It is the point where protection begins; a strong winner keeps
running toward +10/+20/+30/+40 behind a stop that only ever tightens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aureon.management.profit_guardian import ProfitGuardianAgent
from aureon.management.trade_manager import TradeManagementAgent
from aureon.models.agent_decision import GuardianDecision, ManagementAction, TradeManagementDecision
from aureon.models.base import to_utc, utc_now
from aureon.models.enums import (
    Direction,
    ExitPriority,
    ManagementPhase,
    assert_management_transition,
)
from aureon.models.trade import TradeManagementState

LADDER: tuple[int, ...] = (5, 10, 20, 30, 40)


@dataclass(frozen=True)
class ExitPolicy:
    """Every number that shapes live management. Recorded on the state for audit.

    The defaults are the ones already used by Agents 14/16 and by the backtester's
    trailing simulation (activation +5, lock 4, trail 0.55 of peak). Nothing more
    aggressive is invented here; tune through ``AureonConfig``/environment, not by hand.
    """

    activation_move: float = 5.0
    minimum_lock: float = 4.0
    trail_fraction_of_peak: float = 0.55
    tighten_fraction_of_peak: float = 0.70
    tighten_giveback: float = 5.0
    emergency_giveback: float = 8.0
    #: Distance from entry (price units) at which hard risk exits regardless of anything
    #: else. ``None`` means "use the broker stop supplied at entry"; a position with
    #: neither has no hard-risk line and the manager says so in its decision.
    hard_stop_move: float | None = None
    #: A stop is never placed closer than this to the executable quote.
    executable_buffer: float = 0.25

    def __post_init__(self) -> None:
        if self.activation_move <= 0:
            raise ValueError("activation_move must be > 0")
        if not 0 <= self.minimum_lock < self.activation_move:
            raise ValueError("minimum_lock must be >= 0 and < activation_move")
        if not 0 < self.trail_fraction_of_peak <= self.tighten_fraction_of_peak < 1:
            raise ValueError("trail fractions must satisfy 0 < trail <= tighten < 1")
        if self.hard_stop_move is not None and self.hard_stop_move <= 0:
            raise ValueError("hard_stop_move must be > 0 when set")

    def as_dict(self) -> dict[str, float]:
        return {
            "activation_move": self.activation_move,
            "minimum_lock": self.minimum_lock,
            "trail_fraction_of_peak": self.trail_fraction_of_peak,
            "tighten_fraction_of_peak": self.tighten_fraction_of_peak,
            "tighten_giveback": self.tighten_giveback,
            "emergency_giveback": self.emergency_giveback,
            "hard_stop_move": -1.0 if self.hard_stop_move is None else self.hard_stop_move,
            "executable_buffer": self.executable_buffer,
        }

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ExitPolicy:
        import os

        source = os.environ if env is None else env

        def _f(name: str, default: float | None) -> float | None:
            raw = str(source.get(name, "") or "").strip()
            if not raw:
                return default
            return float(raw)

        return cls(
            activation_move=_f("AUREON_EXIT_ACTIVATION_MOVE", 5.0) or 5.0,
            minimum_lock=_f("AUREON_EXIT_MINIMUM_LOCK", 4.0) or 0.0,
            trail_fraction_of_peak=_f("AUREON_EXIT_TRAIL_FRACTION", 0.55) or 0.55,
            tighten_fraction_of_peak=_f("AUREON_EXIT_TIGHTEN_FRACTION", 0.70) or 0.70,
            tighten_giveback=_f("AUREON_EXIT_TIGHTEN_GIVEBACK", 5.0) or 5.0,
            emergency_giveback=_f("AUREON_EXIT_EMERGENCY_GIVEBACK", 8.0) or 8.0,
            hard_stop_move=_f("AUREON_EXIT_HARD_STOP_MOVE", None),
        )


@dataclass(frozen=True)
class ExitDecision:
    """What the manager wants done now, and why. Consumed by the monitor, never a broker."""

    action: ManagementAction
    priority: ExitPriority
    reason: str
    phase: ManagementPhase
    previous_phase: ManagementPhase
    new_stop: float | None = None
    previous_stop: float | None = None
    exit_requested: bool = False
    stop_changed: bool = False
    phase_changed: bool = False
    management: TradeManagementDecision | None = None
    guardian: GuardianDecision | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.exit_requested or self.stop_changed


class DeterministicExitManager:
    """Pure lifecycle: (state, quote, context) -> (state, decision)."""

    rule_version = "EXIT_MANAGER_V1"

    def __init__(self, policy: ExitPolicy | None = None) -> None:
        self.policy = policy or ExitPolicy()
        self.trade_manager = TradeManagementAgent(
            primary_target_move=self.policy.activation_move,
            protect_after_move=self.policy.activation_move,
            protect_fraction=self.policy.minimum_lock / self.policy.activation_move,
        )
        self.guardian = ProfitGuardianAgent(
            primary_target_move=self.policy.activation_move,
            minimum_lock=self.policy.minimum_lock,
            trail_fraction_of_peak=self.policy.trail_fraction_of_peak,
            tighten_fraction_of_peak=self.policy.tighten_fraction_of_peak,
            emergency_giveback=self.policy.emergency_giveback,
            tighten_giveback=self.policy.tighten_giveback,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def open(
        self,
        *,
        entry_price: float,
        direction: Direction,
        initial_stop: float | None,
        now: datetime | None = None,
    ) -> TradeManagementState:
        """The state a freshly opened Aureon position starts in."""
        stop = initial_stop
        if stop is None and self.policy.hard_stop_move is not None:
            stop = entry_price - direction.sign * self.policy.hard_stop_move
        return TradeManagementState(
            phase=ManagementPhase.OPEN,
            entry_price=float(entry_price),
            direction=direction,
            initial_stop=stop,
            current_stop=stop,
            updated_at=to_utc(now or utc_now()),
            policy=self.policy.as_dict(),
            rule_version=self.rule_version,
        )

    def assess(
        self,
        state: TradeManagementState,
        *,
        current_price: float,
        high: float | None = None,
        low: float | None = None,
        market_health: dict[str, bool | None] | None = None,
        structural_invalidation: str | None = None,
        now: datetime | None = None,
    ) -> tuple[TradeManagementState, ExitDecision]:
        """Advance the state by one quote. Pure: same inputs, same outputs.

        ``high``/``low`` let a candle (or a bid/ask pair) update the excursions more
        faithfully than one price; they default to ``current_price``.
        ``structural_invalidation`` is a reason string supplied by the caller when the
        setup's structure has failed (an opposite structural event, the HTF flipping and
        the session turning). The manager never derives it from a model.
        """
        moment = to_utc(now or utc_now())
        previous_phase = state.phase
        previous_stop = state.current_stop
        if state.is_terminal:
            return state, ExitDecision(
                action=ManagementAction.HOLD,
                priority=ExitPriority.NONE,
                reason="position is closed; nothing to manage",
                phase=state.phase,
                previous_phase=previous_phase,
            )

        sign = state.direction.sign
        entry = state.entry_price
        favourable_price = high if high is not None else current_price
        adverse_price = low if low is not None else current_price
        if sign < 0:
            favourable_price, adverse_price = (
                low if low is not None else current_price,
                high if high is not None else current_price,
            )
        favourable = max(0.0, (favourable_price - entry) * sign)
        adverse = max(0.0, (entry - adverse_price) * sign)
        current_move = (current_price - entry) * sign

        update: dict[str, Any] = {
            "updated_at": moment,
            "current_move": current_move,
            "mfe": max(state.mfe, favourable),
            "mae": max(state.mae, adverse),
        }
        if favourable >= state.mfe and favourable > 0:
            update["peak_favorable_price"] = favourable_price
        for target in LADDER:
            key = f"reached_{target}"
            if not getattr(state, key) and update["mfe"] >= float(target):
                update[key] = True
        working = state.model_copy(update=update)
        peak_move = working.mfe
        peak_price = (
            working.peak_favorable_price
            if working.peak_favorable_price is not None
            else current_price
        )

        # ── 1. HARD RISK ────────────────────────────────────────────────────
        hard_stop = working.initial_stop
        if hard_stop is not None:
            breached = (adverse_price - hard_stop) * sign <= 0
            if breached:
                return self._exit(
                    working,
                    previous_phase,
                    previous_stop,
                    priority=ExitPriority.HARD_RISK,
                    reason=(
                        f"hard risk: price {adverse_price:.2f} breached initial stop "
                        f"{hard_stop:.2f}"
                    ),
                    hard_risk_reason="initial stop breached",
                    moment=moment,
                )
        # A ratcheted protective stop counts as risk too: once it is hit the trade is over.
        if (
            working.current_stop is not None
            and working.current_stop != working.initial_stop
            and (adverse_price - working.current_stop) * sign <= 0
        ):
            return self._exit(
                working,
                previous_phase,
                previous_stop,
                priority=ExitPriority.PROFIT_PROTECTION,
                reason=(
                    f"protective stop {working.current_stop:.2f} reached at {adverse_price:.2f}"
                ),
                moment=moment,
            )

        # ── 2. STRUCTURAL INVALIDATION ──────────────────────────────────────
        if structural_invalidation:
            return self._exit(
                working,
                previous_phase,
                previous_stop,
                priority=ExitPriority.STRUCTURAL_INVALIDATION,
                reason=f"structural invalidation: {structural_invalidation}",
                structural_reason=structural_invalidation,
                moment=moment,
            )

        # ── 3. PROFIT PROTECTION / TRAILING ─────────────────────────────────
        management = self.trade_manager.assess(
            direction=state.direction,
            entry_price=entry,
            current_price=current_price,
            peak_price=peak_price,
        )
        guardian = None
        proposed_stop: float | None = None
        action = ManagementAction.HOLD
        reason = "favourable move below activation; holding with initial risk"
        phase = working.phase

        if peak_move >= self.policy.activation_move:
            guardian = self.guardian.assess(
                direction=state.direction,
                entry_price=entry,
                current_price=current_price,
                peak_price=peak_price,
                market_health=market_health,
            )
            if guardian.action is ManagementAction.EXIT:
                return self._exit(
                    working,
                    previous_phase,
                    previous_stop,
                    priority=ExitPriority.PROFIT_PROTECTION,
                    reason=(
                        f"guardian emergency exit: giveback {guardian.giveback:.2f} with "
                        f"{len(guardian.warnings)} weakening conditions"
                    ),
                    management=management,
                    guardian=guardian,
                    moment=moment,
                )
            # The protection floor from Agent 14 (+5 reached => lock minimum_lock) and the
            # guardian's trail are both candidates; the ratchet keeps the tighter one.
            candidates = [
                value
                for value in (management.trail_price, guardian.trail_price)
                if value is not None
            ]
            if candidates:
                proposed_stop = max(candidates) if sign > 0 else min(candidates)
            action = guardian.action
            reason = (
                f"+{self.policy.activation_move:g} reached (peak {peak_move:.2f}); "
                f"guardian {guardian.action.value}"
            )
        elif management.action is ManagementAction.PROTECT and management.trail_price is not None:
            proposed_stop = management.trail_price
            action = ManagementAction.PROTECT
            reason = "; ".join(management.reason)
        elif peak_move > 0:
            phase = ManagementPhase.PROTECTION_PENDING if phase is ManagementPhase.OPEN else phase
            reason = (
                f"favourable {peak_move:.2f} below activation "
                f"{self.policy.activation_move:g}; protection pending"
            )

        new_stop = working.current_stop
        stop_changed = False
        if proposed_stop is not None:
            capped = self._executable(proposed_stop, current_price, sign)
            if new_stop is None or (capped - new_stop) * sign > 1e-9:
                new_stop = capped
                stop_changed = True

        extra: dict[str, Any] = {}
        if stop_changed:
            extra["current_stop"] = new_stop
            extra["trail_updates"] = working.trail_updates + 1
            if not working.protection_activated:
                extra["protection_activated"] = True
                extra["protection_activation_price"] = current_price
            if peak_move >= self.policy.activation_move and not working.trail_activated:
                extra["trail_activated"] = True
                extra["trail_activation_price"] = current_price
        if peak_move >= self.policy.activation_move and (
            working.trail_activated or extra.get("trail_activated")
        ):
            phase = ManagementPhase.TRAILING
        elif working.protection_activated or extra.get("protection_activated"):
            phase = ManagementPhase.PROTECTED if phase is not ManagementPhase.TRAILING else phase
        if phase is not working.phase:
            assert_management_transition(working.phase, phase)
            extra["phase"] = phase
        extra["last_action"] = action.value
        extra["last_reason"] = reason
        advanced = working.model_copy(update=extra)
        return advanced, ExitDecision(
            action=action,
            priority=ExitPriority.PROFIT_PROTECTION if stop_changed else ExitPriority.NONE,
            reason=reason,
            phase=advanced.phase,
            previous_phase=previous_phase,
            new_stop=new_stop if stop_changed else None,
            previous_stop=previous_stop,
            stop_changed=stop_changed,
            phase_changed=advanced.phase is not previous_phase,
            management=management,
            guardian=guardian,
            detail={"peak_move": peak_move, "current_move": current_move},
        )

    def close(
        self,
        state: TradeManagementState,
        *,
        exit_price: float,
        exit_time: datetime,
        exit_reason: str,
    ) -> TradeManagementState:
        """Record the broker-observed close. The exit experience is derived here, once."""
        if state.is_terminal:
            return state
        assert_management_transition(state.phase, ManagementPhase.CLOSED)
        realized = (exit_price - state.entry_price) * state.direction.sign
        given_back = max(0.0, state.mfe - realized)
        reason = state.exit_reason or exit_reason
        return state.model_copy(
            update={
                "phase": ManagementPhase.CLOSED,
                "exit_price": float(exit_price),
                "exit_time": to_utc(exit_time),
                "exit_reason": reason,
                "realized_move": realized,
                "profit_given_back_from_peak": given_back,
                "last_action": ManagementAction.EXIT.value,
                "updated_at": to_utc(exit_time),
            }
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _executable(self, stop: float, current_price: float, sign: int) -> float:
        """Never place a stop beyond (or on top of) the executable quote."""
        limit = current_price - sign * self.policy.executable_buffer
        return min(stop, limit) if sign > 0 else max(stop, limit)

    def _exit(
        self,
        working: TradeManagementState,
        previous_phase: ManagementPhase,
        previous_stop: float | None,
        *,
        priority: ExitPriority,
        reason: str,
        moment: datetime,
        management: TradeManagementDecision | None = None,
        guardian: GuardianDecision | None = None,
        hard_risk_reason: str | None = None,
        structural_reason: str | None = None,
    ) -> tuple[TradeManagementState, ExitDecision]:
        update: dict[str, Any] = {
            "last_action": ManagementAction.EXIT.value,
            "last_reason": reason,
            "exit_priority": priority,
            "exit_reason": reason,
        }
        if working.phase is not ManagementPhase.EXIT_PENDING:
            assert_management_transition(working.phase, ManagementPhase.EXIT_PENDING)
            update["phase"] = ManagementPhase.EXIT_PENDING
            update["exit_requested_at"] = moment
        if hard_risk_reason:
            update["hard_risk_reason"] = hard_risk_reason
        if structural_reason:
            update["structural_invalidation_reason"] = structural_reason
        advanced = working.model_copy(update=update)
        return advanced, ExitDecision(
            action=ManagementAction.EXIT,
            priority=priority,
            reason=reason,
            phase=advanced.phase,
            previous_phase=previous_phase,
            previous_stop=previous_stop,
            exit_requested=previous_phase is not ManagementPhase.EXIT_PENDING,
            phase_changed=advanced.phase is not previous_phase,
            management=management,
            guardian=guardian,
            detail={"peak_move": working.mfe, "current_move": working.current_move},
        )
