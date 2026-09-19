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

---

## Decisions added while implementing Phase 2

`docs/ARCHITECTURE.md` is still absent, so §13's exact wording was unavailable and
the EMA-cross rule below is the standard reading. Rows 29 and 31 are the two most
worth checking against the real §13, because they change detection counts.

| # | Spec | Decision |
|---|------|----------|
| 28 | §9 fixture provenance | The committed `XAUUSD_M5.csv` is **synthetic** (seeded random walk via `scripts/gen_fixtures.py`), as no real broker export was available — the spec permits this if stated. It deliberately spans a weekend, so the 49h discontinuity exercises gap handling and Phase 3's `INVALID` horizons rather than leaving them untested. |
| 29 | §13 `sequence_today` / `sequence_session` | Count **candles** within the broker day and session, not detections. The engine must build a context before knowing whether any agent will emit, so a per-agent detection count is not available at that moment; and it is recoverable by querying `detections`, whereas a candle index is deterministic under replay. |
| 30 | §79/§82 engine window | `AnalysisEngine`'s window length is **fixed** and derived from `max(agent.min_window())`, identical in live and replay. This is a **correctness** parameter, not a tuning knob: EMA and Wilder's RSI are recursive, so a 30-bar and a 200-bar window disagree on EMA(21) by enough to move a crossover onto a different candle. `test_a_shorter_window_breaks_parity_as_expected` pins it. |
| 31 | §13 what counts as a cross | A cross is a **change in the sign of `fast − slow`, ignoring bars where they are exactly equal**. Comparing only against the previous bar is wrong either way: treating equality as the opposite side invents a cross for `1, 2, 2, 1` against `slow = 2` (a touch from below that fell back), while requiring strict inequality misses `3, 2, 1` (a real cross passing through equality). Carrying the last non-zero side forward handles both. |
| 32 | §75 backfill warm-up | The observer's backfill reaches back a full `window_size` **before** the cursor, feeds those candles to rebuild the engine's window, and discards their detections. Without this a restart silently produced detections that never existed and missed ones that did — caught by `test_observer_recovery` and pinned there. |
| 33 | §13/§14 RSI's role | RSI is attached as **context only** and never gates a cross. Keeping the gate out means the stored detections record what the crossover rule saw, so "would RSI have helped?" stays answerable from `detection_evaluations` instead of being baked into what was recorded. |
| 34 | §10 weekly schedule | The weekly open/close (Sun 22:00 → Fri 21:00, plus a 60-minute `PREOPEN`) is expressed in **UTC**, deliberately separate from §18's session windows, which are market-local. The weekly boundary is a market-wide convention, not a property of whichever broker clock we read. This is what makes Sunday 21:30 UTC (`PREOPEN`) differ from 23:30 UTC (`OPEN`). |
| 35 | §79 agent window shape | Agents receive a pandas `DataFrame` with a UTC `DatetimeIndex` of candle **open** times and columns `open, high, low, close, tick_volume, real_volume`, oldest first, last row = the candle just closed. `validate_window` rejects anything else so a malformed window fails with a clear message rather than as a wrong indicator. |
| 36 | §83 outbox durability | SQLite in WAL mode with `synchronous=FULL`. The table's entire purpose is surviving an abrupt kill, so trading a real durability guarantee for write throughput on a few detections an hour would be a bad bargain. `delivered_at` is set strictly **after** the remote write returns. |

---

## Decisions added while implementing Phase 2 Part B

`docs/ARCHITECTURE.md` is still absent, so §14–§18 were unavailable and every agent
rule below is the standard reading. Rows 40, 41 and 44 are the ones most worth
checking against the real sections, because they change what is detected.

| # | Spec | Decision |
|---|------|----------|
| 37 | §79 engine windows | `AnalysisEngine` holds one buffer sized to the hungriest agent but hands **each agent exactly its own `min_window()`**. Without this, window dependence leaks between agents: registering the liquidity agent (583 bars) beside the cross agent (64) would widen the shared window and silently rewrite the cross agent's history, invalidating the recorded baseline just by adding a sibling. `test_an_agent_is_unaffected_by_its_siblings` pins it. |
| 38 | §14 RSI agent | Emits on zone **transitions** (`overbought_entry` / `_exit`, `oversold_entry` / `_exit`), not while a zone holds. RSI sits above 70 for long stretches; emitting throughout would produce hundreds of near-identical detections and bury the bar where the state actually changed. Context-only: `direction` is always `None`. |
| 39 | §18 session trend | Context-only (`direction=None`), although a session that closed higher has an obvious "up" direction. A session summary describes the **past**, and Phase 7's inferred-link rule matches on "same symbol, same direction, trade opened shortly after" (§50) — a directional session detection would attract spurious links to unrelated trades and quietly corrupt the review. The trend is carried as a label plus the numeric change instead. |
| 40 | §15 liquidity sweep | `event_key` is `"{direction}\|{level_type}"` where *direction* is **where price went** (`up` through a high), while `Detection.direction` is the **implied reversal** (an up-sweep is a SELL bias). The two deliberately disagree; conflating them is the easy mistake. This is a strategy claim rather than an observation, so it wants confirming against §15. |
| 41 | §17 breakout | `Detection.direction` **agrees** with the break direction — a continuation, not a reversal — which is the mirror of row 40. A breakout also requires the **previous close on the other side of the level**, so price holding above a level reports one breakout rather than one per bar for the rest of the session. |
| 42 | §15/§17 level timing | Levels are computed as of the **previous** bar. Otherwise a candle can sweep or break a level it helped create, which is trivially true and meaningless. |
| 43 | §15 swing definition | A swing is a fractal pivot: strictly greater (or less) than `strength` bars **on each side**. Strictly, so a flat double top is not a fresh pivot but an already-tested level. A pivot is only reported once `strength` bars have closed after it — reporting an unconfirmed one would use future information, since the bar might yet be exceeded and the "level" vanish. |
| 44 | §15/§17/§16/§18 thresholds | Penetration, rejection, close-beyond, wick ratios and the session flat band are **placeholders**, tuned only to be non-degenerate. They are deliberately permissive rather than restrictive: detections are immutable, cheap and evaluated in Phase 3, so recording a marginal sweep costs little while missing one is unrecoverable. Expect to retune against §15/§17 and against Phase 3's reached-N results. |
| 45 | §18 who writes `sessions/` | The **observer** writes them, not the agent: an agent that touched Firestore would break both the purity contract and the rule that only repositories write (CLAUDE.md). They also bypass the outbox, because a session summary is derived entirely from candles the observer re-processes on restart — a lost write regenerates, so failing loudly would stop observation for something recoverable. |
| 46 | §18 internal day keys | `LevelTracker` groups bars into broker days using **opaque integers** (local-midnight epoch), never formatted date strings — they are only ever compared for equality. Measured over the same workload: per-element `strftime` 1.6s, `DatetimeIndex.strftime` 4.9s, integer keys 0.25s. `tz_localize(None)` is applied first so a DST transition day cannot make the flooring ambiguous. |

---

## Decisions added while implementing Phase 3

`docs/ARCHITECTURE.md` is still absent, so §21–§23 were unavailable. Row 47 is the
one to read first: it is a finding about the specified thresholds, not a preference.

| # | Spec | Decision |
|---|------|----------|
| 47 | §21 threshold scale | Thresholds are in **points**, consistent with every other distance in the system. At XAUUSD's `point = 0.01` the specified 3/5/10/15/20 are $0.03–$0.20 — far inside a single M5 candle's ~$0.45–1.10 range — so **every threshold is reached on the first candle, in every horizon, 100% of the time**. The rule is implemented exactly as specified and the saturation is reported as a finding rather than quietly retuned: the rule is frozen by design, and the mechanism for a better scale is a new `rule_id`. The report and the baseline both flag it automatically. **Confirm the intended unit against §21 before drawing any conclusion from these numbers.** |
| 48 | §22 evaluation scope | Only detections **with a direction** are evaluated. Without one there is no favourable side, so "did it work?" has no meaning. Context-only detections (`rsi`, `session_trend`, `wick`) are still stored and still available to the reviews as context; the backfill reports how many were skipped for this reason. |
| 49 | §23 same-candle crossings | When the favourable and adverse thresholds are first crossed **within one candle**, their order is unobservable from candle data. §23's wording ("MAE_FIRST if adverse happened *before* favourable; MFE_FIRST otherwise") puts these in MFE_FIRST — the optimistic side. Rather than hide that, a new `path_ambiguous` flag records it so a review can exclude them. On the fixture this is 463 of 470 classifications, which is itself the clearest evidence for row 47. |
| 50 | §22 where evaluation runs | **Inside the observer process**, which the spec offers as one of two options. It needs exactly the closed-candle stream the observer already has; a separate process would mean a second subscription that could drift out of step with the one producing the detections. Finished evaluations are released from memory so a long-running observer does not accumulate them. Splitting it into `main_evaluator.py` later needs no change to the tracker. |
| 51 | §23 excursion timing | A candle's high and low happened *somewhere inside* it, and candle data cannot say when. So the time a threshold was reached is recorded as that candle's **close** — the first moment the crossing is actually known. Using the open would claim knowledge that did not yet exist and make every `time_to` optimistic. |
| 52 | §22 durability | Evaluations are written straight to Firestore, bypassing the outbox. An evaluation is a pure function of its detection and the candles that followed, so a lost write is regenerated by re-running the backfill — unlike a detection, which can never be re-derived once its candle has left the provider's history. |
| 53 | §23 MFE sign | `mfe` is the furthest price ran in the detection's favour and **may be negative**, meaning the best price ever offered was still worse than the reference. That is informative, so it is stored raw rather than clamped to zero. `mae` is its mirror and is normally negative. |
| 54 | §21 `opposite_cross` | Completed by a later detection from the **same agent, symbol and timeframe** with the opposite direction. An opposite-direction detection from a *different* agent is a different opinion, not this agent reversing, so it does not end the horizon. |

---

## Decisions added while implementing Phase 4 (the money phase)

`docs/ARCHITECTURE.md` was still absent, so **§41, §56 and §57 — the guard's bullet
list — could not be read**. Row 55 is the most important row in this document: it states
what the guard checks and why each rule is there, entirely as inference. A guard missing
a bullet is a money-losing bug, and only the frozen spec can say whether one is missing.

| # | Spec | Decision |
|---|------|----------|
| 55 | §41/§56/§57 guard rules | Seventeen rules, each its own function with its own test: `trading_enabled`, `symbol_allowed`, `confirmation_fresh`, `market_open`, `symbol_available`, `quote_available`, `quote_fresh`, `spread_within_limit`, `price_has_not_run_away`, `volume_valid`, `volume_within_max_lot`, `filling_mode_supported`, `stops_valid`, `pending_entry_valid`, `margin_sufficient`, `within_open_position_limit`, `within_daily_trade_limit`. Every "cannot tell" answer is a **refusal**, not a pass. **Reconcile this list against the real §56/§57/§41 before trading a funded account.** |
| 56 | §41 deviation | The guard **clamps** the requested deviation to `max_deviation_points` and never widens it. A request asking for more slippage tolerance than policy allows gets policy's. |
| 57 | §41 price drift | `price_has_not_run_away` compares the execution-time price against the **confirmed** quote and refuses only **adverse** movement beyond `max_deviation_points`. Refusing a fill that improved on what the human saw would be perverse. Distinct from the spread rule, which measures the book's width rather than its movement. |
| 58 | §30 unmapped broker retcodes | An unrecognised MT5 retcode is treated as a **rejection**, not an unknown outcome: the broker returned a code, so it did decide. Only the *absence* of an answer (a raised exception, a `None` result) leaves the request `EXECUTING`. Getting this backwards would either strand good requests or invite a re-send. |
| 59 | §35 reconciliation matching | Matched on magic + comment token + symbol + direction, with volume as an **upper bound within tolerance** rather than an equality — a partial fill is a legitimate match with a smaller volume, and insisting on equality would make every partial fill unfindable. Deals sharing a `position_id` are collapsed into one candidate, so a multi-deal fill does not look like multiple orders. |
| 60 | §35 ambiguity | More than one matching broker record → `FAILED_RECONCILIATION` with the candidates listed, never a guess. Two matching orders means something already went wrong, and picking one could attach the request to the wrong position. |
| 61 | §35 a vanished pending order | A `PENDING` order that is no longer at the broker and never filled resolves to **`CANCELLED`**, not `EXPIRED`. Claiming expiry would assert an expiry time we never observed. |
| 62 | §29 the stale-claim rollback | `claim` writes `FAILED_STALE` and **returns**; the refusal is raised by the caller *after* the commit. Raising inside the transaction aborts it and rolls the write back, leaving the request `CONFIRMED` and claimable later at a price the human never saw. This was a real bug, caught by scenario D. |
| 63 | §32 same-status resolves | `resolve` short-circuits only when there is genuinely **nothing to write**. An earlier version returned early on any unchanged status, silently discarding the `comment_token` the executor stamps before sending — leaving reconciliation nothing to search for. Also a real bug, caught by scenario A. |
| 64 | §87 test substrate | The failure-injection suite runs against the **real Firestore emulator**, via `make emulator` (Node + Java, no Docker) or `docker-compose.emulator.yml`. Exactly-once rests entirely on Firestore's transaction semantics, so an in-memory double would be testing my model of those semantics rather than the semantics. Without `FIRESTORE_EMULATOR_HOST` the suite **skips loudly** rather than passing vacuously. |
| 65 | §87 emulator behind a proxy | gRPC honours its own proxy variables, so a session with `HTTP(S)_PROXY` set cannot reach a localhost emulator (`Expected SETTINGS frame as the first frame`). `no_grpc_proxy=127.0.0.1,localhost` is the targeted bypass; the Makefile sets it. This disables nothing else. |
| 66 | §56 settings failure | `ExecutionSettingsRepository.read_or_default` returns `ExecutionSettings()` — i.e. `trading_enabled=False` — when the document is missing or unreadable. An absent settings document must not become an open trading bot. |

---

## Decisions added while implementing Phase 5

`docs/ARCHITECTURE.md` was still absent, so §36, §44, §45 and §52 are inferred.

| # | Spec | Decision |
|---|------|----------|
| 67 | §49 trade document id | `{account_scope}__{mt5_position_id}`, deterministic. The monitor re-sees every position on every poll and after every restart; a random id would accumulate one document per sighting. `account_scope` is included because two accounts can legitimately hold the same position id. |
| 68 | §44 realized P&L | The **sum of the deals' profit**, plus commission and swap, exactly as the broker reports them — never recomputed from prices and volumes. A recomputation would silently disagree with the account statement the moment a contract size, a currency conversion, or a two-price partial close is involved. The broker's number is the one the money followed. |
| 69 | §36 exit matching | Exits are matched on `mt5_position_id` + deal `entry` type, **never on the comment**. A closing deal often carries no comment, or one the broker wrote itself (`"sl 2398.00"`); matching exits by comment would miss every stop-loss and every close from a phone. The comment is still used for *entries* (Phase 4), where it is the only way to find an order whose result was lost. |
| 70 | §44 close price and reason | Taken from the **last** exit deal by time — the price the position finally left the market at. Deals are sorted before folding, because broker history can arrive out of order. |
| 71 | §44 incomplete exit evidence | A position gone from the broker's open list whose visible deals do **not** account for its full volume is recorded `PARTIALLY_CLOSED`, not `CLOSED`. `CLOSED` is terminal, so closing on partial evidence freezes a wrong `realized_pnl` that no later poll can correct. Caught by a test; a broker timestamp one second ahead of ours is enough to trigger it. |
| 72 | §44 deal window | The deal query reaches back `DEAL_OVERLAP_SECONDS` before the last sync **and 60s forward** of now. Backward because a deal can be published slightly out of order; forward because the broker's clock is not ours and a deal stamped ahead of us would otherwise be invisible for a whole poll — long enough to see only part of a multi-deal exit. Re-reading is free: every write here is idempotent. |
| 73 | §45 excursion units and side | Points, signed by the position's direction, so a BUY and a SELL are directly comparable — the same convention the detection evaluator uses. Measured at the **exit** side of the book (bid for a long, ask for a short), because that is the price the position could actually be closed at; the mid would flatter every excursion by half the spread. |
| 74 | §45 the weaker label wins | A position resuming live tracking over **reconstructed** figures keeps the `reconstructed` label. The record must describe the weakest measurement it contains, not the most recent one. Reconstruction also ignores candles outside the position's life, or someone else's price action gets attributed to this trade. |
| 75 | §45 first observation | The first observed price sets **both** extremes, so `mae` can legitimately be positive early on ("the worst it got was still in our favour") for the same reason `mfe` can be negative. Both are stored raw rather than clamped. |
| 76 | §52 what counts as external | No Aureon magic, **or** Aureon magic with no matching request → `source=external_mt5`, `trade_request_id=None`. The second case matters: our magic without a request means we cannot account for it, so it is observed and never managed. |
| 77 | §43 pending order resolution order | A **fill is checked first, always** — before expiry, before cancellation. An order can fill in the same instant a cancel is issued (Phase 4 scenario G), and recording `CANCELLED` for a position that actually exists would leave a live trade nobody is watching. That is the worst of the three mistakes by a wide margin. |
| 78 | §58 monitor independence | The monitor polls regardless of `trading_enabled`. That switch stops the executor; positions opened before it was thrown are still live and still need their closes recorded. A monitor that paused alongside the executor would blind the operator during exactly the incident that made them disable trading. A test asserts the monitor makes no order-placing broker call at all. |
