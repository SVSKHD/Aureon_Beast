"""Firestore collection and document paths.

The only place in the system that knows what anything is called. Repositories
build every reference through these helpers so a collection can be renamed in one
place, and so a typo'd collection name fails here rather than silently creating a
second, empty collection that reads as "no data".

Reflects three Phase 1 decisions:

* **decision 9** -- there is no ``pending_orders`` collection. A pending order is
  the ``PENDING`` trade request; a second collection would be a second source of
  truth to keep in step.
* **decision 10** -- ``heartbeats/{service}`` is the source of truth for
  liveness, with a derived copy embedded in ``system_state``.
* **decision 11** -- ``settings/execution`` holds the runtime gates that override
  the environment.

Tick data never appears here, by design (CLAUDE.md): it is far too voluminous for
Firestore and is never needed after the candle closes.
"""

from __future__ import annotations

import os

from aureon.models.identity import evaluation_doc_id

# ── The prefix ────────────────────────────────────────────────────────────────

DEFAULT_COLLECTION_PREFIX = "aureon_beast"


def collection_prefix() -> str:
    """``AUREON_COLLECTION_PREFIX``, read at import time.

    Read from the environment here rather than threaded through every repository, because
    a prefix that varied between two repositories in one process would split the database
    in half silently -- the observer writing detections one place and the review reading
    another, both reporting success.
    """
    return os.environ.get("AUREON_COLLECTION_PREFIX", DEFAULT_COLLECTION_PREFIX).strip(
        "_"
    ) or DEFAULT_COLLECTION_PREFIX


def collection(name: str) -> str:
    """``{prefix}_{name}``. The ONLY place a collection name is built (§83).

    Every constant below goes through this, so one environment variable moves a whole
    deployment's data and a boundary test can forbid a bare collection literal anywhere
    else. That matters because a bare literal is invisible: it does not fail, it reads and
    writes a second, empty collection that looks exactly like "no data yet".
    """
    if not name:
        raise ValueError("collection name must not be empty")
    if "/" in name:
        raise ValueError(f"collection name must not contain '/': {name!r}")
    return f"{PREFIX}_{name}"


PREFIX = collection_prefix()

# ── Collections ───────────────────────────────────────────────────────────────
# The constants keep their names and hold PREFIXED values, so every existing caller is
# already correct and nothing has to remember to prefix at the call site.
DETECTIONS = collection("detections")
DETECTION_EVALUATIONS = collection("detection_evaluations")
SESSIONS = collection("sessions")
TRADE_REQUESTS = collection("trade_requests")
TRADES = collection("trades")
CONTROL_REQUESTS = collection("control_requests")
AUDIT_LOGS = collection("audit_logs")
HEARTBEATS = collection("heartbeats")
SYSTEM_STATE = collection("system_state")
SETTINGS = collection("settings")
SYMBOL_SPECS = collection("symbol_specs")
DAILY_REVIEWS = collection("daily_reviews")
WEEKLY_REVIEWS = collection("weekly_reviews")
# 9C: what Discord has already said, and what a human asked to be told.
NOTIFICATIONS = collection("notifications")
ALERTS = collection("alerts")
ASSESSMENTS = collection("assessments")
TRADE_NOTES = collection("trade_notes")
# 11A F-15: named operational conditions, and whether each is currently true.
OPS_EVENTS = collection("ops_events")

def _all_collections() -> tuple[str, ...]:
    """Every prefixed collection constant in this module, found rather than listed (11A, F-10).

    The hand-maintained tuple this replaces had drifted twice within one phase:
    ``assessments`` and ``trade_notes`` were added in 9D and never listed, so every consumer
    of the registry -- the contracts table, the emulator's cleanup, the docs check -- silently
    did not know about two collections that were being written to in production shape.

    A registry that has to be updated by hand will drift, and the drift is invisible: nothing
    fails, the missing collection simply is not considered. So it is derived from the module's
    own namespace, discriminating on the PREFIX rather than on naming convention -- doc-id
    constants like ``LEGACY_SYSTEM_STATE_DOC`` are upper-case strings too, and they are not
    collections.

    Sorted, so the contracts file and every table built from this are stable across runs.
    ``tests/unit/test_paths.py`` re-derives the same set by parsing the SOURCE for
    ``= collection(...)``, which is an independent derivation: a constant defined AFTER this
    function is called would be missed here and caught there.
    """
    found = {
        value
        for name, value in globals().items()
        if name.isupper() and isinstance(value, str) and value.startswith(f"{PREFIX}_")
    }
    return tuple(sorted(found))

# ── Fixed document ids ────────────────────────────────────────────────────────
#: The whole-system document id, used until Phase 9A split ``system_state`` per symbol.
#: Kept named so the read path can SKIP it: a deployment that ran before the split still
#: has one, and merging it back in would report every symbol twice -- once live, once
#: frozen at whenever the split happened.
LEGACY_SYSTEM_STATE_DOC = "current"
EXECUTION_SETTINGS_DOC = "execution"
#: ``settings/notifications`` -- which agents Discord announces (9C).
NOTIFICATION_SETTINGS_DOC = "notifications"

# Service names used as heartbeat document ids (§67).
SERVICE_OBSERVER = "observer"
SERVICE_EXECUTOR = "executor"
SERVICE_MONITOR = "monitor"
SERVICE_DISCORD = "discord"
SERVICES: tuple[str, ...] = (
    SERVICE_OBSERVER,
    SERVICE_EXECUTOR,
    SERVICE_MONITOR,
    SERVICE_DISCORD,
)


def _require(value: str, what: str) -> str:
    """Reject an empty or slash-bearing id.

    An empty id makes Firestore auto-generate one, which would silently create a
    duplicate of a document meant to be idempotent -- exactly the failure the
    deterministic ids exist to prevent. A slash would forge a nested path.
    """
    if not value:
        raise ValueError(f"{what} must not be empty")
    if "/" in value:
        raise ValueError(f"{what} must not contain '/': {value!r}")
    return value


def detection_path(detection_id: str) -> str:
    return f"{DETECTIONS}/{_require(detection_id, 'detection_id')}"


def detection_evaluation_path(detection_id: str, rule_id: str) -> str:
    """``detection_evaluations/{detection_id}__{rule_id}`` (Phase 3)."""
    return (
        f"{DETECTION_EVALUATIONS}/"
        f"{evaluation_doc_id(_require(detection_id, 'detection_id'), _require(rule_id, 'rule_id'))}"
    )


def session_doc_id(market_date: str, session: str) -> str:
    return f"{_require(market_date, 'market_date')}__{_require(session, 'session')}"


def session_path(market_date: str, session: str) -> str:
    """One document per (broker day, session) (§18)."""
    return f"{SESSIONS}/{session_doc_id(market_date, session)}"


def trade_request_path(request_id: str) -> str:
    return f"{TRADE_REQUESTS}/{_require(request_id, 'request_id')}"


def trade_path(trade_id: str) -> str:
    return f"{TRADES}/{_require(trade_id, 'trade_id')}"


def control_request_path(control_id: str) -> str:
    return f"{CONTROL_REQUESTS}/{_require(control_id, 'control_id')}"


def audit_path(audit_id: str) -> str:
    return f"{AUDIT_LOGS}/{_require(audit_id, 'audit_id')}"


def heartbeat_path(service: str) -> str:
    """``heartbeats/{service}`` -- the source of truth for liveness (decision 10)."""
    return f"{HEARTBEATS}/{_require(service, 'service')}"


def system_state_doc_id(symbol: str, timeframe: object) -> str:
    """``XAUUSD_M5``. One document per symbol and timeframe (9A).

    Per symbol rather than one document for everything, for two reasons that only appear
    with a second symbol: the write throttle is per document, so gold's candle close would
    otherwise suppress silver's state for the next few seconds, and a reader that wants one
    symbol should not have to read every symbol to get it.
    """
    frame = getattr(timeframe, "value", timeframe)
    return f"{_require(symbol, 'symbol')}_{_require(str(frame), 'timeframe')}"


def system_state_path(symbol: str, timeframe: object) -> str:
    return f"{SYSTEM_STATE}/{system_state_doc_id(symbol, timeframe)}"


def execution_settings_path() -> str:
    """``settings/execution`` -- runtime gates, overriding env (decision 11)."""
    return f"{SETTINGS}/{EXECUTION_SETTINGS_DOC}"


def symbol_spec_path(symbol: str) -> str:
    """``symbol_specs/{symbol}`` -- broker metadata published for Discord (decision 79).

    A convenience copy, never an authority: the execution guard re-reads the live symbol
    at execution time.
    """
    return f"{SYMBOL_SPECS}/{_require(symbol, 'symbol')}"


def notification_settings_path() -> str:
    """``settings/notifications`` (9C)."""
    return f"{SETTINGS}/{NOTIFICATION_SETTINGS_DOC}"


def notification_id(kind: str, ref_id: str) -> str:
    """``{kind}__{ref_id}`` -- the id that makes a send exactly-once (9C).

    Derived from what the message is ABOUT rather than auto-generated, so a bot that dies
    between posting and recording, then restarts, finds the document instead of posting
    again. The two parts are joined by a double underscore because a detection_id is a hex
    digest and an alert id is ours: neither contains one, so the id cannot be ambiguous.
    """
    return f"{_require(kind, 'kind')}__{_require(ref_id, 'ref_id')}"


def notification_path(kind: str, ref_id: str) -> str:
    return f"{NOTIFICATIONS}/{notification_id(kind, ref_id)}"


def alert_path(alert_id: str) -> str:
    return f"{ALERTS}/{_require(alert_id, 'alert_id')}"


def ops_event_id(name: str, scope: str | None = None) -> str:
    """``{name}`` or ``{name}__{scope}`` (11A, F-15).

    Derived from what the condition is ABOUT rather than auto-generated, so a service that
    restarts mid-condition finds the existing document and does not re-announce. The double
    underscore matches the convention `notification_id` and `evaluation_doc_id` already use.
    """
    base = _require(name, "name")
    return f"{base}__{scope.upper()}" if scope else base


def ops_event_path(name: str, scope: str | None = None) -> str:
    return f"{OPS_EVENTS}/{ops_event_id(name, scope)}"


def assessment_path(assessment_id: str) -> str:
    """``assessments/{assessment_id}`` (9D)."""
    return f"{ASSESSMENTS}/{_require(assessment_id, 'assessment_id')}"


def trade_note_path(note_id: str) -> str:
    """``trade_notes/{note_id}`` (9D).

    Its own collection rather than a subcollection of the trade, so a note cannot be
    mistaken for a field write on a CLOSED trade (§45) by any code path that iterates a
    document's children.
    """
    return f"{TRADE_NOTES}/{_require(note_id, 'note_id')}"


def daily_review_doc_id(market_date: str, symbol: str | None = None) -> str:
    """``{market_date}`` or ``{market_date}_{symbol}`` (9A).

    The symbol is a suffix rather than a prefix so the ids still sort chronologically as
    strings, which is what makes "the latest review" an index-free maximum (decision 26).
    A review without a symbol keeps the pre-9A id, so nothing already stored moves.
    """
    date = _require(market_date, "market_date")
    return f"{date}_{_require(symbol, 'symbol').upper()}" if symbol else date


def daily_review_path(market_date: str, symbol: str | None = None) -> str:
    """Keyed by broker date (and symbol) so regenerating a day overwrites it (Phase 7)."""
    return f"{DAILY_REVIEWS}/{daily_review_doc_id(market_date, symbol)}"


def weekly_review_doc_id(
    iso_year: int, iso_week: int, symbol: str | None = None
) -> str:
    week = f"{iso_year:04d}-W{iso_week:02d}"
    return f"{week}_{_require(symbol, 'symbol').upper()}" if symbol else week


def weekly_review_path(
    iso_year: int, iso_week: int, symbol: str | None = None
) -> str:
    return f"{WEEKLY_REVIEWS}/{weekly_review_doc_id(iso_year, iso_week, symbol)}"



#: Every collection this deployment writes (11A, F-10). Assigned at the END of the module so
#: the scan in ``_all_collections`` sees every constant above it; anything added below this
#: line would be missed, which is what the source-parsing test exists to catch.
ALL_COLLECTIONS: tuple[str, ...] = _all_collections()
