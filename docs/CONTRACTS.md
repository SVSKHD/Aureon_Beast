# Aureon contracts

**Generated — do not edit by hand.** Run `python scripts/gen_contracts.py`
after any model change and commit the result.

Schema version: **1** (§6, decision 12) — carried on every
stored document.

---

## Collections

Shown with the DEFAULT prefix `aureon_beast`. The live one
comes from `AUREON_COLLECTION_PREFIX` (decision 111); the document describes the
shape, not one deployment's value -- otherwise this file would differ between a
test run and production and could never be checked in.

| collection | document id | model |
|---|---|---|
| `aureon_beast_alerts` | `alert_id` | `PriceAlert` |
| `aureon_beast_assessments` | `assessment_id` | `Assessment` |
| `aureon_beast_audit_logs` | `audit_id` | `AuditRecord` |
| `aureon_beast_control_requests` | `control_id` | `ControlRequest` |
| `aureon_beast_daily_reviews` | `{market_date}[_{symbol}]` | `DailyReview` |
| `aureon_beast_detection_evaluations` | `{detection_id}__{rule_id}` | `DetectionEvaluation` |
| `aureon_beast_detections` | `detection_id` | `Detection` |
| `aureon_beast_heartbeats` | `{service}` | `Heartbeat` |
| `aureon_beast_market_day_frames` | `**not documented — add it to COLLECTION_DOCS**` | `?` |
| `aureon_beast_market_days` | `**not documented — add it to COLLECTION_DOCS**` | `?` |
| `aureon_beast_notifications` | `{kind}__{ref_id}` | `Notification` |
| `aureon_beast_ops_events` | `{name}[__{scope}]` | `OpsEvent` |
| `aureon_beast_sessions` | `{market_date}__{session}` | `SessionSummary` |
| `aureon_beast_settings` | `execution / notifications` | `ExecutionSettings, NotificationSettings` |
| `aureon_beast_symbol_specs` | `{symbol}` | `SymbolInfo` |
| `aureon_beast_system_state` | `{symbol}_{timeframe}` | `SystemState` |
| `aureon_beast_trade_notes` | `note_id` | `TradeNote` |
| `aureon_beast_trade_requests` | `request_id` | `TradeRequest` |
| `aureon_beast_trades` | `trade_id` | `Trade` |
| `aureon_beast_weekly_reviews` | `{iso_year}-W{iso_week}[_{symbol}]` | `WeeklyReview` |

Tick data is never stored in Firestore. There is no `pending_orders`
collection (decision 9): a pending order *is* the `PENDING` trade request.

---

## Documents

### Detection

An immutable observation. Never an instruction to trade.

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `detection_id` | `str` | yes | — | Deterministic id from identity.detection_id. |
| `account_scope` | `str` | yes | — |  |
| `symbol` | `str` | yes | — |  |
| `timeframe` | `Timeframe` | yes | — |  |
| `agent_name` | `str` | yes | — |  |
| `agent_version` | `str` | yes | — |  |
| `agent_params_snapshot` | `dict[str, object]` | no | `dict()` | Full agent parameters at observation time (§84). |
| `event_key` | `str` | yes | — | Agent-specific event, e.g. "bullish" or "up\|previous_day_high". |
| `direction` | `Direction \| null` | no | `None` | Direction, where the event has one. Context-only agents omit it. |
| `detected_at` | `MarketTime` | yes | — | Candle CLOSE: when this became known. |
| `candle_open_time` | `MarketTime` | yes | — |  |
| `price` | `float` | yes | — | Candle close price at detection. |
| `indicators` | `IndicatorSnapshot` | no | `IndicatorSnapshot()` |  |
| `session` | `SessionContext` | yes | — |  |
| `levels` | `dict[str, float]` | no | `dict()` | Numeric levels involved (swept level, broken level, ...). |
| `sequence_today` | `int` | yes | — |  |
| `sequence_session` | `int` | yes | — |  |
| `volume_profile_ref` | `VolumeProfileRef \| null` | no | `None` | Asia's value area and nodes as they stood at this close (9B). |
| `volatility` | `VolatilityContext \| null` | no | `None` | ATR and session range vs median at this close (9B). |
| `mtf` | `MtfContext \| null` | no | `None` | Higher-timeframe reads and alignment at this close (11D). |

### DetectionEvaluation

Outcomes for one detection under one rule (§22).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `detection_id` | `str` | yes | — |  |
| `rule_id` | `str` | yes | — |  |
| `evaluation_rule_id` | `str \| null` | no | `None` | Alias of rule_id, for review documents (§84). |
| `reference_price` | `ReferencePrice` | yes | — |  |
| `reference_value` | `float \| null` | no | `None` | The actual price measured from. |
| `horizons` | `tuple[HorizonResult]` | no | `()` |  |
| `context_tags` | `dict[str, bool]` | no | `dict()` | What else the machine had seen at this detection's candle close (§19, §23). Derived ONLY from data available at that close -- see aureon.evaluation.context_tags. Research grouping, never a gate. |
| `updated_at` | `AwareDatetime \| null` | no | `None` |  |

### TradeRequest

A human-initiated request to trade (§25-§34).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `request_id` | `str` | yes | — |  |
| `status` | `TradeRequestStatus` | no | `'requested'` |  |
| `symbol` | `str` | yes | — |  |
| `order_type` | `OrderType` | yes | — |  |
| `volume` | `float` | yes | — |  |
| `price` | `float \| null` | no | `None` | Entry for pending orders. |
| `sl` | `float \| null` | no | `None` |  |
| `tp` | `float \| null` | no | `None` |  |
| `deviation_points` | `int` | no | `0` |  |
| `filling_mode` | `FillingMode \| null` | no | `None` |  |
| `requested_by` | `str` | yes | — | Discord user id of the requester. |
| `requested_at` | `AwareDatetime` | no | `utc_now()` |  |
| `confirmed_at` | `AwareDatetime \| null` | no | `None` |  |
| `confirmed_by` | `str \| null` | no | `None` |  |
| `expires_at` | `AwareDatetime \| null` | no | `None` | Confirmation TTL deadline (§28). |
| `confirmation_version` | `int` | no | `0` |  |
| `quote` | `QuoteSnapshot \| null` | no | `None` | Quote shown to the human at confirmation (§27). |
| `detection_id` | `str \| null` | no | `None` | Explicitly linked detection, if any (§50). |
| `link_type` | `LinkType \| null` | no | `None` |  |
| `executor_instance_id` | `str \| null` | no | `None` |  |
| `lease_expires_at` | `AwareDatetime \| null` | no | `None` |  |
| `execution_started_at` | `AwareDatetime \| null` | no | `None` |  |
| `execution_attempt_id` | `str \| null` | no | `None` |  |
| `comment_token` | `str \| null` | no | `None` |  |
| `magic` | `int \| null` | no | `None` |  |
| `order_ticket` | `int \| null` | no | `None` |  |
| `position_id` | `int \| null` | no | `None` |  |
| `deal_ids` | `tuple[int]` | no | `()` |  |
| `fill_price` | `float \| null` | no | `None` |  |
| `filled_volume` | `float \| null` | no | `None` |  |
| `failure_code` | `FailureCode \| null` | no | `None` |  |
| `failure_message` | `str \| null` | no | `None` |  |
| `last_reconciled_at` | `AwareDatetime \| null` | no | `None` |  |
| `last_synced_at` | `AwareDatetime \| null` | no | `None` |  |

### Trade

A real position. MT5 is the truth (§49-§53).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `trade_id` | `str` | yes | — |  |
| `mt5_position_id` | `int` | yes | — |  |
| `trade_request_id` | `str \| null` | no | `None` | None for externally-opened positions (§52). |
| `source` | `TradeSource` | no | `'aureon'` |  |
| `symbol` | `str` | yes | — |  |
| `direction` | `Direction` | yes | — |  |
| `volume` | `float` | yes | — | Original opened volume. |
| `open_price` | `float` | yes | — |  |
| `open_time` | `MarketTime` | yes | — |  |
| `sl` | `float \| null` | no | `None` |  |
| `tp` | `float \| null` | no | `None` |  |
| `magic` | `int \| null` | no | `None` |  |
| `status` | `TradeStatus` | no | `'open'` |  |
| `closed_volume` | `float` | no | `0.0` |  |
| `close_price` | `float \| null` | no | `None` |  |
| `close_time` | `MarketTime \| null` | no | `None` |  |
| `close_reason` | `str` | no | `'unknown'` | Convention (decision 15): ['broker', 'discord', 'manual', 'mobile', 'sl', 'tp', 'unknown']. |
| `close_reason_raw` | `str \| null` | no | `None` | Broker's own reason code, kept verbatim. |
| `realized_pnl` | `float \| null` | no | `None` |  |
| `commission` | `float` | no | `0.0` |  |
| `swap` | `float` | no | `0.0` |  |
| `deal_ids` | `tuple[int]` | no | `()` |  |
| `excursion` | `Excursion` | no | `Excursion()` |  |
| `detection_id` | `str \| null` | no | `None` |  |
| `link_type` | `LinkType \| null` | no | `None` |  |
| `last_reconciled_at` | `AwareDatetime \| null` | no | `None` |  |
| `last_synced_at` | `AwareDatetime \| null` | no | `None` |  |

### ControlRequest

A requested action on something already live (§46, §47).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `control_id` | `str` | yes | — |  |
| `kind` | `ControlRequestKind` | yes | — |  |
| `status` | `ControlRequestStatus` | no | `'requested'` |  |
| `target` | `str` | yes | — | Order ticket for cancel, position id for close. |
| `symbol` | `str \| null` | no | `None` |  |
| `volume` | `float \| null` | no | `None` | Partial close volume; None closes all. |
| `requested_by` | `str` | yes | — |  |
| `requested_at` | `AwareDatetime` | no | `utc_now()` |  |
| `executor_instance_id` | `str \| null` | no | `None` |  |
| `lease_expires_at` | `AwareDatetime \| null` | no | `None` |  |
| `completed_at` | `AwareDatetime \| null` | no | `None` |  |
| `failure_code` | `FailureCode \| null` | no | `None` |  |
| `failure_message` | `str \| null` | no | `None` |  |

### AuditRecord

One audited action.

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `audit_id` | `str` | yes | — |  |
| `at` | `AwareDatetime` | no | `utc_now()` |  |
| `actor` | `str` | yes | — | Discord user id, executor instance id, or 'system'. |
| `action` | `str` | yes | — | e.g. "trade_request.confirm", "trading.disable". |
| `collection` | `str \| null` | no | `None` |  |
| `document_id` | `str \| null` | no | `None` |  |
| `from_status` | `str \| null` | no | `None` |  |
| `to_status` | `str \| null` | no | `None` |  |
| `reason` | `str \| null` | no | `None` |  |
| `detail` | `dict[str, object]` | no | `dict()` |  |
| `reconciliation` | `bool` | no | `False` |  |

### Heartbeat

A service's liveness beat, at ``heartbeats/{service}`` (§67).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `service` | `str` | yes | — | observer \| executor \| monitor \| discord |
| `updated_at` | `AwareDatetime` | no | `utc_now()` |  |
| `instance_id` | `str \| null` | no | `None` |  |
| `detail` | `dict[str, object]` | no | `dict()` |  |

### SystemState

One document describing the whole system's health (§59, §61-§63).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `updated_at` | `AwareDatetime` | no | `utc_now()` |  |
| `symbols` | `tuple[SymbolState]` | no | `()` |  |
| `heartbeats` | `dict[str, AwareDatetime]` | no | `dict()` |  |
| `trading_enabled` | `bool \| null` | no | `None` | Mirror of settings/execution, for display only. |
| `account_mode` | `AccountMode \| null` | no | `None` |  |
| `sleep_phase` | `SleepPhase \| null` | no | `None` |  |
| `next_market_open` | `AwareDatetime \| null` | no | `None` |  |
| `notes` | `str \| null` | no | `None` |  |

### ExecutionSettings

Execution gates and limits (§56, §84).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `trading_enabled` | `bool` | no | `False` |  |
| `max_lot` | `float` | no | `1.0` |  |
| `max_spread_points` | `float` | no | `50.0` |  |
| `max_deviation_points` | `int` | no | `20` |  |
| `max_open_positions` | `int` | no | `5` |  |
| `max_daily_trades` | `int` | no | `20` |  |
| `confirmation_ttl_seconds` | `float` | no | `60.0` |  |
| `quote_ttl_seconds` | `float` | no | `15.0` |  |
| `status_stale_after_seconds` | `float` | no | `45.0` |  |
| `executor_lease_seconds` | `float` | no | `60.0` |  |
| `allowed_symbols` | `tuple[str]` | no | `()` | Empty means no symbol allowlist is enforced. |
| `per_symbol` | `dict[str, SymbolLimits]` | no | `dict()` |  |
| `settings_version` | `int` | no | `0` |  |
| `updated_at` | `AwareDatetime \| null` | no | `None` |  |
| `updated_by` | `str \| null` | no | `None` |  |
| `disabled_reason` | `str \| null` | no | `None` |  |

### SessionSummary

A completed trading session (§18).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `session_id` | `str` | yes | — | {market_date}__{session} |
| `account_scope` | `str` | yes | — |  |
| `symbol` | `str` | yes | — |  |
| `timeframe` | `Timeframe` | yes | — |  |
| `session` | `SessionName` | yes | — |  |
| `market_date` | `str` | yes | — | Broker-local date, YYYY-MM-DD. |
| `session_config_version` | `int` | yes | — | Boundaries are config (decision 7); stamped so a later change cannot silently reinterpret this document. |
| `started_at` | `MarketTime` | yes | — | Open of the session's first candle. |
| `ended_at` | `MarketTime` | yes | — | Close of the session's last candle. |
| `open` | `float` | yes | — |  |
| `high` | `float` | yes | — |  |
| `low` | `float` | yes | — |  |
| `close` | `float` | yes | — |  |
| `trend` | `str` | yes | — | One of ['down', 'flat', 'up']. |
| `change` | `float` | yes | — | close - open, in price. |
| `change_points` | `float` | yes | — | close - open, in points. |
| `range` | `float` | yes | — | high - low, in price. |
| `candle_count` | `int` | yes | — |  |

### DailyReview

One broker trading day (§61).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `period_start` | `AwareDatetime` | yes | — |  |
| `period_end` | `AwareDatetime` | yes | — |  |
| `market_tz` | `str` | yes | — |  |
| `generated_at` | `AwareDatetime \| null` | no | `None` |  |
| `evaluation_rule_id` | `str` | yes | — | Which frozen rule produced the counts (§84). |
| `symbol` | `str \| null` | no | `None` |  |
| `detections_total` | `int` | no | `0` |  |
| `detections_by_agent` | `dict[str, int]` | no | `dict()` |  |
| `detections_by_session` | `dict[SessionName, int]` | no | `dict()` |  |
| `horizons` | `tuple[HorizonOutcome]` | no | `()` |  |
| `pending_horizons_excluded` | `int` | no | `0` |  |
| `invalid_horizons_excluded` | `int` | no | `0` |  |
| `trades_total` | `int` | no | `0` |  |
| `trades_closed` | `int` | no | `0` |  |
| `realized_pnl` | `float` | no | `0.0` |  |
| `trades_by_session` | `dict[SessionName, int]` | no | `dict()` |  |
| `explicit_links` | `int` | no | `0` |  |
| `inferred_links` | `tuple[InferredLink]` | no | `()` |  |
| `notes` | `str \| null` | no | `None` |  |
| `assessments_total` | `int` | no | `0` |  |
| `assessments_hit` | `int` | no | `0` |  |
| `assessments_miss` | `int` | no | `0` |  |
| `assessments_neither` | `int` | no | `0` |  |
| `assessments_unresolved` | `int` | no | `0` |  |
| `assessments_not_scored` | `int` | no | `0` |  |
| `assessment_hit_by_cohort` | `dict[str, str]` | no | `dict()` |  |
| `assessment_hit_by_source` | `dict[str, str]` | no | `dict()` |  |
| `trade_notes` | `dict[str, tuple[str]]` | no | `dict()` |  |
| `trades_by_tag` | `dict[str, tuple[str]]` | no | `dict()` |  |
| `market_date` | `str` | yes | — | Broker-local date, YYYY-MM-DD. |
| `sessions_covered` | `tuple[SessionName]` | no | `()` |  |

### WeeklyReview

One trading week, generated after Friday's close (§63).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `period_start` | `AwareDatetime` | yes | — |  |
| `period_end` | `AwareDatetime` | yes | — |  |
| `market_tz` | `str` | yes | — |  |
| `generated_at` | `AwareDatetime \| null` | no | `None` |  |
| `evaluation_rule_id` | `str` | yes | — | Which frozen rule produced the counts (§84). |
| `symbol` | `str \| null` | no | `None` |  |
| `detections_total` | `int` | no | `0` |  |
| `detections_by_agent` | `dict[str, int]` | no | `dict()` |  |
| `detections_by_session` | `dict[SessionName, int]` | no | `dict()` |  |
| `horizons` | `tuple[HorizonOutcome]` | no | `()` |  |
| `pending_horizons_excluded` | `int` | no | `0` |  |
| `invalid_horizons_excluded` | `int` | no | `0` |  |
| `trades_total` | `int` | no | `0` |  |
| `trades_closed` | `int` | no | `0` |  |
| `realized_pnl` | `float` | no | `0.0` |  |
| `trades_by_session` | `dict[SessionName, int]` | no | `dict()` |  |
| `explicit_links` | `int` | no | `0` |  |
| `inferred_links` | `tuple[InferredLink]` | no | `()` |  |
| `notes` | `str \| null` | no | `None` |  |
| `assessments_total` | `int` | no | `0` |  |
| `assessments_hit` | `int` | no | `0` |  |
| `assessments_miss` | `int` | no | `0` |  |
| `assessments_neither` | `int` | no | `0` |  |
| `assessments_unresolved` | `int` | no | `0` |  |
| `assessments_not_scored` | `int` | no | `0` |  |
| `assessment_hit_by_cohort` | `dict[str, str]` | no | `dict()` |  |
| `assessment_hit_by_source` | `dict[str, str]` | no | `dict()` |  |
| `trade_notes` | `dict[str, tuple[str]]` | no | `dict()` |  |
| `trades_by_tag` | `dict[str, tuple[str]]` | no | `dict()` |  |
| `iso_year` | `int` | yes | — |  |
| `iso_week` | `int` | yes | — |  |
| `daily_review_ids` | `tuple[str]` | no | `()` |  |

### PriceAlert

A level a human asked to be told about (9C, §71).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `alert_id` | `str` | yes | — |  |
| `symbol` | `str` | yes | — |  |
| `level` | `float` | yes | — |  |
| `side` | `str` | yes | — | One of ('above', 'below'). |
| `requested_by` | `str` | yes | — | Discord user id -- the only recipient. |
| `note` | `str \| null` | no | `None` | The human's own words, echoed back when it fires. |
| `status` | `PriceAlertStatus` | no | `'armed'` |  |
| `created_at` | `AwareDatetime \| null` | no | `None` |  |
| `expires_at` | `AwareDatetime \| null` | no | `None` |  |
| `fired_at` | `AwareDatetime \| null` | no | `None` |  |
| `fired_price` | `float \| null` | no | `None` |  |
| `fired_snapshot` | `dict[str, object]` | no | `dict()` | What the market looked like when it crossed, frozen by the observer. Rendered by Discord as-is; never recomputed (9C). |
| `cancelled_by` | `str \| null` | no | `None` |  |

### Notification

One thing Discord said, so it cannot say it twice (9C).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `notification_id` | `str` | yes | — | {kind}__{ref_id}; see paths.notification_id. |
| `kind` | `NotificationKind` | yes | — |  |
| `symbol` | `str` | yes | — |  |
| `ref_id` | `str` | yes | — | detection_id or alert_id -- what this is about. |
| `channel_id` | `str` | yes | — |  |
| `sent_at` | `AwareDatetime \| null` | no | `None` |  |
| `status` | `NotificationStatus` | no | `'sent'` |  |
| `failure_message` | `str \| null` | no | `None` |  |

### Assessment

One measured readout for one detection (§62-§64, 9D).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `assessment_id` | `str` | yes | — |  |
| `detection_id` | `str` | yes | — |  |
| `symbol` | `str` | yes | — |  |
| `rule_id` | `str` | yes | — |  |
| `trend_read` | `TrendRead` | yes | — |  |
| `cohort_filter` | `CohortFilter` | yes | — |  |
| `n` | `int` | no | `0` |  |
| `confirmations` | `tuple[HorizonConfirmation]` | no | `()` |  |
| `tp_estimates` | `tuple[Estimate]` | no | `()` |  |
| `sl_estimates` | `tuple[Estimate]` | no | `()` |  |
| `paired` | `PairedOutcome \| null` | no | `None` |  |
| `insufficient` | `bool` | no | `False` |  |
| `disagrees_with_detection` | `bool` | no | `False` |  |
| `history_source` | `HistorySource` | no | `'unknown'` |  |
| `real_days` | `int` | no | `0` |  |
| `created_at` | `AwareDatetime \| null` | no | `None` |  |

### TradeNote

A human sentence about one trade, kept out of the trade (§45, 9D).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `note_id` | `str` | yes | — |  |
| `trade_id` | `str` | yes | — |  |
| `author` | `str` | yes | — |  |
| `text` | `str` | yes | — |  |
| `at` | `AwareDatetime \| null` | no | `None` |  |

### OpsEvent

One named condition, and whether it is currently true.

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `event_id` | `str` | yes | — | {name} or {name}__{scope}; see paths.ops_event_id. |
| `name` | `str` | yes | — |  |
| `scope` | `str \| null` | no | `None` |  |
| `active` | `bool` | no | `False` |  |
| `since` | `AwareDatetime \| null` | no | `None` |  |
| `detail` | `str` | no | `''` |  |
| `service` | `str` | no | `''` |  |
| `onsets` | `int` | no | `0` |  |
| `updated_at` | `AwareDatetime \| null` | no | `None` |  |

---

## Embedded value models

### MarketTime

An instant, stored as UTC, renderable in the broker's market zone.

| field | type | required | default | notes |
|---|---|---|---|---|
| `utc` | `AwareDatetime` | yes | — | The instant, always normalised to UTC. |
| `market_tz` | `str` | yes | — | IANA zone of the broker's server clock (§8). |

### Candle

One CLOSED candle.

| field | type | required | default | notes |
|---|---|---|---|---|
| `symbol` | `str` | yes | — |  |
| `timeframe` | `Timeframe` | yes | — |  |
| `open_time` | `MarketTime` | yes | — | Candle OPEN, the candle's identity. |
| `open` | `float` | yes | — |  |
| `high` | `float` | yes | — |  |
| `low` | `float` | yes | — |  |
| `close` | `float` | yes | — |  |
| `tick_volume` | `int` | no | `0` |  |
| `real_volume` | `int` | no | `0` |  |
| `spread` | `int \| null` | no | `None` | Broker-reported spread in points, if available. |

### QuoteSnapshot

A bid/ask pair at an instant (§27, §41).

| field | type | required | default | notes |
|---|---|---|---|---|
| `symbol` | `str` | yes | — |  |
| `bid` | `float` | yes | — |  |
| `ask` | `float` | yes | — |  |
| `captured_at` | `AwareDatetime` | no | `utc_now()` |  |
| `point` | `float \| null` | no | `None` | Symbol point size, so spread can be given in points. |

### SymbolInfo

Broker metadata for a symbol (§39, §42).

| field | type | required | default | notes |
|---|---|---|---|---|
| `symbol` | `str` | yes | — |  |
| `point` | `float` | yes | — | Smallest price increment. |
| `digits` | `int` | yes | — |  |
| `volume_min` | `float` | yes | — |  |
| `volume_max` | `float` | yes | — |  |
| `volume_step` | `float` | yes | — |  |
| `stops_level` | `int` | no | `0` | Minimum stop distance from price, in POINTS (§42). |
| `filling_modes` | `tuple[FillingMode]` | no | `()` |  |
| `trade_mode` | `str` | no | `'unknown'` | Broker trade mode: full / close_only / disabled (§10). |
| `spread` | `int \| null` | no | `None` | Current spread in points. |

### CandleContext

What the engine hands an agent alongside the candle window (§79).

| field | type | required | default | notes |
|---|---|---|---|---|
| `account_scope` | `str` | yes | — |  |
| `symbol` | `str` | yes | — |  |
| `timeframe` | `Timeframe` | yes | — |  |
| `market_tz` | `str` | yes | — |  |
| `closed_at` | `MarketTime` | yes | — | The candle's close instant. |
| `candle_open_time` | `MarketTime` | yes | — |  |
| `session` | `SessionContext` | yes | — |  |
| `sequence_today` | `int` | yes | — | Nth detection-eligible candle in the broker day. |
| `sequence_session` | `int` | yes | — | Nth within the session. |

### IndicatorSnapshot

Indicator values AT the closed candle (§13, §14).

| field | type | required | default | notes |
|---|---|---|---|---|
| `ema` | `dict[str, float]` | no | `dict()` | Named EMA values, e.g. {"fast": 2401.2, "slow": 2399.8}. |
| `rsi` | `float \| null` | no | `None` | RSI, when warmed up. |
| `extras` | `dict[str, float]` | no | `dict()` | Agent-specific numeric context. |

### SessionContext

Which session the candle closed in (§18).

| field | type | required | default | notes |
|---|---|---|---|---|
| `session` | `SessionName` | yes | — |  |
| `session_config_version` | `int` | yes | — |  |

### BrokerOrderRequest

What Aureon asks the broker to do (§30, §37-§42).

| field | type | required | default | notes |
|---|---|---|---|---|
| `symbol` | `str` | yes | — |  |
| `order_type` | `OrderType` | yes | — |  |
| `volume` | `float` | yes | — |  |
| `price` | `float \| null` | no | `None` | Required for pending orders; None for market. |
| `sl` | `float \| null` | no | `None` |  |
| `tp` | `float \| null` | no | `None` |  |
| `deviation_points` | `int` | no | `0` |  |
| `filling_mode` | `FillingMode \| null` | no | `None` |  |
| `magic` | `int` | yes | — |  |
| `comment` | `str` | yes | — | comment_token (§34). |

### BrokerOrderResult

What the broker said back (§31-§33).

| field | type | required | default | notes |
|---|---|---|---|---|
| `ok` | `bool` | yes | — |  |
| `retcode` | `int \| null` | no | `None` |  |
| `retcode_name` | `str \| null` | no | `None` |  |
| `order_ticket` | `int \| null` | no | `None` |  |
| `deal_ids` | `tuple[int]` | no | `()` |  |
| `position_id` | `int \| null` | no | `None` |  |
| `fill_price` | `float \| null` | no | `None` |  |
| `filled_volume` | `float \| null` | no | `None` |  |
| `failure_code` | `FailureCode \| null` | no | `None` |  |
| `message` | `str \| null` | no | `None` |  |
| `raw` | `dict[str, object]` | no | `dict()` |  |

### AccountInfo

Account state, for the margin and exposure guards (§56).

| field | type | required | default | notes |
|---|---|---|---|---|
| `login` | `int` | yes | — |  |
| `currency` | `str` | no | `'USD'` |  |
| `balance` | `float` | yes | — |  |
| `equity` | `float` | yes | — |  |
| `margin` | `float` | no | `0.0` |  |
| `margin_free` | `float` | no | `0.0` |  |
| `margin_level` | `float \| null` | no | `None` |  |
| `leverage` | `int \| null` | no | `None` |  |
| `server` | `str \| null` | no | `None` |  |
| `mode` | `AccountMode` | no | `'unknown'` |  |
| `raw` | `dict[str, object]` | no | `dict()` |  |

### BrokerPosition

An open position as the broker reports it (§49).

| field | type | required | default | notes |
|---|---|---|---|---|
| `position_id` | `int` | yes | — |  |
| `symbol` | `str` | yes | — |  |
| `direction` | `Direction` | yes | — |  |
| `volume` | `float` | yes | — |  |
| `open_price` | `float` | yes | — |  |
| `open_time` | `AwareDatetime` | yes | — |  |
| `sl` | `float \| null` | no | `None` |  |
| `tp` | `float \| null` | no | `None` |  |
| `profit` | `float` | no | `0.0` |  |
| `swap` | `float` | no | `0.0` |  |
| `magic` | `int \| null` | no | `None` |  |
| `comment` | `str \| null` | no | `None` |  |
| `raw` | `dict[str, object]` | no | `dict()` |  |

### BrokerOrder

A resting (pending) order as the broker reports it (§43).

| field | type | required | default | notes |
|---|---|---|---|---|
| `order_ticket` | `int` | yes | — |  |
| `symbol` | `str` | yes | — |  |
| `order_type` | `OrderType` | yes | — |  |
| `volume` | `float` | yes | — |  |
| `price` | `float` | yes | — |  |
| `sl` | `float \| null` | no | `None` |  |
| `tp` | `float \| null` | no | `None` |  |
| `placed_at` | `AwareDatetime \| null` | no | `None` |  |
| `expires_at` | `AwareDatetime \| null` | no | `None` |  |
| `magic` | `int \| null` | no | `None` |  |
| `comment` | `str \| null` | no | `None` |  |
| `raw` | `dict[str, object]` | no | `dict()` |  |

### BrokerDeal

A deal -- the record of something actually executing (§36).

| field | type | required | default | notes |
|---|---|---|---|---|
| `deal_id` | `int` | yes | — |  |
| `order_ticket` | `int \| null` | no | `None` |  |
| `position_id` | `int \| null` | no | `None` |  |
| `symbol` | `str` | yes | — |  |
| `direction` | `Direction` | yes | — |  |
| `entry` | `DealEntry` | yes | — |  |
| `volume` | `float` | yes | — |  |
| `price` | `float` | yes | — |  |
| `executed_at` | `AwareDatetime` | yes | — |  |
| `profit` | `float` | no | `0.0` |  |
| `commission` | `float` | no | `0.0` |  |
| `swap` | `float` | no | `0.0` |  |
| `magic` | `int \| null` | no | `None` |  |
| `comment` | `str \| null` | no | `None` |  |
| `reason` | `str \| null` | no | `None` | Broker reason code, kept verbatim (decision 15). |
| `raw` | `dict[str, object]` | no | `dict()` |  |

### PendingOrder

A live pending order at the broker (§43, decision 9).

| field | type | required | default | notes |
|---|---|---|---|---|
| `order_ticket` | `int` | yes | — |  |
| `symbol` | `str` | yes | — |  |
| `order_type` | `OrderType` | yes | — |  |
| `volume` | `float` | yes | — |  |
| `price` | `float` | yes | — |  |
| `sl` | `float \| null` | no | `None` |  |
| `tp` | `float \| null` | no | `None` |  |
| `magic` | `int \| null` | no | `None` |  |
| `comment` | `str \| null` | no | `None` |  |
| `placed_at` | `AwareDatetime \| null` | no | `None` |  |
| `expires_at` | `AwareDatetime \| null` | no | `None` |  |

### Excursion

How far a position ran for and against, while it was open (§45).

| field | type | required | default | notes |
|---|---|---|---|---|
| `mfe` | `float \| null` | no | `None` | Max favourable excursion. |
| `mfe_at` | `AwareDatetime \| null` | no | `None` |  |
| `mfe_price` | `float \| null` | no | `None` |  |
| `mae` | `float \| null` | no | `None` | Max adverse excursion. |
| `mae_at` | `AwareDatetime \| null` | no | `None` |  |
| `mae_price` | `float \| null` | no | `None` |  |
| `source` | `ExcursionSource` | no | `'live_ticks'` |  |

### EvaluationRule

A frozen recipe for evaluating detections (§21).

| field | type | required | default | notes |
|---|---|---|---|---|
| `rule_id` | `str` | yes | — |  |
| `reference_price` | `ReferencePrice` | yes | — |  |
| `horizons` | `tuple[Horizon]` | yes | — |  |
| `thresholds` | `tuple[float]` | yes | — | Favourable/adverse distances, in `threshold_unit`. |
| `threshold_unit` | `ThresholdUnit` | no | `'points'` | Whether `thresholds` are broker points or quote-currency price. |
| `termination` | `str` | no | `'per_horizon'` | How a horizon ends; 'per_horizon' defers to each horizon's kind. |

### Horizon

One measurement window on a detection (§21).

| field | type | required | default | notes |
|---|---|---|---|---|
| `id` | `str` | yes | — |  |
| `kind` | `HorizonKind` | yes | — |  |
| `value` | `int \| null` | no | `None` | Candle/minute count; None for event-driven kinds. |

### HorizonResult

What happened within one horizon -- or that we do not know yet (§22, §23).

| field | type | required | default | notes |
|---|---|---|---|---|
| `horizon_id` | `str` | yes | — |  |
| `status` | `HorizonStatus` | no | `'pending'` |  |
| `future_high` | `float \| null` | no | `None` |  |
| `future_low` | `float \| null` | no | `None` |  |
| `mfe` | `float \| null` | no | `None` | Max favourable excursion, points. |
| `mfe_at` | `AwareDatetime \| null` | no | `None` |  |
| `mfe_price` | `float \| null` | no | `None` |  |
| `mae` | `float \| null` | no | `None` | Max adverse excursion, points. |
| `mae_at` | `AwareDatetime \| null` | no | `None` |  |
| `mae_price` | `float \| null` | no | `None` |  |
| `reached` | `dict[str, bool]` | no | `dict()` |  |
| `time_to` | `dict[str, float \| null]` | no | `dict()` | Seconds from detection to first reach. |
| `path` | `PathClassification` | no | `'none'` |  |
| `path_ambiguous` | `bool` | no | `False` | True when the favourable and adverse thresholds were first crossed within the SAME candle, so their real order is unknowable from candle data. ``path`` still follows the rule in §23, but a review can exclude these rather than trust an order that was never observed. |
| `candles_seen` | `int` | no | `0` |  |
| `completed_at` | `AwareDatetime \| null` | no | `None` |  |
| `invalid_reason` | `str \| null` | no | `None` |  |

### InferredLink

A guessed association between a detection and a trade (§50).

| field | type | required | default | notes |
|---|---|---|---|---|
| `detection_id` | `str` | yes | — |  |
| `trade_id` | `str` | yes | — |  |
| `confidence` | `float` | yes | — |  |
| `link_type` | `LinkType` | no | `'inferred'` |  |
| `reason` | `str \| null` | no | `None` | e.g. "same symbol+direction, opened 4m after". |

### ThresholdOutcome

Reached counts for one threshold, from COMPLETE horizons only (§22).

| field | type | required | default | notes |
|---|---|---|---|---|
| `threshold` | `float` | yes | — |  |
| `reached` | `int` | no | `0` |  |
| `evaluated` | `int` | no | `0` | COMPLETE horizons contributing to this count. |

### HorizonOutcome

Aggregated outcomes for one horizon across a period.

| field | type | required | default | notes |
|---|---|---|---|---|
| `horizon_id` | `str` | yes | — |  |
| `thresholds` | `tuple[ThresholdOutcome]` | no | `()` |  |
| `mfe_mean` | `float \| null` | no | `None` |  |
| `mae_mean` | `float \| null` | no | `None` |  |
| `mfe_first` | `int` | no | `0` |  |
| `mae_first` | `int` | no | `0` |  |

### SymbolState

Per symbol/timeframe observation state (§59).

| field | type | required | default | notes |
|---|---|---|---|---|
| `symbol` | `str` | yes | — |  |
| `timeframe` | `Timeframe` | yes | — |  |
| `market_state` | `MarketState` | no | `'unknown'` |  |
| `last_closed_candle_time` | `AwareDatetime \| null` | no | `None` |  |
| `last_tick_at` | `AwareDatetime \| null` | no | `None` |  |
| `last_quote` | `QuoteSnapshot \| null` | no | `None` | Latest bid/ask as state, for Discord's screens (decision 80). |
| `detections_today` | `int` | no | `0` |  |
| `ema_crosses_today` | `int` | no | `0` |  |
| `ema_crosses_session` | `int` | no | `0` |  |
| `bullish_crosses_today` | `int` | no | `0` |  |
| `bearish_crosses_today` | `int` | no | `0` |  |
| `bullish_crosses_session` | `int` | no | `0` |  |
| `bearish_crosses_session` | `int` | no | `0` |  |
| `ema_fast` | `float \| null` | no | `None` |  |
| `ema_slow` | `float \| null` | no | `None` |  |
| `ema_distance` | `float \| null` | no | `None` | fast - slow, in price. Sign is the current bias. |
| `rsi` | `float \| null` | no | `None` |  |
| `rsi_zone` | `str \| null` | no | `None` | overbought \| oversold \| neutral, derived from rsi. |
| `volume_profile` | `dict[str, ProfileSummary]` | no | `dict()` | current_session \| asia \| day -> that scope's summary (9B). |
| `volatility` | `VolatilityContext \| null` | no | `None` | ATR(14) and the session range vs its median (9B). |
| `trend_read` | `TrendRead \| null` | no | `None` | What the last N closed candles did, as facts and a summary (9D). |
| `mtf` | `MtfContext \| null` | no | `None` | Higher-timeframe reads at the last closed candle (11D). |
| `session` | `SessionName \| null` | no | `None` |  |
| `session_trend` | `str \| null` | no | `None` |  |
| `session_high` | `float \| null` | no | `None` |  |
| `session_low` | `float \| null` | no | `None` |  |
| `last_cross` | `dict[str, object] \| null` | no | `None` | {direction, at, price, detection_id} |
| `last_cross_at` | `AwareDatetime \| null` | no | `None` | Denormalised from last_cross so freshness needs no dict parsing. |
| `last_sweep` | `dict[str, object] \| null` | no | `None` | {direction, level_type, at} |
| `last_wick` | `dict[str, object] \| null` | no | `None` | {classification, at} |
| `last_breakout` | `dict[str, object] \| null` | no | `None` | {direction, level_type, at} |

### SymbolLimits

Per-symbol overrides of the execution limits (9A).

| field | type | required | default | notes |
|---|---|---|---|---|
| `max_lot` | `float \| null` | no | `None` |  |
| `max_spread_points` | `float \| null` | no | `None` |  |
| `max_deviation_points` | `int \| null` | no | `None` |  |

### ResolvedLimits

The limits that actually apply to one symbol, with no ``None`` left.

| field | type | required | default | notes |
|---|---|---|---|---|
| `symbol` | `str` | yes | — |  |
| `max_lot` | `float` | yes | — |  |
| `max_spread_points` | `float` | yes | — |  |
| `max_deviation_points` | `int` | yes | — |  |
| `overridden` | `tuple[str]` | no | `()` |  |

### NotificationSettings

What Discord announces, held at ``settings/notifications`` (9C).

| field | type | required | default | notes |
|---|---|---|---|---|
| `schema_version` | `int` | no | `1` | Document schema version (§6, decision 12). |
| `enabled_kinds` | `tuple[str]` | no | `('ema_cross', 'wick', 'liquidity', 'breakout')` | agent_name values Discord posts an embed for (9C). |
| `detections_enabled` | `bool` | no | `True` |  |
| `updated_at` | `AwareDatetime \| null` | no | `None` |  |
| `updated_by` | `str \| null` | no | `None` |  |

### ProfileBin

One price bin and the volume estimated to have traded in it.

| field | type | required | default | notes |
|---|---|---|---|---|
| `price` | `float` | yes | — | The bin's LOWER edge, in price. |
| `tick_volume` | `float` | yes | — | Estimated MT5 TICK volume in this bin -- the number of price changes, not contracts traded (11A, F-5). |

### ProfileSummary

A profile without its bins, for ``SystemState`` and ``/status`` (9B).

| field | type | required | default | notes |
|---|---|---|---|---|
| `scope` | `str` | yes | — |  |
| `poc_price` | `float \| null` | no | `None` |  |
| `value_area_high` | `float \| null` | no | `None` |  |
| `value_area_low` | `float \| null` | no | `None` |  |
| `hvn` | `tuple[float]` | no | `()` |  |
| `lvn` | `tuple[float]` | no | `()` |  |
| `total_tick_volume` | `float` | no | `0.0` |  |

### VolumeProfile

Estimated volume by price over one scope (9B, §19).

| field | type | required | default | notes |
|---|---|---|---|---|
| `symbol` | `str` | yes | — |  |
| `timeframe` | `Timeframe` | yes | — |  |
| `scope` | `str` | yes | — | One of ('asia', 'london', 'new_york', 'day', 'rolling_24h', 'previous_session'). |
| `start_utc` | `AwareDatetime` | yes | — |  |
| `end_utc` | `AwareDatetime` | yes | — |  |
| `bin_points` | `float` | yes | — | Bin width in POINTS. Part of the record because the POC depends on it. |
| `poc_price` | `float \| null` | no | `None` | Lower edge of the highest-volume bin, or None if empty. |
| `value_area_high` | `float \| null` | no | `None` |  |
| `value_area_low` | `float \| null` | no | `None` | The band around the POC holding 70% of estimated volume (§19). |
| `hvn` | `tuple[float]` | no | `()` |  |
| `lvn` | `tuple[float]` | no | `()` |  |
| `total_tick_volume` | `float` | no | `0.0` |  |
| `bins` | `tuple[ProfileBin]` | no | `()` | At most 200, coarsest-first. |

### VolumeProfileRef

What a detection records about the profile that existed when it fired (9B).

| field | type | required | default | notes |
|---|---|---|---|---|
| `scope` | `str` | yes | — |  |
| `poc_price` | `float \| null` | no | `None` |  |
| `va_high` | `float \| null` | no | `None` |  |
| `va_low` | `float \| null` | no | `None` |  |
| `price_vs_va` | `str \| null` | no | `None` | above \| inside \| below, at the detection's price. |
| `nearest_lvn` | `float \| null` | no | `None` |  |
| `nearest_hvn` | `float \| null` | no | `None` |  |

### VolatilityContext

How big this candle, and this session, are by recent standards (9B).

| field | type | required | default | notes |
|---|---|---|---|---|
| `atr_14` | `float \| null` | no | `None` | ATR(14) in PRICE. |
| `atr_points` | `float \| null` | no | `None` | The same, in points. |
| `candle_range_pct_of_atr` | `float \| null` | no | `None` | This candle's range as a fraction of ATR(14). |
| `session_range` | `float \| null` | no | `None` | High-low of the session so far, in price. |
| `session_range_vs_median` | `float \| null` | no | `None` | That range over the 20-day median for this session. |
| `regime` | `str \| null` | no | `None` | One of ('low', 'normal', 'high'). |
| `bands_version` | `int \| null` | no | `None` | Which regime bands produced `regime` (9B). |

### TrendRead

What the last N closed candles did, as facts rather than as a verdict (9D).

| field | type | required | default | notes |
|---|---|---|---|---|
| `bias` | `TrendBias` | yes | — |  |
| `evidence` | `tuple[str]` | no | `()` |  |
| `candles` | `int` | no | `0` | How many closed candles were read. |
| `as_of` | `AwareDatetime \| null` | no | `None` |  |

### CohortFilter

Which prior detections this assessment counted, and what it had to give up.

| field | type | required | default | notes |
|---|---|---|---|---|
| `symbol` | `str` | yes | — |  |
| `agent_name` | `str` | yes | — |  |
| `direction` | `Direction` | yes | — |  |
| `session` | `SessionName \| null` | no | `None` |  |
| `trend_aligned` | `bool \| null` | no | `None` |  |
| `volatility_regime` | `str \| null` | no | `None` |  |
| `price_vs_va` | `str \| null` | no | `None` |  |
| `wick_tag` | `str \| null` | no | `None` |  |
| `dropped` | `tuple[str]` | no | `()` |  |

### ThresholdConfirmation

How often the cohort reached one threshold, with n and its interval.

| field | type | required | default | notes |
|---|---|---|---|---|
| `threshold` | `float` | yes | — |  |
| `reached` | `int` | no | `0` |  |
| `evaluated` | `int` | no | `0` |  |
| `ci_low` | `float \| null` | no | `None` |  |
| `ci_high` | `float \| null` | no | `None` |  |
| `median_seconds_to_first` | `float \| null` | no | `None` |  |

### HorizonConfirmation

One horizon's confirmation table for the cohort.

| field | type | required | default | notes |
|---|---|---|---|---|
| `horizon_id` | `str` | yes | — |  |
| `evaluated` | `int` | no | `0` |  |
| `thresholds` | `tuple[ThresholdConfirmation]` | no | `()` |  |
| `mae_first` | `int` | no | `0` |  |
| `path_ambiguous` | `int` | no | `0` |  |

### Estimate

One quantile of the cohort's measured excursion, in points and in price.

| field | type | required | default | notes |
|---|---|---|---|---|
| `quantile` | `float` | yes | — |  |
| `points` | `float` | yes | — |  |
| `price` | `float \| null` | no | `None` | The points applied to this detection's reference price. |

### PairedOutcome

How often the cohort reached +T before -S, for the symbol's configured pair.

| field | type | required | default | notes |
|---|---|---|---|---|
| `favourable` | `float` | yes | — |  |
| `adverse` | `float` | yes | — |  |
| `favourable_first` | `int` | no | `0` |  |
| `evaluated` | `int` | no | `0` |  |
| `ci_low` | `float \| null` | no | `None` |  |
| `ci_high` | `float \| null` | no | `None` |  |

---

## Enums

### Direction

Which way a detection points, or a position faces.

| member | value |
|---|---|
| `BUY` | `buy` |
| `SELL` | `sell` |

### OrderType

Order kinds Aureon can place (§37, §42).

| member | value |
|---|---|
| `MARKET_BUY` | `market_buy` |
| `MARKET_SELL` | `market_sell` |
| `BUY_STOP` | `buy_stop` |
| `SELL_STOP` | `sell_stop` |
| `BUY_LIMIT` | `buy_limit` |
| `SELL_LIMIT` | `sell_limit` |

### FillingMode

Broker filling policy. Offered only when symbol_info allows it (§39).

| member | value |
|---|---|
| `FOK` | `fok` |
| `IOC` | `ioc` |
| `RETURN` | `return` |

### Timeframe

Candle timeframes, with the arithmetic the engines need (§7).

| member | value |
|---|---|
| `M1` | `M1` |
| `M5` | `M5` |
| `M15` | `M15` |
| `M30` | `M30` |
| `H1` | `H1` |
| `H4` | `H4` |
| `D1` | `D1` |

### MarketState

Tradability of a symbol right now (§10).

| member | value |
|---|---|
| `OPEN` | `open` |
| `CLOSED` | `closed` |
| `PREOPEN` | `preopen` |
| `STALE` | `stale` |
| `UNKNOWN` | `unknown` |

### SessionName

Trading sessions (§18). Boundaries live in aureon/config/sessions.py.

| member | value |
|---|---|
| `ASIA` | `asia` |
| `LONDON` | `london` |
| `NEW_YORK` | `new_york` |
| `OFF` | `off` |

### Freshness

How current a service's last write is (§59, §61-§63).

| member | value |
|---|---|
| `LIVE` | `live` |
| `STALE` | `stale` |
| `OFFLINE` | `offline` |

### TradeRequestStatus

Lifecycle of a human-initiated trade request (§25).

| member | value |
|---|---|
| `REQUESTED` | `requested` |
| `CONFIRMED` | `confirmed` |
| `EXECUTING` | `executing` |
| `PENDING` | `pending` |
| `PARTIALLY_FILLED` | `partially_filled` |
| `FILLED` | `filled` |
| `CANCELLED` | `cancelled` |
| `EXPIRED` | `expired` |
| `FAILED` | `failed` |
| `FAILED_STALE` | `failed_stale` |
| `FAILED_RECONCILIATION` | `failed_reconciliation` |

### TradeStatus

Lifecycle of an actual position. MT5 is the truth (§49-§53).

| member | value |
|---|---|
| `OPEN` | `open` |
| `PARTIALLY_CLOSED` | `partially_closed` |
| `CLOSED` | `closed` |

### TradeSource

Where a trade came from. External trades are imported, never managed (§52).

| member | value |
|---|---|
| `AUREON` | `aureon` |
| `EXTERNAL_MT5` | `external_mt5` |

### LinkType

How a trade came to be associated with a detection (§50).

| member | value |
|---|---|
| `EXPLICIT` | `explicit` |
| `INFERRED` | `inferred` |

### FailureCode

Why a request failed (§56, §57, §41, §78).

| member | value |
|---|---|
| `TRADING_DISABLED` | `trading_disabled` |
| `NOT_AUTHORIZED` | `not_authorized` |
| `LIVE_EXECUTION_NOT_ALLOWED` | `live_execution_not_allowed` |
| `MARKET_CLOSED` | `market_closed` |
| `SPREAD_LIMIT` | `spread_limit` |
| `STALE_QUOTE` | `stale_quote` |
| `DEVIATION_EXCEEDED` | `deviation_exceeded` |
| `CONFIRMATION_EXPIRED` | `confirmation_expired` |
| `SYMBOL_NOT_FOUND` | `symbol_not_found` |
| `SYMBOL_NOT_TRADEABLE` | `symbol_not_tradeable` |
| `VOLUME_INVALID` | `volume_invalid` |
| `MAX_LOT_EXCEEDED` | `max_lot_exceeded` |
| `STOPS_TOO_CLOSE` | `stops_too_close` |
| `INVALID_STOPS` | `invalid_stops` |
| `FILLING_MODE_UNSUPPORTED` | `filling_mode_unsupported` |
| `INSUFFICIENT_MARGIN` | `insufficient_margin` |
| `MAX_OPEN_POSITIONS` | `max_open_positions` |
| `MAX_DAILY_TRADES` | `max_daily_trades` |
| `BROKER_REJECTED` | `broker_rejected` |
| `REQUOTE` | `requote` |
| `CONNECTION_LOST` | `connection_lost` |
| `LEASE_EXPIRED` | `lease_expired` |
| `RECONCILIATION_NOT_FOUND` | `reconciliation_not_found` |
| `RECONCILIATION_AMBIGUOUS` | `reconciliation_ambiguous` |
| `UNKNOWN` | `unknown` |

### ControlRequestKind

A Discord-initiated action on something already live (§46, §47).

| member | value |
|---|---|
| `CANCEL` | `cancel` |
| `CLOSE` | `close` |

### ControlRequestStatus

Lifecycle of a control request. Mirrors the claim/lease pattern.

| member | value |
|---|---|
| `REQUESTED` | `requested` |
| `EXECUTING` | `executing` |
| `COMPLETED` | `completed` |
| `FAILED` | `failed` |
| `FAILED_STALE` | `failed_stale` |

### HorizonStatus

Whether a horizon's answer is known yet.

| member | value |
|---|---|
| `PENDING` | `pending` |
| `COMPLETE` | `complete` |
| `INVALID` | `invalid` |

### HorizonKind

How a horizon's end is defined (§21).

| member | value |
|---|---|
| `CANDLES` | `candles` |
| `MINUTES` | `minutes` |
| `SESSION_CLOSE` | `session_close` |
| `DAY_CLOSE` | `day_close` |
| `OPPOSITE_CROSS` | `opposite_cross` |

### ReferencePrice

Which price a horizon measures from (§21).

| member | value |
|---|---|
| `CLOSE` | `close` |
| `NEXT_OPEN` | `next_open` |

### ThresholdUnit

What a rule's thresholds are measured in (§21).

| member | value |
|---|---|
| `POINTS` | `points` |
| `PRICE` | `price` |

### PathClassification

Whether adversity or the target came first (§23).

| member | value |
|---|---|
| `MFE_FIRST` | `mfe_first` |
| `MAE_FIRST` | `mae_first` |
| `NONE` | `none` |

### TrendBias

What the last N closed candles did, summarised (9D).

| member | value |
|---|---|
| `BULLISH` | `bullish` |
| `BEARISH` | `bearish` |
| `SIDEWAYS` | `sideways` |

### ExcursionSource

Whether excursions were watched live or rebuilt afterwards (§45).

| member | value |
|---|---|
| `LIVE_TICKS` | `live_ticks` |
| `RECONSTRUCTED` | `reconstructed` |

### ExecutionClassification

How a human's execution compared with what the machine observed (§64).

| member | value |
|---|---|
| `TAKEN_AND_REACHED` | `taken_and_reached` |
| `TAKEN_AND_NOT_REACHED` | `taken_and_not_reached` |
| `MISSED_AND_REACHED` | `missed_and_reached` |
| `MISSED_AND_NOT_REACHED` | `missed_and_not_reached` |
| `DISCRETIONARY` | `discretionary` |
| `UNKNOWN` | `unknown` |

### DealEntry

MT5 deal entry type, used to match deals to positions (§36).

| member | value |
|---|---|
| `IN` | `in` |
| `OUT` | `out` |
| `INOUT` | `inout` |

---

## State machine

Every status write goes through `aureon.models.enums.assert_transition`
(CLAUDE.md). A status with no outgoing edges is terminal.

### TradeRequestStatus (§25, decision 1)

| from | to |
|---|---|
| `requested` | `cancelled`, `confirmed`, `expired` |
| `confirmed` | `cancelled`, `executing`, `failed`, `failed_stale` |
| `executing` | `failed`, `failed_reconciliation`, `filled`, `partially_filled`, `pending` |
| `pending` | `cancelled`, `expired`, `failed_reconciliation`, `filled`, `partially_filled` |
| `partially_filled` | `cancelled`, `expired`, `failed_reconciliation`, `filled`, `partially_filled` |
| `filled` | _terminal_ |
| `cancelled` | _terminal_ |
| `expired` | _terminal_ |
| `failed` | _terminal_ |
| `failed_stale` | _terminal_ |
| `failed_reconciliation` | _terminal_ |

### TradeStatus

| from | to |
|---|---|
| `open` | `closed`, `partially_closed` |
| `partially_closed` | `closed`, `partially_closed` |
| `closed` | _terminal_ |

### HorizonStatus (§22)

| from | to |
|---|---|
| `pending` | `complete`, `invalid` |
| `complete` | _terminal_ |
| `invalid` | _terminal_ |

### ControlRequestStatus (§46, §47)

| from | to |
|---|---|
| `requested` | `executing`, `failed_stale` |
| `executing` | `completed`, `failed` |
| `completed` | _terminal_ |
| `failed` | _terminal_ |
| `failed_stale` | _terminal_ |

---

## Identity (§12, §34)

Every id is a pure function of its inputs; nothing here reads a clock, a random
source or a config default. Generated from `aureon/models/identity.py`, so a
change to the recipe shows up as a diff here rather than as re-keyed data.

### `detection_id` components (§12, frozen)

sha256 over these, in this order, joined by `0x1F` (ASCII unit separator --
NOT `|`, which `event_key` legitimately contains):

| # | component |
|---|---|
| 1 | `account_scope` |
| 2 | `symbol` |
| 3 | `timeframe` |
| 4 | `candle_close_utc` |
| 5 | `agent_name` |
| 6 | `agent_version` |
| 7 | `event_key` |

`agent_version` is a component, so bumping an agent's version **forks** history:
the new version's detections sit beside the old version's for the same candles
rather than replacing them (decision 97). The timestamp is the candle **close**,
the instant the detection became knowable.

### `comment_token` (§34)

`AUR:` + first 6 base32 chars of
sha256(request_id) = 10 chars, inside MT5's 31-character
comment field (decision 5). Deterministic so an executor that crashed mid-send
re-derives exactly the token it stamped.

---

## Sessions

`SESSION_CONFIG_VERSION = 1` (§18, decision 7), stamped on
every detection so a later boundary change cannot silently reinterpret old data.

| session | window (market time) |
|---|---|
| `asia` | `02:00`–`10:00` |
| `london` | `10:00`–`18:00` |
| `new_york` | `15:00`–`23:00` |

Overlaps resolve by fixed precedence: `london` → `new_york` → `asia`.
