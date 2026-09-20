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

ALL_COLLECTIONS: tuple[str, ...] = (
    DETECTIONS,
    DETECTION_EVALUATIONS,
    SESSIONS,
    TRADE_REQUESTS,
    TRADES,
    CONTROL_REQUESTS,
    AUDIT_LOGS,
    HEARTBEATS,
    SYSTEM_STATE,
    SETTINGS,
    SYMBOL_SPECS,
    DAILY_REVIEWS,
    WEEKLY_REVIEWS,
)

# ── Fixed document ids ────────────────────────────────────────────────────────
#: The whole-system document id, used until Phase 9A split ``system_state`` per symbol.
#: Kept named so the read path can SKIP it: a deployment that ran before the split still
#: has one, and merging it back in would report every symbol twice -- once live, once
#: frozen at whenever the split happened.
LEGACY_SYSTEM_STATE_DOC = "current"
EXECUTION_SETTINGS_DOC = "execution"

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


def daily_review_path(market_date: str) -> str:
    """Keyed by broker date so regenerating a day overwrites it (Phase 7)."""
    return f"{DAILY_REVIEWS}/{_require(market_date, 'market_date')}"


def weekly_review_doc_id(iso_year: int, iso_week: int) -> str:
    return f"{iso_year:04d}-W{iso_week:02d}"


def weekly_review_path(iso_year: int, iso_week: int) -> str:
    return f"{WEEKLY_REVIEWS}/{weekly_review_doc_id(iso_year, iso_week)}"
