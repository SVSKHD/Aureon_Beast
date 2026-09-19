"""The state machine (§25, decision 1).

Every status write in the system passes through these functions, so the table is
tested directly rather than only through the repositories that use it.
"""

from __future__ import annotations

import pytest

from aureon.models.enums import (
    TERMINAL_REQUEST_STATUSES,
    TRADE_REQUEST_TRANSITIONS,
    ControlRequestStatus,
    HorizonStatus,
    TradeRequestStatus,
    TradeStatus,
    TransitionError,
    assert_control_request_transition,
    assert_horizon_transition,
    assert_trade_request_transition,
    assert_trade_transition,
)

S = TradeRequestStatus


def test_nothing_returns_to_requested_or_confirmed() -> None:
    """Decision 1: a confirmation is a one-way door.

    Re-confirming is a repository no-op returning the current document, never a
    transition back. If an edge back to CONFIRMED existed, a second confirm could
    re-arm an already-executed request.
    """
    offenders = [
        (current, target)
        for current, targets in TRADE_REQUEST_TRANSITIONS.items()
        for target in targets
        if target in {S.REQUESTED, S.CONFIRMED} and current is not S.REQUESTED
    ]
    assert offenders == []


def test_requested_may_only_advance_to_confirmed_cancelled_or_expired() -> None:
    assert TRADE_REQUEST_TRANSITIONS[S.REQUESTED] == frozenset(
        {S.CONFIRMED, S.CANCELLED, S.EXPIRED}
    )


def test_execution_never_skips_confirmation() -> None:
    """Execution only from CONFIRMED (CLAUDE.md non-negotiable)."""
    with pytest.raises(TransitionError):
        assert_trade_request_transition(S.REQUESTED, S.EXECUTING)


def test_confirmed_may_be_cancelled_or_go_stale() -> None:
    assert_trade_request_transition(S.CONFIRMED, S.EXECUTING)
    assert_trade_request_transition(S.CONFIRMED, S.CANCELLED)
    assert_trade_request_transition(S.CONFIRMED, S.FAILED_STALE)


@pytest.mark.parametrize(
    "target",
    [S.FILLED, S.PARTIALLY_FILLED, S.PENDING, S.FAILED, S.FAILED_RECONCILIATION],
)
def test_executing_resolves_to_every_expected_outcome(target: S) -> None:
    assert_trade_request_transition(S.EXECUTING, target)


def test_partially_filled_is_re_entrant() -> None:
    """Volume can arrive across several deals, each a fresh partial fill."""
    assert_trade_request_transition(S.PARTIALLY_FILLED, S.PARTIALLY_FILLED)
    assert_trade_request_transition(S.PARTIALLY_FILLED, S.FILLED)


@pytest.mark.parametrize("status", sorted(TERMINAL_REQUEST_STATUSES, key=str))
def test_terminal_statuses_have_no_exit(status: S) -> None:
    with pytest.raises(TransitionError, match="terminal"):
        assert_trade_request_transition(status, S.EXECUTING)


def test_failed_reconciliation_is_terminal() -> None:
    """Decision 1: manual repair is an operator action outside the machine."""
    assert S.FAILED_RECONCILIATION in TERMINAL_REQUEST_STATUSES


def test_terminal_set_is_exactly_the_expected_six() -> None:
    assert {s.value for s in TERMINAL_REQUEST_STATUSES} == {
        "filled",
        "cancelled",
        "expired",
        "failed",
        "failed_stale",
        "failed_reconciliation",
    }


def test_trade_partial_closes_are_re_entrant_and_closed_is_final() -> None:
    assert_trade_transition(TradeStatus.OPEN, TradeStatus.PARTIALLY_CLOSED)
    assert_trade_transition(TradeStatus.PARTIALLY_CLOSED, TradeStatus.PARTIALLY_CLOSED)
    assert_trade_transition(TradeStatus.PARTIALLY_CLOSED, TradeStatus.CLOSED)
    with pytest.raises(TransitionError):
        assert_trade_transition(TradeStatus.CLOSED, TradeStatus.OPEN)


def test_a_complete_horizon_can_never_reopen() -> None:
    """A COMPLETE horizon is frozen, so re-running the tracker is idempotent."""
    assert_horizon_transition(HorizonStatus.PENDING, HorizonStatus.COMPLETE)
    assert_horizon_transition(HorizonStatus.PENDING, HorizonStatus.INVALID)
    with pytest.raises(TransitionError):
        assert_horizon_transition(HorizonStatus.COMPLETE, HorizonStatus.PENDING)
    with pytest.raises(TransitionError):
        assert_horizon_transition(HorizonStatus.INVALID, HorizonStatus.COMPLETE)


def test_control_requests_follow_the_claim_pattern() -> None:
    assert_control_request_transition(
        ControlRequestStatus.REQUESTED, ControlRequestStatus.EXECUTING
    )
    assert_control_request_transition(
        ControlRequestStatus.EXECUTING, ControlRequestStatus.COMPLETED
    )
    with pytest.raises(TransitionError):
        assert_control_request_transition(
            ControlRequestStatus.COMPLETED, ControlRequestStatus.EXECUTING
        )


def test_unknown_status_is_rejected_rather_than_ignored() -> None:
    with pytest.raises(TransitionError, match="unknown current status"):
        assert_trade_request_transition("not_a_status", S.FILLED)  # type: ignore[arg-type]


def test_every_status_appears_in_the_table() -> None:
    """A status absent from the table would raise 'unknown' at runtime."""
    assert set(TRADE_REQUEST_TRANSITIONS) == set(TradeRequestStatus)
