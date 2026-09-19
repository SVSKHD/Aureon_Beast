"""Deterministic identifiers (§12, §34).

Every id here is a pure function of its inputs. That is what makes the system
idempotent end to end:

* the same closed candle replayed through the same agent yields the same
  ``detection_id``, so the replay/live parity test can compare ids directly and
  the outbox can ``set()`` by doc id without ever creating a duplicate;
* a crashed executor can re-derive the ``comment_token`` it stamped on an order
  it is no longer sure it sent, which is the only way reconciliation can find
  that order again.

Nothing in this module may consult the clock, a random source, or config
defaults -- an id that varies between two runs over identical data would break
both properties above.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime

from aureon.models.base import to_utc

# Decision 3 (§12): ASCII unit separator. NOT "|", because event_key legitimately
# contains it -- the liquidity and breakout agents emit "{direction}|{level_type}".
# With "|" as the separator, ("up", "a|b") and ("up|a", "b") would hash alike.
SEP = "\x1f"

# Decision 5 (§34): "AUR:" + 6 base32 chars = 10 chars, well inside MT5's
# 31-character comment field.
COMMENT_PREFIX = "AUR:"
COMMENT_HASH_CHARS = 6


def _digest(*parts: str) -> bytes:
    """sha256 over parts joined by the unit separator."""
    return hashlib.sha256(SEP.join(parts).encode("utf-8")).digest()


def _canonical_timestamp(value: datetime) -> str:
    """A timestamp rendered so that two equal instants always render alike.

    Normalising to UTC first is essential: the same candle open expressed in
    Europe/Athens and in UTC is the same instant and must produce the same id.
    Microseconds are dropped because a candle open is second-precision, and a
    stray microsecond from a provider must not fork the id.
    """
    return to_utc(value).replace(microsecond=0).isoformat()


def detection_id_components(
    *,
    account_scope: str,
    symbol: str,
    timeframe: str,
    agent_name: str,
    event_key: str,
    candle_time: datetime,
) -> tuple[str, ...]:
    """The exact tuple hashed into a ``detection_id``, for debugging and tests.

    ``agent_version`` is deliberately NOT a component (decision 16): a patch
    release of an agent re-running over history must land on the SAME document,
    upserting it, rather than minting a parallel detection for a candle that only
    ever happened once. The version is still stamped on the document as a field,
    so which build observed it remains answerable.
    """
    return (
        account_scope,
        symbol,
        timeframe,
        agent_name,
        event_key,
        _canonical_timestamp(candle_time),
    )


def detection_id(
    *,
    account_scope: str,
    symbol: str,
    timeframe: str,
    agent_name: str,
    event_key: str,
    candle_time: datetime,
) -> str:
    """Stable id for a detection (§12).

    Keyed on the closed candle that produced it, so re-observing that candle --
    by replay, by restart recovery, or by a redelivery from the outbox -- is a
    no-op rather than a duplicate.
    """
    return _digest(
        *detection_id_components(
            account_scope=account_scope,
            symbol=symbol,
            timeframe=timeframe,
            agent_name=agent_name,
            event_key=event_key,
            candle_time=candle_time,
        )
    ).hex()


def comment_token(request_id: str) -> str:
    """The broker order comment that ties an order back to a request (§34).

    Decision 5: ``AUR:`` + 6 base32 chars of sha256(request_id). Deterministic on
    purpose -- an executor that crashed mid-send re-derives this token on restart
    and uses it to search the broker's order and deal history for the order it
    may or may not have placed. A random token would make that order
    unfindable, and an unfindable order is one a human has to reconcile by hand.

    Base32 avoids the case-folding and punctuation risks of base64 in a field
    that round-trips through a broker terminal.
    """
    if not request_id:
        raise ValueError("request_id must be non-empty to derive a comment token")
    encoded = base64.b32encode(_digest(request_id)).decode("ascii")
    return COMMENT_PREFIX + encoded[:COMMENT_HASH_CHARS]


def execution_attempt_id() -> str:
    """A fresh id for one send attempt (§32).

    Unlike everything else here this is intentionally random: two attempts on the
    same request must be distinguishable in the audit trail, which is exactly
    what tells an operator whether a retry ever happened.
    """
    return uuid.uuid4().hex


def evaluation_doc_id(detection_id_value: str, rule_id: str) -> str:
    """Doc id for ``detection_evaluations`` (Phase 3).

    One document per (detection, rule) pair, so a second rule can be evaluated
    over the same history without touching the first rule's frozen results.
    """
    if "__" in rule_id:
        raise ValueError(f"rule_id must not contain '__': {rule_id!r}")
    return f"{detection_id_value}__{rule_id}"
