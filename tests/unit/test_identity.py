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
    SEP,
    comment_token,
    detection_id,
    evaluation_doc_id,
    execution_attempt_id,
)

CANDLE = datetime(2026, 9, 18, 13, 5, tzinfo=UTC)
BASE = dict(
    account_scope="primary",
    symbol="XAUUSD",
    timeframe="M5",
    agent_name="ema_cross",
    event_key="bullish",
    candle_time=CANDLE,
)


def test_same_inputs_give_the_same_id() -> None:
    """The property replay/live parity depends on (§82)."""
    assert detection_id(**BASE) == detection_id(**BASE)


def test_id_is_independent_of_the_timezone_the_candle_arrives_in() -> None:
    """The same instant in Athens and in UTC is the same candle.

    A provider that reports server-local time must not produce a different id
    from one reporting UTC, or replay would never match live.
    """
    athens = dict(BASE, candle_time=CANDLE.astimezone(ZoneInfo("Europe/Athens")))
    assert detection_id(**athens) == detection_id(**BASE)


def test_sub_second_noise_does_not_fork_the_id() -> None:
    """A candle open is second-precision; a stray microsecond is provider noise."""
    noisy = dict(BASE, candle_time=CANDLE.replace(microsecond=7))
    assert detection_id(**noisy) == detection_id(**BASE)


def test_a_naive_candle_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        detection_id(**dict(BASE, candle_time=CANDLE.replace(tzinfo=None)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_scope", "live"),
        ("symbol", "EURUSD"),
        ("timeframe", "M15"),
        ("agent_name", "liquidity"),
        ("event_key", "bearish"),
        ("candle_time", CANDLE + timedelta(minutes=5)),
    ],
)
def test_every_component_changes_the_id(field: str, value: object) -> None:
    assert detection_id(**dict(BASE, **{field: value})) != detection_id(**BASE)


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
