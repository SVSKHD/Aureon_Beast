# Aureon — PostgreSQL Migration Plan (final backend phase)

Frontend/Vue work is explicitly OUT OF SCOPE.

The backend must become: local-first; independent of Firebase/Firestore; resilient to internet/cloud outages; optimized around PostgreSQL locally; capable of optional weekly Supabase synchronization later; fully compatible with the existing MT5/Discord/execution architecture; setup-centric in Discord; explicit about execution failures; safe under unknown broker outcomes. Do not rebuild already-complete components unnecessarily.

## Core target architecture

All critical Aureon operations run locally on the Windows machine where MT5 is running.

```
MT5 → Aureon Backend → LOCAL PostgreSQL (application truth) + Local Parquet (candle/archive truth)
PostgreSQL feeds: Observer, Setup Engine, Executor, Position Monitor, Discord, Reviews, Ops, future API
Optional: LOCAL PostgreSQL → (weekly only) → Supabase
```

Supabase must NEVER be required for normal operation. If internet or Supabase is unavailable, observer, setup engine, executor, monitor, Discord, reviews, MT5, local Postgres and Parquet all continue. Only optional cloud sync waits.

## 1. Remove Firebase / Firestore completely
Remove: google-cloud-firestore runtime dependency, Firebase credentials, GOOGLE_APPLICATION_CREDENTIALS, AUREON_FIREBASE_PROJECT_ID, FIRESTORE_EMULATOR_HOST, Firestore preflight and round-trip checks, collection-prefix architecture, Firestore transaction dependencies, emulator as the main integration backend, Firebase client initialization. Do not remove domain models or semantics. Domains: detections, detection evaluations, setups, setup events, setup evaluations, sessions, trade requests, trades, control requests, audit logs, heartbeats, system state, settings, symbol specs, daily reviews, weekly reviews, notifications, alerts, assessments, trade notes, ops events, market days, market-day frames. Replace persistence only.

## 2. Source-of-truth rules
MT5 = broker truth. PostgreSQL = application truth. Parquet = raw/historical candle archive. SQLite outbox = local durable delivery/recovery queue. Never call Firestore application truth again.

## 3. Storage configuration
`AUREON_STORAGE_BACKEND=postgres`, `AUREON_DATABASE_URL=postgresql+psycopg://aureon:<password>@127.0.0.1:5432/aureon`. Local only, 127.0.0.1:5432, never exposed. Deployment: Windows PC with MT5, PostgreSQL, Aureon, Discord process, Parquet/SQLite outbox.

## 4. Storage abstraction
`aureon/storage/interfaces/`, `aureon/storage/postgres/{database.py, models.py, repositories/, migrations/}`, `aureon/storage/backend.py`. Services contain no SQL. Observer, executor, monitor, Discord, setup engine, reviews, ops depend on repositories via a backend factory: Service → Repository interface → Storage backend → PostgreSQL.

## 5. Postgres stack
SQLAlchemy 2.x, psycopg 3, Alembic. Pydantic domain models separate from SQLAlchemy persistence models.

## 6. Migrations
Alembic: initial schema, version tracking, preflight schema check (PASS migrations schema=current), future upgrades, tests against migration state. Outdated schema → FAIL safely with a remedy.

## 7. Schema design
Relational columns for filtering, ordering, identity, state machines, claims, time/date. JSONB for frozen market context, MTF context, setup reference blocks, metadata, research snapshots. Not everything as JSON.

## 8. detections
detection_id TEXT PK, account_scope, symbol, timeframe, agent, agent_version, event_key, market_date DATE, detected_at TIMESTAMPTZ, price NUMERIC, direction_context, context JSONB, mtf JSONB, created_at. Indexes: (symbol, detected_at), (symbol, market_date), (agent, detected_at), (market_date). Deterministic identity unchanged.

## 9. detection_evaluations
detection_id, rule_id, symbol, market_date, status, complete_horizons, mfe, mae, path data, result payload JSONB, updated_at. Unique (detection_id, rule_id). Frozen rule identity preserved.

## 10. setups
setup_id TEXT PK, account_scope, symbol, timeframe, family, direction_context, state, market_date, anchor_kind, anchor_price, invalidation_price, linked_detection_ids JSONB, context_summary JSONB, reference JSONB, confirmed_at, closed_at, created_at, updated_at, last_event_id, event_count. Indexes: (symbol, state), (symbol, market_date), (family, state), (updated_at). Deterministic setup ID preserved.

## 11. setup_events
event_id TEXT PK, setup_id FK, event_type, from_state, to_state, linked_detection_id, price, payload JSONB, created_at. Index (setup_id, created_at). Deterministic event IDs; replay/restart must not duplicate.

## 12. Setup transactions atomic
BEGIN → SELECT setup FOR UPDATE → check exists → check event_id absent → validate transition → INSERT event → UPDATE setup → COMMIT. Existing event → idempotent return, no re-apply. Racing writers must not corrupt state.

## 13. setup_evaluations
Unique (setup_id, rule_id); market_date, symbol, history source, payload, horizons, MFE/MAE, updated_at. Strict no-hindsight.

## 14. trade_requests
trade_request_id PK, symbol, direction, order_type, lot, entry_price, sl, tp, status, requested_by, linked_detection_id, linked_setup_id, requested_at, confirmed_at, claimed_at, claim_owner, claim_expires_at, broker_order_id, failure_code, broker_retcode, failure_detail, created_at, updated_at. Current state machine preserved.

## 15. Exactly-once executor claim
`SELECT … FROM trade_requests WHERE status='CONFIRMED' AND claimable ORDER BY confirmed_at FOR UPDATE SKIP LOCKED LIMIT 1`, then atomically move to EXECUTING/claimed. Two executors never execute the same request. Leases/idempotency/reconciliation semantics kept.

## 16. Unknown broker send
Uncertain result → DO NOT retry → status RECONCILING → reconcile against MT5. MT5 authoritative.

## 17. control_requests
Postgres, atomic claim semantics like trade requests (trading enable/disable, cancel, close, others). No duplicate control execution.

## 18. trades
Normalized lifecycle: broker IDs, request linkage, open/close state, price, lots, P&L, close reason, source incl. external_mt5, timestamps. Manual/mobile MT5 actions reconcile into Postgres.

## 19. Position monitor
MT5 + local Postgres, no cloud. Reduce repetitive scans; keep conservative periodic reconciliation.

## 20. heartbeats
Local, throttled: service, instance_id, updated_at, detail JSONB. Not synced by default.

## 21. system_state
Local snapshots per symbol/timeframe; candle close forces a write, otherwise throttled.

## 22. settings
Execution settings in PostgreSQL. trading_enabled runtime-controlled and fail-closed, not env-only. Live gate two-layer: AUREON_ALLOW_LIVE_EXECUTION=true AND DB trading_enabled=true.

## 23. Live account safety
REAL or UNKNOWN blocked unless AUREON_ALLOW_LIVE_EXECUTION=true. UNKNOWN conservative. Observer read-only; monitor read/reconcile.

## 24. MT5 connection
Default: attached terminal via mt5.initialize() then account_info(). AUREON_MT5_LOGIN/PASSWORD/SERVER/TERMINAL_PATH optional overrides. Preflight reports login, server, mode, currency, trade_allowed.

## 25. Remove Firebase credentials from .env
Delete GOOGLE_APPLICATION_CREDENTIALS, AUREON_FIREBASE_PROJECT_ID, FIRESTORE_EMULATOR_HOST. Add AUREON_STORAGE_BACKEND, AUREON_DATABASE_URL.

## 26. SQLite outbox remains
Observer → outbox → Postgres delivery → mark drained. Postgres down → detections do not disappear.

## 27. Local Postgres outage
NEW MONEY-MOVING OPERATIONS FAIL CLOSED. No order without a committed durable state transition. Observer may continue MT5 reading, Parquet archival, outbox accumulation. Ops `local_database_unavailable` once on onset, recovery once.

## 28. Supabase placeholder
AUREON_SUPABASE_SYNC_ENABLED=false, AUREON_SUPABASE_URL=, AUREON_SUPABASE_SERVICE_ROLE_KEY=, AUREON_SUPABASE_SYNC_DAY=SUNDAY, AUREON_SUPABASE_SYNC_HOUR=03. Default disabled; Aureon runs perfectly with all blank.

## 29. Weekly sync only
No polling, no Realtime, no event-by-event writes. Weekly, during confirmed closed-market period via existing sleep/market-state logic, not weekday alone.

## 30. sync_batches
batch_id PK, period_start, period_end, sync_type, status (PENDING|SYNCING|SYNCED|FAILED), created_at, started_at, completed_at, retry_count, row_counts JSONB, checksum, last_error. Weekly id like 2026-W39_WEEKLY. Restart must not duplicate a completed batch.

## 31. Sync content
May include detections, evaluations, setups, setup events, setup evaluations, sessions, trades, assessments, trade notes, daily/weekly reviews, market-day summaries, audit history. Never: raw Parquet, heartbeats, leases, process state, SQLite outbox, credentials, MT5 password, DB password, local paths.

## 32. Sync service
`aureon/sync/{__init__.py, models.py, batch_builder.py, weekly_sync.py, supabase_sync.py}`: disabled state, configured state, dry-run, deterministic batches, retry, checksums, error recording. Never sync with incomplete config.

## 33. Manual sync command
`python scripts/sync_supabase.py --dry-run`, later without flag, optional `--week 2026-W39`. Disabled → print "Supabase sync is disabled." without erroring as if core is broken.

## 34. Supabase failure
Never stops Aureon. Ops `weekly_supabase_sync_failed`. Local data untouched.

## 35. Preflight
PASS storage backend=postgres; PASS postgres_connection host=127.0.0.1 database=aureon; PASS migrations schema=current; PASS transaction_probe; PASS mt5_init; PASS mt5_account login/server/mode; INFO supabase_sync disabled|weekly configured. Postgres failure → preflight fails. Missing Supabase → never fails.

## 36. Main launcher
`python main_aureon.py`: load .env → preflight Postgres → preflight MT5 → start observer, monitor, executor, Discord, review watcher → optionally weekly sync scheduler. No internet required to start.

## 37. Reviews
All review storage/queries to PostgreSQL. Keep daily/weekly reviews, setup and assessment statistics, trade notes, history-source fields, real_days, synthetic/real/mixed labels. Statistical meaning unchanged.

## 38. Tuning
`scripts/tune_report.py --symbol --days` reads Postgres. No auto-apply. XAUUSD and XAGUSD separately calibrated.

## 39. Session evidence
`session_run.py`, `session_verify.py` on Postgres. SESSION VERIFIED strict: archive, gaps, live-vs-replay, persistence, outbox drained, duplicate IDs, full expected session, no missing/extra/mismatched detections. Not complete until actually run.

## 40. Demo drills
`demo_drills.py` on Postgres. DEMO only. No weakening.

## 41–46. Discord setup-centric
Primary intelligence = Setup + SetupEvents + frozen MarketContext + linked Detections + MTF. Raw detection alerts secondary. Cards show (only when values exist, never "— / —"): SETUP (family, state, symbol, timeframe, direction context, price); EMA (20, 50, gap, gap/ATR, slopes, compression, near-cross, cross); RSI (value, direction, momentum turn, 50-transition); LIQUIDITY (nearest level, distance, proximity, sweep, reclaim/rejection); BREAKOUT (level, distance, pressure, close beyond, acceptance, fakeout risk); TICK VOLUME (expansion ratio, reference, labelled MT5 tick volume); PROFILE (VAH, VAL, POC, nearest HVN/LVN, price vs VA); VOLATILITY (ATR14, relative, regime); MTF (M15, M30, H1, H4, D1, alignment). Bounded timeline from persisted events only. Notifications on WATCH, DEVELOPING, CONFIRMED, PULLBACK, CONTINUATION, FAKEOUT_RISK, INVALIDATED, COMPLETED with dedupe setup_id+event_id. Detection notifications remain secondary; preferences: setups only, detections only, both, agent filters, family filters. One notification system.

## 47–48. Chart renderer
`aureon/visuals/chart_renderer.py`: 80–120 candles; candlesticks, EMA20/50, setup anchor, invalidation, current price, confirmation/pullback markers, linked detections, session/swing levels, POC/VAH/VAL, nearest HVN/LVN; panels tick volume + RSI; XAUUSD and XAGUSD via symbol digits. Visual only — consumes stored data, never decides state, direction, validity, confirmation, execution.

## 49–52. Execution failure UX
Discord says WHY using existing FailureCode normalization + broker retcode + safe detail + request ID. Codes rendered: LIVE_EXECUTION_NOT_ALLOWED, TRADING_DISABLED, MARKET_CLOSED, INSUFFICIENT_MARGIN, INVALID_STOPS, STOPS_TOO_CLOSE, VOLUME_INVALID, STALE_QUOTE, REQUOTE, FILLING_MODE_UNSUPPORTED, CONNECTION_LOST, MAX_OPEN_POSITIONS, BROKER_REJECTED, REQUEST_EXPIRED, DUPLICATE/ALREADY_CLAIMED. Uncertain result → "⚠️ Execution outcome uncertain … Status: RECONCILING", never retried. Render existing states; no conflicting Discord-only state machine.

## 53–55. Wake-up
Result listener on local Postgres; LISTEN/NOTIFY (trade_request_confirmed, control_request_created, setup_event_created, notification_ready) as wake-up only; DB is truth; fallback scans every 30–60 s. No high-frequency cloud polling; Supabase untouched during daily operation.

## 56–58. Tests
Postgres replaces the emulator (Docker or temporary test DB; never production data). Required: schema migration, repository CRUD, deterministic IDs, duplicate detections, duplicate setup events, setup transition atomicity/rollback, two setup writers racing, trade atomic claim, two executors racing, expired lease, reconciliation, notification exactly-once, duplicate notification claim, control atomic claim, transaction rollback, DB reconnect, DB outage, outbox recovery, weekly sync batch idempotency. Boundary: observer/setup engine/chart renderer/watch events cannot import execution; Discord cannot import MetaTrader5; only approved modules import MetaTrader5; only storage layer contains DB implementation.

## 59–61. Still pending / out of scope
Real session testing for both symbols; real calibration (no auto-apply); no Vue, API, dashboard, browser auth, Supabase frontend, frontend realtime/charts.

## 62–63. Docs and tracking
Update README, .env.example, ARCHITECTURE.md, RUNBOOK.md, PHASES.md, CONTRACTS.md; PostgreSQL = application truth, Parquet = archive, SQLite outbox = durability, Supabase = optional weekly mirror. Keep pending: real XAU/XAG session verification, real setup evidence and tuning, XAG calibration, demo execution proof, live Discord demo proof, real sleep/wake proof. Code-complete ≠ real-world validated.

## 64. Acceptance criteria
1 starts with no Firebase creds; 2 Firestore not required; 3 local Postgres is application truth; 4 main_aureon.py starts everything; 5 Postgres preflight passes; 6 MT5 attach works; 7 detection persistence; 8 setup persistence; 9 setup events atomic+idempotent; 10 setup evaluations; 11 trade-request atomic claim; 12 exactly-once invariants green; 13 position reconciliation; 14 Discord on local Postgres; 15 setup cards show early EMA/RSI/liquidity/context; 16 no empty placeholders; 17 setup notifications exactly-once; 18 charts for both symbols; 19 normalized failure reasons; 20 unknown result → RECONCILING; 21 raw candles local Parquet; 22 SQLite outbox protects observer work; 23 DB failure → money ops fail closed; 24 Supabase disabled by default; 25 Supabase outage cannot break Aureon; 26 weekly batch generation; 27 dry-run; 28 unit tests; 29 boundary tests; 30 Postgres integration/failure-injection tests; 31 docs match; 32 frontend untouched.

## Final operating experience
Start local PostgreSQL → open MT5 → log in → `python main_aureon.py`. Banner: STORAGE PostgreSQL local PASS; DATABASE SCHEMA current PASS; MT5 connected PASS, account detected from active terminal; SUPABASE weekly sync DISABLED; SERVICES Observer/Monitor/Executor/Discord/Review RUNNING; on REAL without live execution enabled: Executor RECONCILE-ONLY / LIVE LOCKED.

## Final principle
Aureon operates indefinitely with Firebase, Supabase, internet and frontend unavailable, as long as MT5 is connected and local PostgreSQL is healthy. Cloud is optional sync/backup, never on the money path.

OBSERVE → UNDERSTAND → HUMAN DECIDES → EXECUTE → RECONCILE. Nothing bypasses this chain.
