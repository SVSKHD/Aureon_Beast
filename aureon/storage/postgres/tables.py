"""The 24 tables, as SQLAlchemy models (plan §7–§21, C-5, C-6).

One module rather than one per domain, because the schema is read as a whole far more
often than it is read a table at a time -- a foreign key, an index and a claim column only
make sense next to the thing they point at. ``0001_initial`` is generated from this file's
metadata, and so is ``docs/CONTRACTS.md``.

**Column rule (plan §7).** Relational for anything filtered, ordered, identified, claimed
or transitioned on; ``JSONB`` for frozen context read back whole. The test is not size --
it is whether a query will ever need to reach inside. A market snapshot is read as a unit
and is JSONB; ``market_date`` is filtered by every review and is a column.

**Ids are the deterministic ids, used as primary keys.** Not surrogate integers. That is
the whole point of §12's identity: the primary key *is* the idempotency guarantee, so an
outbox retry that re-delivers the same detection collides with itself in the database
rather than relying on every caller remembering to upsert.

**These are persistence models, not domain models** (plan §5). The pydantic models in
``aureon/models`` stay the contract the rest of the system speaks. Column names follow the
domain model's field names wherever one exists, so a row and a model can be read against
each other without a translation table (decision 342).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aureon.storage.postgres.models import Base, Json

# ── Observations ──────────────────────────────────────────────────────────────


class Detection(Base):
    """§8. Immutable. Nothing here may encode what happened afterwards (CLAUDE.md).

    **The time columns are C-6's, and there are four of them rather than one.** A detection
    has two distinct instants and a framing, and collapsing them into a single
    ``detected_at`` would lose the one the identity depends on:

    * ``candle_close_utc`` -- when the detection became *knowable*, and the component
      ``detection_id`` hashes (§12). This is the domain model's ``detected_at``.
    * ``candle_time_utc`` -- the bar's OPEN. A reader comparing a detection to a chart needs
      the bar, not the moment the bar ended.
    * ``timezone`` -- the broker's zone, so the market rendering can be derived.
    * ``market_date`` -- the broker DAY, stored because §8 indexes it and every review
      filters on it. Deriving it per row would rebuild the full-scan the Firestore
      ``in_period`` had to do.

    ``market_timestamp`` is deliberately NOT a column: it is ``candle_close_utc`` rendered
    in ``timezone``, and storing a rendering beside the instant it comes from is the one
    thing ``MarketTime`` exists to prevent (decision 341).
    """

    __tablename__ = "detections"

    detection_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    account_scope: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    timeframe: Mapped[str] = mapped_column(String)
    agent_name: Mapped[str] = mapped_column(String)
    agent_version: Mapped[str] = mapped_column(String)
    event_key: Mapped[str] = mapped_column(String)
    direction: Mapped[str | None] = mapped_column(String)

    candle_close_utc: Mapped[datetime] = mapped_column()
    candle_time_utc: Mapped[datetime] = mapped_column()
    timezone: Mapped[str] = mapped_column(String)
    market_date: Mapped[str] = mapped_column(String)

    price: Mapped[float] = mapped_column(Float)
    sequence_today: Mapped[int] = mapped_column(Integer)
    sequence_session: Mapped[int] = mapped_column(Integer)

    # Relational: reviews group by session, so it is filtered rather than read whole.
    session: Mapped[str] = mapped_column(String)
    session_config_version: Mapped[str] = mapped_column(String)

    # Frozen context. Read back whole, never queried into.
    agent_params_snapshot: Mapped[dict[str, Any]] = mapped_column(Json)
    indicators: Mapped[dict[str, Any] | None] = mapped_column(Json)
    levels: Mapped[dict[str, Any]] = mapped_column(Json)
    evidence: Mapped[dict[str, Any]] = mapped_column(Json)
    volume_profile_ref: Mapped[dict[str, Any] | None] = mapped_column(Json)
    volatility: Mapped[dict[str, Any] | None] = mapped_column(Json)
    mtf: Mapped[dict[str, Any] | None] = mapped_column(Json)

    __table_args__ = (
        Index("ix_detections_symbol_close", "symbol", "candle_close_utc"),
        Index("ix_detections_symbol_date", "symbol", "market_date"),
        Index("ix_detections_agent_close", "agent_name", "candle_close_utc"),
        Index("ix_detections_market_date", "market_date"),
    )


class DetectionEvaluation(Base):
    """§9, §21. What a detection's outcome was, under ONE frozen rule.

    Separate from the detection for the reason §21 gives: an outcome on the detection would
    be future information sitting on a record of the present. One row per (detection, rule),
    so re-running a rule upserts and running a SECOND rule adds rows.
    """

    __tablename__ = "detection_evaluations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    detection_id: Mapped[str] = mapped_column(
        String, ForeignKey("detections.detection_id", ondelete="CASCADE")
    )
    rule_id: Mapped[str] = mapped_column(String)
    evaluation_rule_id: Mapped[str | None] = mapped_column(String)
    reference_price: Mapped[str] = mapped_column(String)
    reference_value: Mapped[float | None] = mapped_column(Float)

    context_tags: Mapped[dict[str, Any]] = mapped_column(Json)
    horizons: Mapped[dict[str, Any]] = mapped_column(Json)
    updated_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("detection_id", "rule_id", name="uq_detection_evaluation"),
        Index("ix_detection_evaluations_rule", "rule_id"),
    )


class Setup(Base):
    """§10. A structure tracked over time. Edited as it advances, unlike a detection."""

    __tablename__ = "setups"

    setup_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    account_scope: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    timeframe: Mapped[str] = mapped_column(String)
    family: Mapped[str] = mapped_column(String)
    direction_context: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    market_date: Mapped[str] = mapped_column(String)

    invalidation_price: Mapped[float | None] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column()
    updated_at: Mapped[datetime | None] = mapped_column()
    # 12 T-9: stamped once, the first time the setup reaches CONFIRMED. "Did it ever make a
    # claim" has to be answerable after the state has moved on.
    confirmed_at: Mapped[datetime | None] = mapped_column()
    closed_at: Mapped[datetime | None] = mapped_column()

    last_event_id: Mapped[str | None] = mapped_column(String)
    event_count: Mapped[int] = mapped_column(Integer)
    setup_version: Mapped[str] = mapped_column(String)

    anchor: Mapped[dict[str, Any]] = mapped_column(Json)
    linked_detection_ids: Mapped[dict[str, Any]] = mapped_column(Json)
    context_summary: Mapped[dict[str, Any]] = mapped_column(Json)
    agent_confluence: Mapped[dict[str, Any]] = mapped_column(Json)
    reference: Mapped[dict[str, Any] | None] = mapped_column(Json)
    params_snapshot: Mapped[dict[str, Any]] = mapped_column(Json)

    __table_args__ = (
        Index("ix_setups_symbol_state", "symbol", "state"),
        Index("ix_setups_symbol_date", "symbol", "market_date"),
        Index("ix_setups_family_state", "family", "state"),
        Index("ix_setups_updated", "updated_at"),
    )


class SetupEvent(Base):
    """§11. A setup's history, as rows rather than an array on the parent.

    An array would grow until a write failed. The event id is deterministic over (setup,
    candle close, event type), which is what makes a replay of the same candle address the
    same row instead of appending a second copy.
    """

    __tablename__ = "setup_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    setup_id: Mapped[str] = mapped_column(
        String, ForeignKey("setups.setup_id", ondelete="CASCADE")
    )
    event_type: Mapped[str] = mapped_column(String)
    from_state: Mapped[str] = mapped_column(String)
    to_state: Mapped[str] = mapped_column(String)
    linked_detection_id: Mapped[str | None] = mapped_column(String)

    market_time_utc: Mapped[datetime] = mapped_column()
    timezone: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column()

    context_snapshot: Mapped[dict[str, Any]] = mapped_column(Json)
    reason: Mapped[str | None] = mapped_column(String)

    __table_args__ = (Index("ix_setup_events_setup_created", "setup_id", "created_at"),)


class SetupEvaluation(Base):
    """§13. A setup's outcome under one rule, cohort dimensions included as columns.

    The cohort dimensions (family, direction_context, symbol, market_date) are relational
    rather than folded into ``context_summary``, because ``setup_reference`` SELECTS on them
    to build a cohort -- that is the definition of a filtered field under §7.
    """

    __tablename__ = "setup_evaluations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    setup_id: Mapped[str] = mapped_column(
        String, ForeignKey("setups.setup_id", ondelete="CASCADE")
    )
    rule_id: Mapped[str] = mapped_column(String)
    evaluation_rule_id: Mapped[str | None] = mapped_column(String)

    family: Mapped[str] = mapped_column(String)
    direction_context: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    timeframe: Mapped[str] = mapped_column(String)
    market_date: Mapped[str] = mapped_column(String)
    setup_version: Mapped[str] = mapped_column(String)

    reference_price: Mapped[str] = mapped_column(String)
    reference_value: Mapped[float | None] = mapped_column(Float)

    context_summary: Mapped[dict[str, Any]] = mapped_column(Json)
    horizons: Mapped[dict[str, Any]] = mapped_column(Json)
    updated_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("setup_id", "rule_id", name="uq_setup_evaluation"),
        Index("ix_setup_evaluations_cohort", "symbol", "family", "market_date"),
    )


class SessionSummary(Base):
    """§18 (Phase 1). One row per (broker day, session, symbol, timeframe).

    ``timezone`` is here for the reason detections, setup events, trades and market days
    each carry one: ``started_at`` and ``ended_at`` are ``MarketTime``, and a ``MarketTime``
    cannot be rebuilt from an instant alone. Reconstructing it from the running config
    instead would mean a session stored under one broker's zone rendering itself in
    another's the day the config changes -- which is the drift `MarketTime` exists to make
    impossible (decision 357).
    """

    __tablename__ = "sessions"

    session_doc_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    session_id: Mapped[str] = mapped_column(String)
    account_scope: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    timeframe: Mapped[str] = mapped_column(String)
    session: Mapped[str] = mapped_column(String)
    market_date: Mapped[str] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String)
    session_config_version: Mapped[str] = mapped_column(String)

    started_at: Mapped[datetime | None] = mapped_column()
    ended_at: Mapped[datetime | None] = mapped_column()

    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    trend: Mapped[str | None] = mapped_column(String)
    change: Mapped[float | None] = mapped_column(Float)
    change_points: Mapped[float | None] = mapped_column(Float)
    range: Mapped[float | None] = mapped_column(Float)
    candle_count: Mapped[int] = mapped_column(Integer)

    __table_args__ = (Index("ix_sessions_symbol_date", "symbol", "market_date"),)


# ── The money path (C-7: additive-only from here on) ──────────────────────────
# These four tables may only ever GAIN columns and indexes. No later migration may drop or
# retype one of them, and none of them has a destructive downgrade -- because the only copy
# of what was requested, what was sent and what came back lives here, and a downgrade that
# "just" narrowed a column would be indistinguishable from the trade never having happened.


class TradeRequest(Base):
    """§14, §15. The human's intent, and the executor's claim on it.

    **The claim is three columns, not a boolean.** §15's exactly-once claim is
    ``SELECT ... WHERE status = 'CONFIRMED' AND claimable ORDER BY confirmed_at FOR UPDATE
    SKIP LOCKED LIMIT 1``, and what makes it safe is that the lease has an OWNER and an
    EXPIRY: a crashed executor's claim has to become claimable again without a human, and a
    second executor has to be able to tell "mine" from "someone else's, expired".

    ``sl`` and ``tp`` are nullable and are written by the Discord request handler alone
    (C-2), from values a trader typed. Nothing in the engine, the agents or the executor
    may assign them: a stop this system chose would be a trading decision made by a
    detection, which is the one thing it must never do. `/execute` without them sends an
    order with no stops, and a boundary test enforces the rule.
    """

    __tablename__ = "trade_requests"

    request_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    order_type: Mapped[str] = mapped_column(String)
    volume: Mapped[float] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    # C-2. User-supplied or absent. Never computed.
    sl: Mapped[float | None] = mapped_column(Float)
    tp: Mapped[float | None] = mapped_column(Float)
    deviation_points: Mapped[int] = mapped_column(Integer)
    filling_mode: Mapped[str | None] = mapped_column(String)

    requested_by: Mapped[str] = mapped_column(String)
    requested_at: Mapped[datetime] = mapped_column()
    confirmed_at: Mapped[datetime | None] = mapped_column()
    confirmed_by: Mapped[str | None] = mapped_column(String)
    expires_at: Mapped[datetime | None] = mapped_column()
    confirmation_version: Mapped[int] = mapped_column(Integer)

    detection_id: Mapped[str | None] = mapped_column(String)
    link_type: Mapped[str | None] = mapped_column(String)

    # The claim (§15). ``executor_instance_id`` is the plan's ``claim_owner`` and
    # ``lease_expires_at`` its ``claim_expires_at``; the domain model's names are kept so a
    # row and a model can be read against each other (decision 342).
    executor_instance_id: Mapped[str | None] = mapped_column(String)
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    execution_started_at: Mapped[datetime | None] = mapped_column()
    execution_attempt_id: Mapped[str | None] = mapped_column(String)

    comment_token: Mapped[str | None] = mapped_column(String)
    # BigInteger throughout for broker identifiers: an MT5 ticket is already ten digits at
    # some brokers and int4 stops at 2147483647. An overflow here is a failed write on the
    # money path, after the order has been sent.
    magic: Mapped[int | None] = mapped_column(BigInteger)
    order_ticket: Mapped[int | None] = mapped_column(BigInteger)
    position_id: Mapped[int | None] = mapped_column(BigInteger)
    deal_ids: Mapped[dict[str, Any]] = mapped_column(Json)

    fill_price: Mapped[float | None] = mapped_column(Float)
    filled_volume: Mapped[float | None] = mapped_column(Float)
    failure_code: Mapped[str | None] = mapped_column(String)
    failure_message: Mapped[str | None] = mapped_column(String)

    quote: Mapped[dict[str, Any] | None] = mapped_column(Json)
    last_reconciled_at: Mapped[datetime | None] = mapped_column()
    last_synced_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        # The claim query's own index: status first because it is the equality, then
        # confirmed_at because it is the ORDER BY that makes the claim fair.
        Index("ix_trade_requests_claim", "status", "confirmed_at"),
        Index("ix_trade_requests_symbol_status", "symbol", "status"),
        Index("ix_trade_requests_lease", "lease_expires_at"),
        Index("ix_trade_requests_detection", "detection_id"),
    )


class Trade(Base):
    """§18, §49–§53. An actual position. MT5 is the truth; this is Aureon's record of it."""

    __tablename__ = "trades"

    trade_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    mt5_position_id: Mapped[int] = mapped_column(BigInteger)
    trade_request_id: Mapped[str | None] = mapped_column(String)
    # §52: an external trade is imported and never managed, so the source is a column the
    # monitor filters on rather than a flag buried in a payload.
    source: Mapped[str] = mapped_column(String)

    symbol: Mapped[str] = mapped_column(String)
    direction: Mapped[str] = mapped_column(String)
    volume: Mapped[float] = mapped_column(Float)
    open_price: Mapped[float] = mapped_column(Float)
    open_time_utc: Mapped[datetime] = mapped_column()
    timezone: Mapped[str] = mapped_column(String)

    sl: Mapped[float | None] = mapped_column(Float)
    tp: Mapped[float | None] = mapped_column(Float)
    magic: Mapped[int | None] = mapped_column(BigInteger)

    status: Mapped[str] = mapped_column(String)
    closed_volume: Mapped[float] = mapped_column(Float)
    close_price: Mapped[float | None] = mapped_column(Float)
    close_time_utc: Mapped[datetime | None] = mapped_column()
    close_reason: Mapped[str] = mapped_column(String)
    close_reason_raw: Mapped[str | None] = mapped_column(String)

    realized_pnl: Mapped[float | None] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float)
    swap: Mapped[float] = mapped_column(Float)

    detection_id: Mapped[str | None] = mapped_column(String)
    link_type: Mapped[str | None] = mapped_column(String)

    deal_ids: Mapped[dict[str, Any]] = mapped_column(Json)
    excursion: Mapped[dict[str, Any]] = mapped_column(Json)
    last_reconciled_at: Mapped[datetime | None] = mapped_column()
    last_synced_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        # The reconciler's lookup: MT5 hands back a position id and this has to find the row.
        Index("ix_trades_position", "mt5_position_id", unique=True),
        Index("ix_trades_symbol_status", "symbol", "status"),
        Index("ix_trades_open_time", "open_time_utc"),
        Index("ix_trades_request", "trade_request_id"),
    )


class ControlRequest(Base):
    """§17, §46, §47. Close, close-all, flatten -- claimed the same way a trade request is.

    The same claim shape on purpose: a control request moves money too, and an operation
    that could run twice because it used a weaker mechanism than the one beside it is the
    kind of asymmetry nobody notices until it closes a position twice.
    """

    __tablename__ = "control_requests"

    control_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    target: Mapped[str] = mapped_column(String)
    symbol: Mapped[str | None] = mapped_column(String)
    volume: Mapped[float | None] = mapped_column(Float)

    requested_by: Mapped[str] = mapped_column(String)
    requested_at: Mapped[datetime] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()

    executor_instance_id: Mapped[str | None] = mapped_column(String)
    lease_expires_at: Mapped[datetime | None] = mapped_column()

    failure_code: Mapped[str | None] = mapped_column(String)
    failure_message: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        Index("ix_control_requests_claim", "status", "requested_at"),
        Index("ix_control_requests_lease", "lease_expires_at"),
    )


class AuditRecord(Base):
    """§71. Append-only. Who asked, what changed, and whether a machine did it.

    ``reconciliation`` is a column rather than a detail key because "was this a human or the
    reconciler" is the first question asked of any surprising state change, and a question
    asked that often is a filter.
    """

    __tablename__ = "audit_logs"

    audit_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    at: Mapped[datetime] = mapped_column()
    actor: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    collection: Mapped[str | None] = mapped_column(String)
    document_id: Mapped[str | None] = mapped_column(String)
    from_status: Mapped[str | None] = mapped_column(String)
    to_status: Mapped[str | None] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(String)
    reconciliation: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[dict[str, Any]] = mapped_column(Json)

    __table_args__ = (
        Index("ix_audit_logs_at", "at"),
        Index("ix_audit_logs_document", "collection", "document_id"),
        Index("ix_audit_logs_actor", "actor", "at"),
    )


# ── Operational state ─────────────────────────────────────────────────────────


class Heartbeat(Base):
    """§67, decision 10. One row per service -- the source of truth for liveness.

    Local and throttled (§20), and not synced to Supabase by default: a heartbeat is a
    statement about THIS machine, and a copy of it in the cloud would be a statement about
    a machine nobody is looking at.
    """

    __tablename__ = "heartbeats"

    service: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column()
    instance_id: Mapped[str | None] = mapped_column(String)
    detail: Mapped[dict[str, Any]] = mapped_column(Json)


class SymbolStateRow(Base):
    """§21. One row per (symbol, timeframe). A candle close forces a write.

    Per symbol and timeframe rather than one row for everything, for the reason 9A split
    it: the write throttle is per row, so gold's candle close would otherwise suppress
    silver's state for the next few seconds, and a reader wanting one symbol should not have
    to read every symbol to get it.

    Almost everything here is a live snapshot read back whole, so it is JSONB. The four
    columns are the ones something filters or orders on.
    """

    __tablename__ = "system_state"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    symbol: Mapped[str] = mapped_column(String)
    timeframe: Mapped[str] = mapped_column(String)
    market_state: Mapped[str] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column()

    state: Mapped[dict[str, Any]] = mapped_column(Json)

    __table_args__ = (Index("ix_system_state_symbol", "symbol", "timeframe", unique=True),)


class Setting(Base):
    """§22, decision 11. ``execution`` and ``notifications``, one row each.

    ``trading_enabled`` lives in the database and defaults to FALSE, so a fresh deployment
    cannot trade until a human enables it -- and it is deliberately NOT an environment
    variable, because it is the switch a human flips in Discord in a hurry. The live-account
    gate is the other layer and stays in the environment (§23).
    """

    __tablename__ = "settings"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    settings_version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime | None] = mapped_column()
    updated_by: Mapped[str | None] = mapped_column(String)
    value: Mapped[dict[str, Any]] = mapped_column(Json)


#: The two rows ``settings`` holds. Here rather than in the repository because this module
#: is the naming authority for the relational store, and a row name in a key-value table is
#: part of the schema exactly as a table name is (decision 356). It also keeps the repository
#: free of the bare literal ``"notifications"``, which §83's guard reads -- correctly -- as a
#: store name spelled by hand.
EXECUTION_SETTINGS_ROW = "execution"
NOTIFICATION_SETTINGS_ROW = "notifications"


class SymbolSpec(Base):
    """Decision 79. Broker metadata published for Discord. A convenience copy, never an
    authority: the execution guard re-reads the live symbol at execution time."""

    __tablename__ = "symbol_specs"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime | None] = mapped_column()
    spec: Mapped[dict[str, Any]] = mapped_column(Json)


class OpsEvent(Base):
    """11A F-15. A named operational condition, and whether it is currently true.

    ``active`` and ``name`` are columns because `/ops` filters on exactly those, and
    ``onsets`` counts how often a condition has recurred -- which is the difference between
    a one-off and something worth fixing.
    """

    __tablename__ = "ops_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    name: Mapped[str] = mapped_column(String)
    scope: Mapped[str | None] = mapped_column(String)
    service: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean)
    since: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime | None] = mapped_column()
    onsets: Mapped[int] = mapped_column(Integer)
    # Free text, not a payload: ``OpsEvent.detail`` is the one line an operator reads in
    # `/ops`. JSONB here would store a JSON string -- round-tripping correctly and lying
    # about the shape to everything that inspects the schema (decision 358).
    detail: Mapped[str] = mapped_column(String)

    __table_args__ = (
        Index("ix_ops_events_active", "active", "name"),
        Index("ix_ops_events_updated", "updated_at"),
    )


class Notification(Base):
    """9C, §71. What Discord has already said. The id is what makes a send exactly-once.

    Derived from what the message is ABOUT rather than generated, so a bot that dies between
    posting and recording finds the row on restart instead of posting again. ``message_id``
    is here rather than on the setup (12 T-11): a Discord message id is a fact about a
    notification, and putting it on the setup would make the observer's record depend on
    whether a chat client happened to be up.
    """

    __tablename__ = "notifications"

    notification_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    kind: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    ref_id: Mapped[str] = mapped_column(String)
    channel_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    sent_at: Mapped[datetime | None] = mapped_column()
    message_id: Mapped[str | None] = mapped_column(String)
    failure_message: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        Index("ix_notifications_ref", "kind", "ref_id"),
        Index("ix_notifications_status", "status", "sent_at"),
    )


class PriceAlert(Base):
    """9C. What a human asked to be told. Armed alerts are polled every candle, so
    ``status`` and ``symbol`` are the index the observer's sweep uses."""

    __tablename__ = "alerts"

    alert_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    symbol: Mapped[str] = mapped_column(String)
    level: Mapped[float] = mapped_column(Float)
    side: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    requested_by: Mapped[str] = mapped_column(String)
    note: Mapped[str | None] = mapped_column(String)
    cancelled_by: Mapped[str | None] = mapped_column(String)

    created_at: Mapped[datetime] = mapped_column()
    expires_at: Mapped[datetime | None] = mapped_column()
    fired_at: Mapped[datetime | None] = mapped_column()
    fired_price: Mapped[float | None] = mapped_column(Float)
    fired_snapshot: Mapped[dict[str, Any] | None] = mapped_column(Json)

    __table_args__ = (
        Index("ix_alerts_armed", "status", "symbol"),
        Index("ix_alerts_expires", "expires_at"),
    )


# ── Research and reviews ──────────────────────────────────────────────────────


class Assessment(Base):
    """9D. A measured-only readout for `/monitor`. Never a prediction.

    ``history_source`` and ``real_days`` are columns and not buried in a payload, because
    C-13 requires that code treating synthetic history as not-real be able to FILTER on
    them -- and a provenance flag that can only be read after deserialising the row is one
    that gets forgotten.
    """

    __tablename__ = "assessments"

    assessment_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    detection_id: Mapped[str | None] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    rule_id: Mapped[str] = mapped_column(String)
    n: Mapped[int] = mapped_column(Integer)
    insufficient: Mapped[bool] = mapped_column(Boolean)
    disagrees_with_detection: Mapped[bool] = mapped_column(Boolean)
    history_source: Mapped[str | None] = mapped_column(String)
    real_days: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column()

    trend_read: Mapped[dict[str, Any]] = mapped_column(Json)
    cohort_filter: Mapped[dict[str, Any]] = mapped_column(Json)
    confirmations: Mapped[dict[str, Any]] = mapped_column(Json)
    tp_estimates: Mapped[dict[str, Any]] = mapped_column(Json)
    sl_estimates: Mapped[dict[str, Any]] = mapped_column(Json)
    paired: Mapped[dict[str, Any] | None] = mapped_column(Json)

    __table_args__ = (
        Index("ix_assessments_symbol_created", "symbol", "created_at"),
        Index("ix_assessments_detection", "detection_id"),
        Index("ix_assessments_provenance", "history_source"),
    )


class TradeNote(Base):
    """9D. A trader's own words about a trade.

    Its own table rather than a child of the trade, so a note cannot be mistaken for a field
    write on a CLOSED trade (§45) by anything that iterates a record's parts. The assessment
    service is forbidden from reading these at all, and a boundary test says so: a note is
    a human's opinion, and measured statistics must not be conditioned on one.
    """

    __tablename__ = "trade_notes"

    note_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    trade_id: Mapped[str] = mapped_column(String)
    author: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(String)
    at: Mapped[datetime] = mapped_column()

    __table_args__ = (Index("ix_trade_notes_trade", "trade_id", "at"),)


class TrainingExample(Base):
    """Immutable EOD learning row derived from one setup and its later outcomes."""

    __tablename__ = "training_examples"

    example_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    market_date: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    timeframe: Mapped[str] = mapped_column(String)
    setup_id: Mapped[str] = mapped_column(String)
    family: Mapped[str] = mapped_column(String)
    direction_context: Mapped[str] = mapped_column(String)
    setup_version: Mapped[str] = mapped_column(String)

    feature_schema_version: Mapped[str] = mapped_column(String)
    label_schema_version: Mapped[str] = mapped_column(String)
    context: Mapped[dict[str, Any]] = mapped_column(Json)
    agent_read: Mapped[dict[str, Any]] = mapped_column(Json)

    # V1 canonical training record. Additive-only so legacy +6 rows remain readable.
    features: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    outcome: Mapped[dict[str, Any] | None] = mapped_column(Json)
    setup_created_at: Mapped[datetime | None] = mapped_column()
    feature_frozen_at: Mapped[datetime | None] = mapped_column()
    outcome_resolved_at: Mapped[datetime | None] = mapped_column()
    entry_price: Mapped[float | None] = mapped_column(Float)
    direction: Mapped[str | None] = mapped_column(String)

    six_dollar_status: Mapped[str] = mapped_column(String)
    six_dollar_reached: Mapped[bool | None] = mapped_column(Boolean)
    six_dollar_reference_price: Mapped[float | None] = mapped_column(Float)
    six_dollar_threshold_price: Mapped[float | None] = mapped_column(Float)
    six_dollar_reached_at: Mapped[datetime | None] = mapped_column()
    time_to_six_seconds: Mapped[float | None] = mapped_column(Float)
    twenty_dollar_reached: Mapped[bool | None] = mapped_column(Boolean)
    forty_dollar_reached: Mapped[bool | None] = mapped_column(Boolean)
    time_to_twenty_seconds: Mapped[float | None] = mapped_column(Float)
    time_to_forty_seconds: Mapped[float | None] = mapped_column(Float)
    max_favourable_move_price: Mapped[float | None] = mapped_column(Float)
    extension_after_six_price: Mapped[float | None] = mapped_column(Float)

    mfe_points: Mapped[float | None] = mapped_column(Float)
    mae_points: Mapped[float | None] = mapped_column(Float)
    mae_before_six_price: Mapped[float | None] = mapped_column(Float)

    evaluation_rule_id: Mapped[str | None] = mapped_column(String)
    evaluation_complete: Mapped[bool] = mapped_column(Boolean)
    generated_at: Mapped[datetime] = mapped_column()

    __table_args__ = (
        Index("ix_training_examples_symbol_date", "symbol", "market_date"),
        Index("ix_training_examples_timeframe", "symbol", "timeframe", "market_date"),
        UniqueConstraint(
            "setup_id",
            "feature_schema_version",
            "label_schema_version",
            name="uq_training_example_contract",
        ),
    )


class DailyTrainingStatus(Base):
    """Durable EOD training checkpoint and data-readiness summary."""

    __tablename__ = "daily_training_status"

    status_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    market_date: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    feature_schema_version: Mapped[str] = mapped_column(String)
    label_schema_version: Mapped[str] = mapped_column(String)

    examples_written: Mapped[int] = mapped_column(Integer)
    reached_six: Mapped[int] = mapped_column(Integer)
    not_reached_six: Mapped[int] = mapped_column(Integer)
    unavailable_six: Mapped[int] = mapped_column(Integer)
    complete_evaluations: Mapped[int] = mapped_column(Integer)
    mae_before_six_available: Mapped[int] = mapped_column(Integer)
    median_mae_before_six_price: Mapped[float | None] = mapped_column(Float)
    max_mae_before_six_price: Mapped[float | None] = mapped_column(Float)

    by_timeframe: Mapped[dict[str, Any]] = mapped_column(Json)
    generated_at: Mapped[datetime] = mapped_column()

    __table_args__ = (
        Index(
            "ix_daily_training_status_contract",
            "symbol",
            "market_date",
            "feature_schema_version",
            "label_schema_version",
            unique=True,
        ),
    )


class ModelRegistry(Base):
    """Versioned trained model artifacts. Shadow is the only live-active status."""

    __tablename__ = "model_registry"

    model_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String)
    algorithm: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    feature_schema_version: Mapped[str] = mapped_column(String)
    label_schema_version: Mapped[str] = mapped_column(String)
    model_schema_version: Mapped[str] = mapped_column(String)
    trained_from: Mapped[str] = mapped_column(String)
    trained_through: Mapped[str] = mapped_column(String)
    training_samples: Mapped[int] = mapped_column(Integer)
    target_metrics: Mapped[dict[str, Any]] = mapped_column(Json)
    artifact: Mapped[dict[str, Any]] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column()
    activated_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        Index("ix_model_registry_symbol_created", "symbol", "created_at"),
        Index("ix_model_registry_symbol_status", "symbol", "status"),
    )


class ModelTrainingRun(Base):
    __tablename__ = "model_training_runs"

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    model_id: Mapped[str | None] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    algorithm: Mapped[str] = mapped_column(String)
    feature_schema_version: Mapped[str] = mapped_column(String)
    label_schema_version: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()
    sample_count: Mapped[int] = mapped_column(Integer)
    trained_from: Mapped[str | None] = mapped_column(String)
    trained_through: Mapped[str | None] = mapped_column(String)
    target_metrics: Mapped[dict[str, Any]] = mapped_column(Json)
    failure_message: Mapped[str | None] = mapped_column(String)

    __table_args__ = (Index("ix_model_training_symbol_started", "symbol", "started_at"),)


class ModelBacktest(Base):
    __tablename__ = "model_backtests"

    backtest_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    model_id: Mapped[str | None] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    algorithm: Mapped[str] = mapped_column(String)
    feature_schema_version: Mapped[str] = mapped_column(String)
    label_schema_version: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()
    start_market_date: Mapped[str | None] = mapped_column(String)
    end_market_date: Mapped[str | None] = mapped_column(String)
    folds: Mapped[dict[str, Any]] = mapped_column(Json)
    aggregate_metrics: Mapped[dict[str, Any]] = mapped_column(Json)
    out_of_sample_predictions: Mapped[int] = mapped_column(Integer)
    failure_message: Mapped[str | None] = mapped_column(String)

    __table_args__ = (Index("ix_model_backtests_symbol_started", "symbol", "started_at"),)


class ModelPrediction(Base):
    __tablename__ = "model_predictions"

    prediction_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    model_id: Mapped[str] = mapped_column(String)
    setup_id: Mapped[str] = mapped_column(String)
    event_id: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    timeframe: Mapped[str] = mapped_column(String)
    predicted_at: Mapped[datetime] = mapped_column()
    feature_schema_version: Mapped[str] = mapped_column(String)
    label_schema_version: Mapped[str] = mapped_column(String)
    probabilities: Mapped[dict[str, Any]] = mapped_column(Json)
    feature_snapshot: Mapped[dict[str, Any]] = mapped_column(Json)
    actual_outcomes: Mapped[dict[str, Any] | None] = mapped_column(Json)
    reconciled_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        Index("ix_model_predictions_symbol_time", "symbol", "predicted_at"),
        Index("ix_model_predictions_model", "model_id", "predicted_at"),
        UniqueConstraint("model_id", "setup_id", name="uq_model_prediction_setup"),
    )


class DailyReview(Base):
    """§37 (Phase 7). Keyed by broker date and symbol, so regenerating a day overwrites it.

    The body is JSONB: a review is a rendered answer, read whole, and the counts inside it
    are not something another query filters on. The four columns are what "which reviews do
    I have" needs.
    """

    __tablename__ = "daily_reviews"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    market_date: Mapped[str] = mapped_column(String)
    symbol: Mapped[str | None] = mapped_column(String)
    # NULLABLE, because ``ReviewBase.generated_at`` is. A review regenerated to be compared
    # against an earlier one is asked for WITHOUT a stamp, so that its bytes are a pure
    # function of the data it aggregates and two runs can be diffed (§61). NOT NULL made
    # that call fail at the database instead -- a schema opinion the model does not hold,
    # and invisible until the one caller that relies on the looseness runs (decision 373).
    generated_at: Mapped[datetime | None] = mapped_column()
    evaluation_rule_id: Mapped[str | None] = mapped_column(String)

    review: Mapped[dict[str, Any]] = mapped_column(Json)

    __table_args__ = (Index("ix_daily_reviews_date", "market_date", "symbol"),)


class WeeklyReview(Base):
    """§37. Same shape, keyed by ISO year and week.

    The id keeps the sortable ``{iso_year}-W{iso_week}[_{symbol}]`` form, so "the latest
    review" stays an index-free maximum (decision 26).
    """

    __tablename__ = "weekly_reviews"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    iso_year: Mapped[int] = mapped_column(Integer)
    iso_week: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str | None] = mapped_column(String)
    #: Nullable for the same reason as the daily review's (decision 373).
    generated_at: Mapped[datetime | None] = mapped_column()
    evaluation_rule_id: Mapped[str | None] = mapped_column(String)

    review: Mapped[dict[str, Any]] = mapped_column(Json)

    __table_args__ = (Index("ix_weekly_reviews_week", "iso_year", "iso_week", "symbol"),)


# ── The broker-day cache (11D) ────────────────────────────────────────────────


class MarketDay(Base):
    """11D. One broker day's shape. M1 is NOT here -- it is in the parquet archive.

    ``complete`` is a column because the day in progress must be excludable by a query: a
    tuning report that averaged a half-finished day alongside finished ones would report a
    range that is simply wrong, and nothing downstream could tell.
    """

    __tablename__ = "market_days"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    symbol: Mapped[str] = mapped_column(String)
    market_date: Mapped[str] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String)
    complete: Mapped[bool] = mapped_column(Boolean)

    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    bars: Mapped[int] = mapped_column(Integer)
    tick_volume: Mapped[int] = mapped_column(BigInteger)

    first_bar_at: Mapped[datetime | None] = mapped_column()
    last_bar_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime | None] = mapped_column()

    frames: Mapped[dict[str, Any]] = mapped_column(Json)

    __table_args__ = (
        Index("ix_market_days_symbol_date", "symbol", "market_date", unique=True),
        Index("ix_market_days_complete", "symbol", "complete", "market_date"),
    )


class MarketDayFrame(Base):
    """11D. The aggregated bars of one day at one timeframe.

    Separate from the day because the bars are bulky and a caller usually wants the day's
    shape without them. ``truncated`` records that the stored bars are not the whole day --
    a reader that mistook a truncated frame for a complete one would compute an EMA over a
    window that silently starts in the middle of the session.
    """

    __tablename__ = "market_day_frames"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    symbol: Mapped[str] = mapped_column(String)
    market_date: Mapped[str] = mapped_column(String)
    timeframe: Mapped[str] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String)
    truncated: Mapped[bool] = mapped_column(Boolean)
    complete: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime | None] = mapped_column()

    bars: Mapped[dict[str, Any]] = mapped_column(Json)

    __table_args__ = (
        Index(
            "ix_market_day_frames_key", "symbol", "market_date", "timeframe", unique=True
        ),
    )


# ── The optional weekly mirror (§30) ──────────────────────────────────────────


class SyncBatch(Base):
    """§30. One row per weekly Supabase push. Never on the money path.

    Exists so a sync is idempotent: the batch id is the ISO week (``2026-W39_WEEKLY``), so a
    retry after a failed or half-finished push addresses the same row rather than mirroring
    the week twice. Supabase being unreachable, misconfigured or switched off changes
    nothing here except that no row is written (§34).
    """

    __tablename__ = "sync_batches"

    batch_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    tables_sent: Mapped[dict[str, Any]] = mapped_column(Json)
    row_counts: Mapped[dict[str, Any]] = mapped_column(Json)
    failure_message: Mapped[str | None] = mapped_column(String)

    __table_args__ = (Index("ix_sync_batches_started", "started_at"),)
