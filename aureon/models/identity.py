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


#: The §12 component order, frozen. Named so a test can assert the tuple the hash
#: is built from rather than only that the hash is stable.
DETECTION_ID_COMPONENTS: tuple[str, ...] = (
    "account_scope",
    "symbol",
    "timeframe",
    "candle_close_utc",
    "agent_name",
    "agent_version",
    "event_key",
)


def detection_id_components(
    *,
    account_scope: str,
    symbol: str,
    timeframe: str,
    candle_close: datetime,
    agent_name: str,
    agent_version: str,
    event_key: str,
) -> tuple[str, ...]:
    """The exact tuple hashed into a ``detection_id`` (§12), for debugging and tests.

    Seven components, in this order, matching ``DETECTION_ID_COMPONENTS``.

    Two of them deserve a note, because both were different before and the change
    is visible in every stored id:

    * **``agent_version`` IS a component.** §12 makes it part of the frozen
      contract, which reverses decision 16. The consequence is deliberate and
      worth stating plainly: bumping an agent's version no longer upserts the same
      document, it mints a parallel detection for the same candle. That is what
      lets ``ema_cross`` 1.0.0 (9/21) and 2.0.0 (20/50) describe the same week
      side by side instead of overwriting each other -- but it also means a pure
      bug-fix release re-run over history leaves the old detections behind rather
      than correcting them. A version bump is now a fork of history, not an edit
      of it.
    * **The timestamp is the candle CLOSE, not its open.** A detection becomes
      known at the close, and §12 keys it on the instant it became knowable.
    """
    return (
        account_scope,
        symbol,
        timeframe,
        _canonical_timestamp(candle_close),
        agent_name,
        agent_version,
        event_key,
    )


def detection_id(
    *,
    account_scope: str,
    symbol: str,
    timeframe: str,
    candle_close: datetime,
    agent_name: str,
    agent_version: str,
    event_key: str,
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
            candle_close=candle_close,
            agent_name=agent_name,
            agent_version=agent_version,
            event_key=event_key,
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


def new_alert_id() -> str:
    """A fresh ``/remind`` alert id (9C).

    Random like ``execution_attempt_id``, because an alert is a request rather than an
    observation: two alerts on the same level are two different things a human asked for, and
    a deterministic id would collapse them into one. Short enough to type into
    ``/remind cancel id:``, which is the only place a human ever writes one.
    """
    return f"al-{uuid.uuid4().hex[:10]}"


def new_assessment_id() -> str:
    """A fresh ``/monitor`` assessment id (9D).

    Random rather than derived from the detection: the SAME detection can be assessed twice
    -- an hour apart, with an hour more history in the cohort -- and those are two different
    statements about two different evidence bases. A deterministic id would overwrite the
    first with the second, which is exactly the record the weekly review needs to score.
    """
    return f"as-{uuid.uuid4().hex[:12]}"


def new_note_id() -> str:
    """A fresh trade-note id (9D). Random, because two notes on one trade are two notes."""
    return f"nt-{uuid.uuid4().hex[:12]}"


def evaluation_doc_id(detection_id_value: str, rule_id: str) -> str:
    """Doc id for ``detection_evaluations`` (Phase 3).

    One document per (detection, rule) pair, so a second rule can be evaluated
    over the same history without touching the first rule's frozen results.
    """
    if "__" in rule_id:
        raise ValueError(f"rule_id must not contain '__': {rule_id!r}")
    return f"{detection_id_value}__{rule_id}"
