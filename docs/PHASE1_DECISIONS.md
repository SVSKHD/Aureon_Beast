# Phase 1 Decisions

Ambiguities in ARCHITECTURE.md resolved by the simplest reading. Each is reversible in Phase 1 without touching services.

| # | Spec | Decision |
|---|------|----------|
| 1 | §25 lists states but not every edge | `REQUESTED → CANCELLED/EXPIRED` (wizard abandoned or timed out); `CONFIRMED → CANCELLED` (requester cancels before the executor claims). Nothing may return to `REQUESTED` or `CONFIRMED`. `FAILED_RECONCILIATION` is terminal — manual repair is an operator action outside the state machine, audited separately. |
| 2 | §41 `FAILED_SPREAD_LIMIT` "possible state/error" | Modelled as `status=FAILED` + `failure_code=SPREAD_LIMIT`, not a separate state. Keeps the state graph small; `FailureCode` enum carries the specifics (§56, §78). |
| 3 | §12 hash separator | ASCII unit separator 0x1F, because `event_key` legitimately contains `\|`. |
| 4 | §12 `account_scope` | Config `AUREON_ACCOUNT_SCOPE`, default `"primary"`. Deliberately an alias, not the broker login, so a broker migration doesn't re-key history. **Set this before Phase 2 and never change it.** |
| 5 | §34 comment token | `AUR:` + 6 base32 chars from sha256(request_id) = 10 chars total; deterministic so a crashed executor can re-derive it. |
| 6 | §8 market timezone default | `Europe/Athens` (typical MT5 server clock, DST-aware). Override with `AUREON_MARKET_TZ`. |
| 7 | §18 session times | Placeholder defaults in `config/sessions.py` (Asia 02–10, London 10–18, NY 15–23 market time). Overlap resolved by fixed precedence London → NY → Asia. Bump `SESSION_CONFIG_VERSION` on any change. |
| 8 | §50 inferred links | `TradeRequest` and `Trade` reject `link_type=inferred`; inferred relationships live only in review documents (Phase 7). |
| 9 | §43 pending orders | No separate `pending_orders` collection; `PendingOrder` is a model the monitor uses over the `PENDING` trade request. Add a collection later only if queries need it. |
| 10 | §67 heartbeats | Stored both embedded in `system_state` (for one-read status) and in a `heartbeats/{service}` doc (source of truth the monitor writes). |
| 11 | §56 settings | Firestore `settings/execution` (`ExecutionSettings`) overrides env defaults at runtime; `trading_enabled` defaults to **false**. |
| 12 | §6 schema version | Single integer `schema_version = 1` on every document; per-collection versions can be introduced later without migration. |
| 13 | §22 evaluation status | `INVALID` reserved for horizons that cannot complete (e.g. data gap); a `COMPLETE` horizon must carry all excursion fields (validator). |
| 14 | §23 `reached_N` / `time_to_N` | Stored as `reached: dict[threshold, bool]` and `time_to: dict[threshold, seconds\|None]` rather than 10 named fields, so thresholds are configurable per rule. |
| 15 | Trade `close_reason` | Free string constrained by convention (`sl\|tp\|manual\|mobile\|broker\|discord\|unknown`); made an enum once Phase 5 shows what MT5 actually reports. |

## Config keys required by §84 — where they live

| Key | Location |
|-----|----------|
| confirmation_ttl_seconds, quote_ttl_seconds, status_stale_after_seconds | `AureonConfig` (env) and `ExecutionSettings` (Firestore, wins at runtime) |
| max_deviation_points, max_spread_points, executor_lease_seconds | same |
| agent_version, agent_params_snapshot | per-agent, stamped on each `Detection` |
| session_config_version | `config/sessions.py` |
| schema_version | `models/base.py` |
| evaluation_rule_id | `AureonConfig.evaluation_rule_id` |
| aureon_magic | `AureonConfig.aureon_magic` |

---

## Decisions added while implementing Phase 1

Same rule as above: simplest reading, reversible within Phase 1. Rows 17 and 22
are the two that want confirmation against the frozen spec, because they were
resolved without the section text in front of me.

| # | Spec | Decision |
|---|------|----------|
| 16 | §12 `detection_id` components | Hashed tuple is `(account_scope, symbol, timeframe, agent_name, event_key, candle_open_time)`. **`agent_version` is deliberately excluded**: a patch release re-running over history must upsert the SAME document, not mint a parallel detection for a candle that only happened once. The version is still stamped as a field, so "which build saw this" stays answerable. |
| 17 | §61/§63 review fields | Section text unavailable; modelled a superset — `detections_by_agent`, `detections_by_session`, `HorizonOutcome`/`ThresholdOutcome` per horizon and threshold, plus `pending_horizons_excluded` and `invalid_horizons_excluded`. **Confirm against §61/§63 before Phase 7 aggregates against it.** |
| 18 | §84 env parsing | An unset **or empty** variable falls back to its default (ordinary env convention). A **whitespace-only** `account_scope` is rejected instead, because it would key every detection id in history on `" "`. |
| 19 | §59 `system_state` location | Single document at `system_state/current` rather than one per symbol. Per-symbol state is the `symbols[]` array inside it, so `/status` and the dashboard render from one read (and one realtime listener). |
| 20 | §18 `sessions/` doc id | `{market_date}__{session}` — broker-local date, so re-running a session overwrites rather than duplicating. |
| 21 | §18 session boundaries | Windows are half-open `[start, end)`. With Asia ending and London starting at 10:00, 10:00 belongs to London alone; without this a boundary candle would fall in two sessions. |
| 22 | §22 `detection_id` length | Full sha256 hex (64 chars). Firestore's 1500-byte id limit is not a constraint, and truncating trades collision margin for cosmetics. |
| 23 | §23 hit rate with no data | `ThresholdOutcome.rate` returns `None`, not `0.0`, when nothing has completed. A rate of zero asserts the threshold was never reached — a finding; no data is not a finding. |
| 24 | §11 detection immutability | Enforced structurally: the model is `frozen=True` with `extra="forbid"`, so neither a mutation nor an added future-information field (`outcome_reached_5`) is possible without editing the model where review will see it. |
| 25 | §71 Discord authorization | Fails closed — an empty `AUREON_AUTHORIZED_USER_IDS` authorises **nobody**. Treating empty as "everyone" would turn a missing env var into an open trading bot. |
| 26 | §63 weekly review doc id | `{iso_year}-W{iso_week:02d}`, e.g. `2026-W03`, so ids sort chronologically as strings. |
| 27 | §21 threshold map keys | `threshold_key()` renders integral thresholds without a decimal point (`3`, not `3.0`), so `3` and `3.0` cannot become two buckets in the same `reached` map. |
