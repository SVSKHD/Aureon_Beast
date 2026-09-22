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


# ── C-1: RECONCILING ──────────────────────────────────────────────────────────


def test_an_uncertain_send_goes_to_reconciling_rather_than_failed() -> None:
    """C-1, and the reason there is exactly one new status.

    When ``order_send`` raises, times out, or the lease expires while EXECUTING, nobody
    knows whether an order exists. ``FAILED`` is a CLAIM that none does -- and a failed
    request invites a resend, which is how one intent becomes two positions. So the
    uncertain outcome gets its own state, and the answer comes from MT5.
    """
    assert_trade_request_transition(S.EXECUTING, S.RECONCILING)


def test_reconciling_can_reach_every_real_outcome() -> None:
    """Reconciliation against MT5 can find a fill, a partial, a resting order -- or fail."""
    for outcome in (S.FILLED, S.PARTIALLY_FILLED, S.PENDING, S.FAILED_RECONCILIATION):
        assert_trade_request_transition(S.RECONCILING, outcome)


def test_reconciling_can_never_become_a_plain_failure() -> None:
    """The distinction the whole state exists to keep.

    By this point a send has been ATTEMPTED and its result is unknown. ``FAILED`` asserts
    that no order exists, which is exactly what nobody knows; ``FAILED_RECONCILIATION`` is
    the honest terminal for "MT5 could not tell us", and it is terminal precisely so that
    no automatic path leads from it back to a resend.
    """
    with pytest.raises(TransitionError):
        assert_trade_request_transition(S.RECONCILING, S.FAILED)


def test_nothing_enters_reconciling_except_an_attempted_send() -> None:
    """A request that was never sent has a knowable outcome, so it must not land here.

    ``REQUESTED`` and ``CONFIRMED`` have not reached the broker: if one of those could
    become RECONCILING, an operator would be asked to reconcile against MT5 for an order
    that was never placed, and finding nothing would look like a lost fill.
    """
    for before in (S.REQUESTED, S.CONFIRMED, S.PENDING, S.PARTIALLY_FILLED):
        with pytest.raises(TransitionError):
            assert_trade_request_transition(before, S.RECONCILING)


def test_reconciling_is_not_terminal() -> None:
    """It is a state something is DONE about, not a resting place.

    Derived from the table rather than listed, so this also proves the terminal set stayed
    correct when the status was added.
    """
    assert S.RECONCILING not in TERMINAL_REQUEST_STATUSES
