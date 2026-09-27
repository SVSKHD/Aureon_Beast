"""Deterministic live exit manager (GAP 2): +5 protection, ratchet, priority, experience."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.management.exit_manager import DeterministicExitManager, ExitPolicy
from aureon.models.agent_decision import ManagementAction
from aureon.models.enums import Direction, ExitPriority, ManagementPhase, TransitionError

T0 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
HEALTHY = {"ema": True, "htf": True, "session": True}


def _run(manager, state, prices, *, health=HEALTHY, **kwargs):
    decisions = []
    for index, price in enumerate(prices):
        state, decision = manager.assess(
            state,
            current_price=price,
            market_health=health,
            now=T0 + timedelta(minutes=5 * index),
            **kwargs,
        )
        decisions.append(decision)
    return state, decisions


def test_five_dollar_move_activates_configured_protection_without_closing() -> None:
    manager = DeterministicExitManager(ExitPolicy())
    state = manager.open(entry_price=100.0, direction=Direction.BUY, initial_stop=92.0, now=T0)
    state, decisions = _run(manager, state, [101.0, 103.0, 105.2])
    assert state.reached_5 is True
    assert state.protection_activated is True
    assert state.phase in {ManagementPhase.PROTECTED, ManagementPhase.TRAILING}
    assert state.current_stop is not None and state.current_stop >= 104.0 - 1e-9
    assert decisions[-1].action is not ManagementAction.EXIT
    assert state.exit_reason is None


def test_strong_winner_stays_open_through_ten_twenty_thirty_forty() -> None:
    manager = DeterministicExitManager()
    state = manager.open(entry_price=100.0, direction=Direction.BUY, initial_stop=92.0, now=T0)
    state, decisions = _run(manager, state, [104.0, 106.0, 111.0, 121.0, 131.0, 141.0])
    assert all(d.action is not ManagementAction.EXIT for d in decisions)
    assert state.phase is ManagementPhase.TRAILING
    assert (
        state.reached_5,
        state.reached_10,
        state.reached_20,
        state.reached_30,
        state.reached_40,
    ) == (True, True, True, True, True)
    assert state.mfe == 41.0
    assert state.peak_favorable_price == 141.0


def test_trailing_progresses_with_the_peak_and_counts_updates() -> None:
    manager = DeterministicExitManager()
    state = manager.open(entry_price=100.0, direction=Direction.SELL, initial_stop=108.0, now=T0)
    state, _ = _run(manager, state, [94.0, 90.0, 85.0])
    assert state.trail_activated is True
    assert state.trail_updates >= 3
    assert state.current_stop is not None and state.current_stop < 100.0
    assert state.trail_activation_price == 94.0


def test_the_stop_never_loosens_on_a_pullback() -> None:
    manager = DeterministicExitManager()
    state = manager.open(entry_price=100.0, direction=Direction.BUY, initial_stop=92.0, now=T0)
    state, _ = _run(manager, state, [108.0, 112.0])
    best = state.current_stop
    state, decisions = _run(manager, state, [110.0, 109.0, 108.5])
    assert state.current_stop == best
    assert all(not d.stop_changed for d in decisions)
    assert state.phase is ManagementPhase.TRAILING


def test_hard_risk_overrides_any_trailing_decision() -> None:
    manager = DeterministicExitManager()
    state = manager.open(entry_price=100.0, direction=Direction.BUY, initial_stop=95.0, now=T0)
    state, _ = _run(manager, state, [106.0])
    # A gap through the initial stop while the guardian would still say TRAIL.
    state, decision = manager.assess(
        state, current_price=106.0, low=94.5, market_health=HEALTHY, now=T0 + timedelta(hours=1)
    )
    assert decision.action is ManagementAction.EXIT
    assert decision.priority is ExitPriority.HARD_RISK
    assert state.phase is ManagementPhase.EXIT_PENDING
    assert state.hard_risk_reason == "initial stop breached"


def test_structural_invalidation_exits_before_profit_protection() -> None:
    manager = DeterministicExitManager()
    state = manager.open(entry_price=100.0, direction=Direction.BUY, initial_stop=92.0, now=T0)
    state, _ = _run(manager, state, [106.0])
    state, decision = manager.assess(
        state,
        current_price=107.0,
        market_health=HEALTHY,
        structural_invalidation="opposite EMA cross with session trend down",
        now=T0 + timedelta(hours=1),
    )
    assert decision.priority is ExitPriority.STRUCTURAL_INVALIDATION
    assert state.structural_invalidation_reason == "opposite EMA cross with session trend down"
    assert state.phase is ManagementPhase.EXIT_PENDING


def test_exit_experience_is_persisted_on_close() -> None:
    manager = DeterministicExitManager()
    state = manager.open(entry_price=100.0, direction=Direction.BUY, initial_stop=92.0, now=T0)
    state, _ = _run(manager, state, [106.0, 118.0, 112.0])
    closed = manager.close(
        state, exit_price=111.0, exit_time=T0 + timedelta(hours=2), exit_reason="sl"
    )
    assert closed.phase is ManagementPhase.CLOSED
    assert closed.exit_price == 111.0
    assert closed.exit_time == T0 + timedelta(hours=2)
    assert closed.exit_reason  # the manager's own reason wins when it decided, else broker's
    assert closed.realized_move == 11.0
    assert closed.profit_given_back_from_peak == 7.0
    assert closed.mfe == 18.0
    assert closed.reached_10 is True and closed.reached_20 is False
    # A closed state is inert.
    again, decision = manager.assess(closed, current_price=150.0, now=T0 + timedelta(hours=3))
    assert again == closed and decision.action is ManagementAction.HOLD


def test_close_is_a_legal_transition_from_every_live_phase() -> None:
    manager = DeterministicExitManager()
    for prices in ([], [102.0], [106.0], [106.0, 112.0]):
        state = manager.open(entry_price=100.0, direction=Direction.BUY, initial_stop=92.0, now=T0)
        state, _ = _run(manager, state, prices)
        closed = manager.close(state, exit_price=100.0, exit_time=T0, exit_reason="manual")
        assert closed.phase is ManagementPhase.CLOSED
    with pytest.raises(TransitionError):
        from aureon.models.enums import assert_management_transition

        assert_management_transition(ManagementPhase.CLOSED, ManagementPhase.OPEN)


def test_policy_is_configurable_and_validated() -> None:
    policy = ExitPolicy(activation_move=8.0, minimum_lock=6.0, trail_fraction_of_peak=0.6)
    manager = DeterministicExitManager(policy)
    state = manager.open(entry_price=100.0, direction=Direction.BUY, initial_stop=90.0, now=T0)
    state, _ = _run(manager, state, [106.0])
    assert state.protection_activated is False  # +6 < activation 8
    state, _ = _run(manager, state, [108.5])
    assert state.protection_activated is True
    assert state.policy["activation_move"] == 8.0
    with pytest.raises(ValueError):
        ExitPolicy(minimum_lock=5.0, activation_move=5.0)
    env = {"AUREON_EXIT_ACTIVATION_MOVE": "7", "AUREON_EXIT_MINIMUM_LOCK": "3"}
    assert ExitPolicy.from_env(env).activation_move == 7.0


def test_hard_stop_move_supplies_a_risk_line_when_the_broker_stop_is_absent() -> None:
    manager = DeterministicExitManager(ExitPolicy(hard_stop_move=6.0))
    state = manager.open(entry_price=100.0, direction=Direction.SELL, initial_stop=None, now=T0)
    assert state.initial_stop == 106.0
    state, decision = manager.assess(state, current_price=106.5, now=T0)
    assert decision.priority is ExitPriority.HARD_RISK
