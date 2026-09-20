"""Deterministic identifiers (§12, §34, decisions 3 and 5).

These properties are what make replay/live parity and crash reconciliation
possible, so they are tested as properties rather than against golden strings --
a golden string would pin the hash without pinning the reasoning.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from aureon.models.identity import (
    COMMENT_PREFIX,
    DETECTION_ID_COMPONENTS,
    SEP,
    comment_token,
    detection_id,
    detection_id_components,
    evaluation_doc_id,
    execution_attempt_id,
)

CANDLE = datetime(2026, 9, 18, 13, 5, tzinfo=UTC)
BASE = dict(
    account_scope="primary",
    symbol="XAUUSD",
    timeframe="M5",
    candle_close=CANDLE,
    agent_name="ema_cross",
    agent_version="2.0.0",
    event_key="bullish",
)


def test_same_inputs_give_the_same_id() -> None:
    """The property replay/live parity depends on (§82)."""
    assert detection_id(**BASE) == detection_id(**BASE)


def test_id_is_independent_of_the_timezone_the_candle_arrives_in() -> None:
    """The same instant in Athens and in UTC is the same candle.

    A provider that reports server-local time must not produce a different id
    from one reporting UTC, or replay would never match live.
    """
    athens = dict(BASE, candle_close=CANDLE.astimezone(ZoneInfo("Europe/Athens")))
    assert detection_id(**athens) == detection_id(**BASE)


def test_sub_second_noise_does_not_fork_the_id() -> None:
    """A candle open is second-precision; a stray microsecond is provider noise."""
    noisy = dict(BASE, candle_close=CANDLE.replace(microsecond=7))
    assert detection_id(**noisy) == detection_id(**BASE)


def test_a_naive_candle_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        detection_id(**dict(BASE, candle_close=CANDLE.replace(tzinfo=None)))


#: One distinct value per §12 component. Keyed by component name so the coverage
#: test below can prove the list is exhaustive rather than merely long.
COMPONENT_VARIANTS: dict[str, object] = {
    "account_scope": "live",
    "symbol": "EURUSD",
    "timeframe": "M15",
    "candle_close": CANDLE + timedelta(minutes=5),
    "agent_name": "liquidity",
    "agent_version": "1.0.0",
    "event_key": "bearish",
}


@pytest.mark.parametrize("field", sorted(COMPONENT_VARIANTS))
def test_every_component_changes_the_id(field: str) -> None:
    """Changing any one component must change the id.

    This is the half that catches a component being IGNORED. The half that catches a
    component being REMOVED is the signature test below: dropping one from the
    function makes the call raise, and dropping it from the hash while keeping the
    parameter makes this test fail for that field.
    """
    variant = dict(BASE, **{field: COMPONENT_VARIANTS[field]})
    assert detection_id(**variant) != detection_id(**BASE), (
        f"{field} is accepted but does not reach the hash"
    )


def test_the_component_list_is_exactly_the_frozen_contract() -> None:
    """§12 freezes both the set of components and their order.

    Pinned as a list rather than a golden hash so a reviewer can see which contract
    changed, and so adding a component fails here -- with the reason -- instead of
    silently re-keying every detection in the database.
    """
    assert DETECTION_ID_COMPONENTS == (
        "account_scope",
        "symbol",
        "timeframe",
        "candle_close_utc",
        "agent_name",
        "agent_version",
        "event_key",
    )
    # The parametrized test above must cover every one of them. Without this, a
    # component added to the contract could go untested indefinitely. The only
    # contract name that differs from its keyword is candle_close_utc/candle_close.
    kwarg_for = {name: name.removesuffix("_utc") for name in DETECTION_ID_COMPONENTS}
    assert set(COMPONENT_VARIANTS) == {kwarg_for[n] for n in DETECTION_ID_COMPONENTS}


def test_removing_any_component_is_a_hard_failure() -> None:
    """Omitting a component must raise, never silently hash a shorter tuple.

    Every parameter is keyword-only and required, so this is enforced by the
    signature. Asserted anyway: a later refactor giving one of them a default would
    make two different market events share a document, and nothing else in the
    suite would notice.
    """
    for field in COMPONENT_VARIANTS:
        partial = {k: v for k, v in BASE.items() if k != field}
        with pytest.raises(TypeError, match=field):
            detection_id(**partial)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=field):
            detection_id_components(**partial)  # type: ignore[arg-type]


def test_the_hashed_tuple_is_the_components_in_contract_order() -> None:
    """The tuple actually fed to sha256, in the order §12 fixes."""
    assert detection_id_components(**BASE) == (
        "primary",
        "XAUUSD",
        "M5",
        "2026-09-18T13:05:00+00:00",
        "ema_cross",
        "2.0.0",
        "bullish",
    )


def test_an_agent_version_bump_forks_rather_than_upserts() -> None:
    """The consequence of §12 including agent_version, stated as a test.

    Worth pinning explicitly because it reverses decision 16 and is easy to
    misremember: the same candle observed by two agent versions is now TWO
    documents. That is what lets 9/21 and 20/50 coexist, and it is also why a
    version bump must never be used to correct history in place.
    """
    v1 = detection_id(**dict(BASE, agent_version="1.0.0"))
    v2 = detection_id(**dict(BASE, agent_version="2.0.0"))
    assert v1 != v2


def test_separator_is_not_the_pipe_that_event_keys_contain() -> None:
    """Decision 3: event_key legitimately contains '|'.

    The liquidity and breakout agents emit "{direction}|{level_type}". With '|' as
    the separator, an agent/event pair could collide with a different pair whose
    text happened to split the other way -- two distinct market events sharing one
    document.
    """
    assert SEP == "\x1f"
    left = detection_id(**dict(BASE, agent_name="liq", event_key="up|previous_day_high"))
    right = detection_id(**dict(BASE, agent_name="liq|up", event_key="previous_day_high"))
    assert left != right


def test_comment_token_shape_matches_decision_5() -> None:
    """'AUR:' + 6 base32 chars = 10 chars, inside MT5's 31-char comment field."""
    token = comment_token("request-abc-123")
    assert token.startswith(COMMENT_PREFIX)
    assert len(token) == 10
    assert len(token) <= 31
    assert token[len(COMMENT_PREFIX) :].isalnum()


def test_comment_token_is_re_derivable_after_a_crash() -> None:
    """The property reconciliation depends on (§34).

    An executor that crashed mid-send must recompute the exact token it stamped,
    or the order it may have placed is unfindable and needs manual repair.
    """
    assert comment_token("r1") == comment_token("r1")
    assert comment_token("r1") != comment_token("r2")


def test_comment_token_requires_a_request_id() -> None:
    with pytest.raises(ValueError):
        comment_token("")


def test_attempt_ids_are_unique_per_attempt() -> None:
    """Deliberately random: two attempts must be distinguishable in the audit."""
    assert execution_attempt_id() != execution_attempt_id()


def test_evaluation_doc_id_keeps_rules_separate() -> None:
    did = detection_id(**BASE)
    assert evaluation_doc_id(did, "EMA_OUTCOME_V1") == f"{did}__EMA_OUTCOME_V1"
    assert evaluation_doc_id(did, "EMA_OUTCOME_V2") != evaluation_doc_id(did, "EMA_OUTCOME_V1")


def test_evaluation_doc_id_rejects_an_ambiguous_rule_id() -> None:
    """'__' is the separator, so a rule id containing it would be ambiguous."""
    with pytest.raises(ValueError, match="__"):
        evaluation_doc_id("d1", "RULE__V1")
