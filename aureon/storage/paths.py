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

from aureon.models.identity import evaluation_doc_id

# ── Collections ───────────────────────────────────────────────────────────────
DETECTIONS = "detections"
DETECTION_EVALUATIONS = "detection_evaluations"
SESSIONS = "sessions"
TRADE_REQUESTS = "trade_requests"
TRADES = "trades"
CONTROL_REQUESTS = "control_requests"
AUDIT_LOGS = "audit_logs"
HEARTBEATS = "heartbeats"
SYSTEM_STATE = "system_state"
SETTINGS = "settings"
DAILY_REVIEWS = "daily_reviews"
WEEKLY_REVIEWS = "weekly_reviews"

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
    DAILY_REVIEWS,
    WEEKLY_REVIEWS,
)

# ── Fixed document ids ────────────────────────────────────────────────────────
SYSTEM_STATE_DOC = "current"
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


def system_state_path() -> str:
    return f"{SYSTEM_STATE}/{SYSTEM_STATE_DOC}"


def execution_settings_path() -> str:
    """``settings/execution`` -- runtime gates, overriding env (decision 11)."""
    return f"{SETTINGS}/{EXECUTION_SETTINGS_DOC}"


def daily_review_path(market_date: str) -> str:
    """Keyed by broker date so regenerating a day overwrites it (Phase 7)."""
    return f"{DAILY_REVIEWS}/{_require(market_date, 'market_date')}"


def weekly_review_doc_id(iso_year: int, iso_week: int) -> str:
    return f"{iso_year:04d}-W{iso_week:02d}"


def weekly_review_path(iso_year: int, iso_week: int) -> str:
    return f"{WEEKLY_REVIEWS}/{weekly_review_doc_id(iso_year, iso_week)}"
