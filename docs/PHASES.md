# Aureon — phase plan and gates

Source of truth for scope is the frozen spec the `§` numbers cite, which the operator holds.
`docs/ARCHITECTURE.md` describes the system **as built** and says so in its first paragraph; it
is not that spec. This file tracks only *which* phase is open and *what gate* closes it.

**Freeze rule (§94): do not start a phase until the previous phase's "Done when"
passes.** Partial passes do not count.

| Phase | Scope | Gate ("Done when") | Status | Evidence |
| --- | --- | --- | --- | --- |
| 0 | Repo scaffolding, standing rules, boundary guards | `pytest tests/boundary` green | ✅ done | ✅ 19 boundary guards |
| 1 | `aureon/models`, `aureon/config`, `aureon/storage/paths.py`, `docs/CONTRACTS.md`, `docs/PHASE1_DECISIONS.md` | Contracts generated, `--check` clean, suite green | ✅ done | ✅ [CONTRACTS.md](CONTRACTS.md) |
| 2 | Observer: market data, indicators, agents, engine, durable outbox | Parity + outbox + recovery tests pass; `docs/PHASE2_BASELINE.md` recorded | ✅ Parts A + B done (one MT5-only criterion outstanding) | ✅ [PHASE2_BASELINE.md](PHASE2_BASELINE.md)<br>⬜ missing: verified real session for XAGUSD, XAUUSD (`scripts/session_run.py --symbol X, then scripts/session_verify.py --symbol X, for each symbol`) |
| 3 | Detection evaluation (`EMA_OUTCOME_V1`, no hindsight) | Backfill over the Phase 2 week completes; reached-3/5/10 counted from COMPLETE horizons only, PENDING reported separately | ✅ done (see the threshold-scale finding) | ✅ [XAU_OUTCOME_V2 outcomes](PHASE2_BASELINE.md) |
| 4 | Execution safety core — exactly-once (**the money phase**) | Failure-injection scenarios A–H + the seeded acceptance suite pass under the emulator; one demo market order and one pending order end to end | 🟡 code + emulator suite green; **demo-account leg outstanding** | ⬜ missing: drills on a demo account (`scripts/demo_drills.py --all --broker mt5 --evidence …`) |
| 5 | Position lifecycle — MT5 is the truth | Demo: open via executor, close from mobile; `trades/` shows CLOSED with right close_price/close_reason/P&L within one poll | 🟡 code + emulator suite green; **demo-account leg outstanding** | ⬜ missing: drills on a demo account (`scripts/demo_drills.py --all --broker mt5 --evidence …`) |
| 6 | Discord — human interface over a safe backend | FOK trade and stop order placed from Discord; `/trading disable` blocks with a clear FAILED embed; every action audited | 🟡 code + emulator suite green; **live-Discord leg outstanding** | ⬜ missing: live Discord session (`the Phase 6 leg of docs/DEMO_EXECUTION_CHECKLIST.md`) |
| 7 | Reviews — machine observation vs human execution | `/status` on a CLOSED market renders the weekly review; baseline numbers reconcile | ✅ done | ✅ [review reconciliation](PHASE2_BASELINE.md) |
| 8 | Aureon Vue — read-only dashboard | Dashboard matches `/status` at the same second; STALE banner on observer stop; non-allowlisted account sees nothing; rules tests pass | ⬜ not started | — |
| 9A | Two symbols in one deployment — XAUUSD + XAGUSD observed, stored, measured and reported identically; `/execute` | Both symbols run in one observer; `/execute xagusd buy 0.1` shows one embed and executes only after CONFIRM; `/status symbol:XAGUSD` renders live and weekly modes | 🟡 code + emulator suite green; **real-session leg outstanding** | ✅ [XAGUSD baseline](PHASE2_BASELINE.md)<br>⬜ missing: verified XAGUSD session (`scripts/session_run.py --symbol XAGUSD, then scripts/session_verify.py --symbol XAGUSD`) |
| 9B | Session tick-volume profile and volatility as context — no new directional detections, no trades | Replayed fixture weeks (both symbols) yield profile-tagged detections; `/status` shows the block; the baseline gets an "outcomes by tick-volume/volatility context" table per symbol | 🟡 code + fixture evidence; **real-session leg outstanding** | ✅ [outcomes by tick-volume/volatility context](PHASE2_BASELINE.md) |
| 9C | Alerts and notifications — `/remind price`, and detections announced in a channel with [Monitor] / [Execute] | An armed level fires **once** on the first quote beyond it, direct to whoever armed it, rendered from the snapshot frozen at the crossing; an enabled detection is announced once however many sweeps or restarts follow; [Execute] opens a lot modal and reaches the same CONFIRM as `/execute` | 🟡 code + emulator suite green; **real-session leg outstanding** | ✅ emulator: exactly-once across two bots and across a restart; [Execute] writes a REQUESTED document and the executor refuses it<br>⬜ missing: a real Discord gateway (no token or guild in this environment) |
| 9D | `/monitor` — the measured record on a detection — and `/note` | `/monitor` on a bullish London cross returns a bias with its evidence, a cohort of n≥30 with what was dropped to reach it, per-horizon confirmation with 95% intervals, and TP/SL quantiles, and stores the assessment; below thirty it says "insufficient history (n=…)" and publishes no rate; the weekly review shows `assessment_hit_rate` and the trader's notes | 🟡 code + emulator suite green; **real-session leg outstanding** | ✅ emulator: the observer's published trend read rendered by `/monitor`; the readout stored and scored; `/note` leaving a CLOSED trade byte for byte<br>⬜ missing: a cohort built from a REAL evaluated history rather than a synthetic one (needs the verified sessions 9A and 9B still owe) |
| 11D | Higher timeframes from the M5 already in hand, and a broker-day cache | M15/M30/H1/H4/D1 are aggregated from closed M5 with no extra broker call; a bucket is emitted only when complete and contiguous; every detection carries `mtf` and an `mtf_alignment`, and each finished broker day is cached as `market_days` + `market_day_frames` | ✅ code + unit, boundary and emulator suites green; agent versions bumped (ema_cross 2.2.0) so the populations fork | — |
| 11C | Evidence per symbol, a tuning report, and where a readout's history came from | Phase 2's Evidence cell goes green only when EVERY observed symbol has a verified session; `scripts/tune_report.py --symbol X --days N` writes `docs/TUNING_X.md` and has no `--apply`; every stored assessment records `history_source` and `real_days`, and `/monitor` says so above the numbers | ✅ code + unit and boundary suites green; **no symbol has a verified session**, so every cohort this repository can build is SYNTHETIC and every screen says so | — |
| 11A | Flaw register — credentials preflight, the live-account guard, tick-volume honesty, a generated collection registry, the docs checks and the named ops conditions | `scripts/preflight.py` names a missing service-account key as a missing key; a REAL or UNKNOWN terminal is refused by the guard's first rule unless `AUREON_ALLOW_LIVE_EXECUTION` is exactly `true`; `paths.ALL_COLLECTIONS` is generated and re-derived a second way by a test; `/ops` lists every named condition, each announced once on onset and once on recovery | ✅ code + unit and boundary suites green; **the frozen spec is still owed** — 12 T-2 added the as-built `docs/ARCHITECTURE.md`, which is a different document and says so | ✅ [ops register](../aureon/services/ops_events.py)<br>✅ [ARCHITECTURE.md (as built, not the frozen spec)](ARCHITECTURE.md) |
| 11B | Market-closed sleep and auto-wake — the four services stay up across the weekend and bring themselves back | Forty-eight simulated hours on the emulator: all four sleep at a confirmed close and wake before the open in order, zero restarts, Saturday `/status` renders the weekly review and names the next open | ✅ code + unit, boundary and emulator suites green; **no real weekend has passed** — only a deployed session can give one | ✅ 2 weekend cycle checks |
| 12 T-1 | One command starts the whole backend, and one dying child stops it | `python main_aureon.py` loads `.env` once, gates on the preflight and starts the five processes in §75 order; a child exiting logs `child_exited`, records `supervisor_stack_down` and takes the stack down non-zero; Ctrl+C interrupts in reverse order and waits `AUREON_SHUTDOWN_GRACE_SECONDS` so the observer drains its outbox | ✅ code + tests green, every process-shaped claim checked against real subprocesses | ✅ [launcher](../main_aureon.py)<br>✅ 28 real-subprocess launcher tests<br>⬜ missing: a real five-service start (`python main_aureon.py on the Windows VPS, with the terminal up`) |
| 12 T-2 | `docs/ARCHITECTURE.md`, written from the code | Process boundaries, the three data flows, broker truth vs application truth, what Discord may write, the claim-before-post tradeoff, the invariants, and an ownership table generated from `paths.py` via `aureon/storage/ownership.py`; a reader can answer "which process writes `trades`?" and "what stops a detection from trading?" from the document alone | ✅ every collection covered in both ARCHITECTURE.md and CONTRACTS.md by test; the 11A strict xfail is deleted because the file landed | ✅ [ARCHITECTURE.md (as built)](ARCHITECTURE.md)<br>✅ [ownership table](../aureon/storage/ownership.py)<br>⬜ missing: the frozen spec (`supplied by the operator; no artefact in this repository is it`) |
| 12 T-3 | Preflight tells the terminal's account from the config's, and each Firestore credential failure from the others | `mt5_account` PASSES with no MT5 variables set, naming the attached account, and FAILS on a mismatch when a login or server is pinned; a key for the wrong project, a non-service-account file, a permission denial and a missing key are four different rows with four different remedies; the emulator says so loudly with the prefix; the `config` row names the `.env` that was loaded | ✅ every branch has a test with fakes; no message in either module can be mistaken for an MT5 one | ✅ [credential diagnosis](../aureon/services/credentials.py)<br>✅ 19 one test per failure branch<br>⬜ missing: a preflight run against a real terminal (`python scripts/preflight.py on the Windows VPS, MT5 logged in`) |
| 12 T-4/T-5 | The real-session evidence gate, unchanged and now tested end to end | `session_verify.py` writes `SESSION VERIFIED` only on 0 missing / 0 extra / 0 changed detections, 0 outbox backlog, 0 duplicate ids and an archive count equal to the processed count; one changed detection leaves the marker out of the document and out of `verified_market_dates` | ✅ the gate is asserted on the written artefact, not on a check's status, in both directions | ⬜ **no real session has run.** `session_run.py` needs Windows and MT5; both symbols are still owed |
| 12 T-6 | The setup document, its deterministic id, and the transaction that moves state and history together | `setups/{setup_id}` plus a `setups/{setup_id}/events/{event_id}` sub-collection; nine-component id with the anchor price BINNED by the symbol's own bin size; every list capped; `assert_setup_transition` gating every state write; the event write idempotent on a deterministic event id so a restart or a replay is a no-op | ✅ 80 unit tests and 8 emulator tests, including two observers racing on one candle | ✅ [setup model](../aureon/models/setup.py)<br>⬜ nothing has produced a setup yet: the engine is T-7 |
| 12 T-7 | Four structures tracked across candles, and none of them able to trade | `aureon/services/setup_engine.py` runs in the observer on every closed candle; each family has its own lifecycle, invalidation and expiry from `symbol_tuning`, versioned; one open setup per (symbol, family, direction, anchor) — or per direction where the anchor moves; a confirmed setup is measured from its CONFIRMED candle by the SAME `OutcomeTracker` the detections use, into `setup_evaluations` | ✅ 31 scripted-path tests, 17 plants all caught, boundary suite green (the engine imports nothing from `aureon.execution`) | ✅ [setup engine](../aureon/services/setup_engine.py)<br>⬜ no setup has ever been produced from real bars: that needs a real session |
| 12 T-8 | Seventeen descriptive observations, recorded and never acted on | `aureon/engine/watch_events.py` emits `WATCH_*` setup events for proximity, EMA gap and slope, RSI turns, tick-volume expansion, profile reclaim/rejection and breakout pressure; distances are ATR multiples from the versioned setup tuning; none of them changes a state, and the model refuses one that tries | ✅ 45 tests including the forbidden-vocabulary check over every module and document | ✅ [watch events](../aureon/engine/watch_events.py)<br>⬜ every threshold is a placeholder: no real session has produced one of these |
| 12 T-9 | Setups carry a measured reference, and a week counts them | `aureon/services/setup_reference.py` builds a cohort of past `setup_evaluations` from strictly earlier broker days using `/monitor`'s own quantile, Wilson, cohort-floor and maturity machinery; the block publishes points and never a price, carries its caption on every rendering, and is measured at open and never rewritten. `confirmed_at` makes "did it ever make a claim" answerable. The weekly review gains a setups section | ✅ 30 tests in `test_setup_reference.py`, 11 in the review's setups section, 3 new boundary tests | ✅ [setup reference](../aureon/services/setup_reference.py)<br>⬜ every cohort is SYNTHETIC and immature: no session has been verified, so `history_source` says so on every block |
| — | **Corrections slice** (§12 identity, EMA 20/50, outcome V2, context tags, live snapshot, safety gaps, collection prefix, live-vs-replay tooling) | pytest green with and without the emulator; baseline carries a 20/50 section; `/status` shows the full snapshot; boundary tests cover identity components, bare collection literals and raw Firestore access | ✅ code done; **real-session leg outstanding** | ⬜ missing: verified real session (`scripts/session_run.py, then scripts/session_verify.py`) |
| — | **Defect register D-1…D-15** | each item green | ✅ closed — D-1…D-3, D-5…D-14 landed in the corrections slice (PR #1); D-4 needed only its missing proof (the tracker already filtered on direction, not agent name); D-7 needed its last label (`last updated`); D-15 recorded as placeholders with a per-symbol hook | ✅ [decisions 118–120](PHASE1_DECISIONS.md) |

## Cross-phase checklist (run after each phase)

- [ ] `pytest` green, **including** the boundary tests in `tests/boundary/`:
      no broker in the observer, no MetaTrader5 outside the two permitted
      modules, no Discord broker access, no Firestore client outside
      `aureon/storage`.
- [ ] `python scripts/gen_contracts.py` produces no diff — or the diff is
      committed together with the model change.
- [ ] `python scripts/gen_baseline.py` produces no diff — or the change to an
      agent, the indicators or the fixture that explains it is committed too.
- [ ] `python scripts/update_phases.py --check` clean, so the Evidence column
      above describes the files that are actually in the repository. It never
      touches Status: whether a partial pass counts is a judgement, and a script
      inferring that from file existence would be making it silently.
- [ ] New env vars added to `.env.example`.
- [ ] New decisions appended to the decisions doc **with the spec §**.
- [ ] Nothing from a later phase was started early (§94).

## Standing caveat: the frozen spec is still absent

`docs/ARCHITECTURE.md` now exists, but it is the **as-built** document written from the code
(12, T-2) — not the frozen spec the `§` numbers point at. That spec is still not in this
repository, and nothing in this repository can stand in for it: the as-built document can say
what the code does and cannot say what the spec required.

Phase 1 was built from the supplied `docs/PHASE1_DECISIONS.md`, which resolves the spec's
ambiguities by section number. Anything the decisions doc does not cover was inferred and logged
as a new decision row (16–27).

One row still wants confirmation against the real section text:

- **row 17** — the `DailyReview` / `WeeklyReview` field sets, modelled as a
  superset because §61/§63 were unavailable. Phase 7 has now been built on that
  superset, so the fields are exercised and reconciled but still unratified: a
  §61/§63 that names *fewer* fields would leave harmless extras, one that names a
  field this superset lacks would be a gap. Still worth confirming.

Row 16 — the caveat about the agent version and the detection id — is **resolved and no
longer a caveat**. The corrections slice put `agent_version` back into the identity hash (decision 120,
`DETECTION_ID_COMPONENTS`), which is why a tuning change or an agent bump forks the population
instead of silently re-keying it. The note asking somebody to confirm it stayed here for four
phases after the thing it described had been fixed, which is the drift `docs/DOCS_CHECK.md` and
`tests/unit/test_docs_consistency.py` now exist to catch (11A, F-11).

Everything else in Phase 1 is pinned by a test, so a correction surfaces as a
failing test rather than as silent drift.

## Phase 2 Part A — what landed, and what Part B still needs

Part A is the EMA-cross vertical slice, end to end: market data providers (MT5 +
historical), pure indicators, the `ema_cross` agent, one `AnalysisEngine` shared by
live and replay, the durable SQLite outbox and its worker, the Firestore
repositories, market-state and heartbeat services, and `main_observer.py` with the
§75 startup sequence.

Its gate is met except for the one criterion that needs a broker: *"`main_observer.py`
runs for one full session against MT5"*. Everything else the gate asks for is green
against the fixture and a live-shaped fake feed. That criterion is flagged rather
than quietly treated as passed.

**Part B (done):** `rsi_agent`, `session_trend_agent`, `wick_agent`,
`liquidity_agent` and `breakout_agent`, plus the shared `LevelTracker` in
`aureon/engine/levels.py`. The liquidity and breakout agents are handed the **same**
tracker instance and a test asserts it — two trackers would be two implementations of
where a level is, and the agents would disagree about the same bar.

Every agent has a replay/live parity test, and a second test asserts that registering
an agent never changes what another agent detects (decision 37).

One criterion of the Phase 2 gate remains outstanding and is **not** treated as
passed: *"`main_observer.py` runs for one full session against MT5"*. It needs a
Windows terminal and a broker. Everything else in the gate is green against the
fixture and a live-shaped fake feed.

## Phase 3 — the threshold-scale finding

Phase 3's gate is met: the backfill completes over the Phase 2 week, and reached-N is
counted from `complete_horizons` only with PENDING and INVALID reported separately.

But the numbers it produces are **not usable as evidence about the strategy**, and the
report says so itself. `EMA_OUTCOME_V1`'s thresholds of 3/5/10/15/20 points are
$0.03–$0.20 at XAUUSD's `point = 0.01`, well inside a single M5 candle's range. So:

- every threshold is reached 100% of the time, in every horizon;
- 463 of 470 path classifications are `path_ambiguous` — the favourable and adverse
  thresholds were first crossed in the *same* candle, so their order was never
  observed.

The machinery is behaving correctly; the scale is measuring nothing. Because a rule is
frozen once shipped (§21), the correction is a **new `rule_id`**, never an edit — that
is exactly the mechanism the freeze exists to provide. Confirm the intended unit
against §21 before drawing conclusions from the recorded numbers (decision 47).

## Phase 4 — what is proven, and what is not

**Proven, against the real Firestore emulator:** scenarios A–H, plus the §81 acceptance
suite — 50 requests per run across 20 seeds, each request independently assigned a
failure mode, 1000 requests in total. Every one ends with at most one order at the
broker, and none is left in `EXECUTING`. The ledger inside `FakeBroker` outlives a
simulated restart, so "exactly once across two process lifetimes" is a claim the tests
actually make rather than assume.

Two real bugs were caught by those tests and are recorded as decisions 62 and 63:

- `claim` wrote `FAILED_STALE` and then **raised inside the Firestore transaction**,
  which rolled the write back. A request whose confirmation had expired stayed
  `CONFIRMED` and could be claimed later at a price the human never saw.
- `resolve` returned early whenever the status was unchanged, silently discarding the
  `comment_token` the executor stamps before sending — leaving reconciliation nothing to
  search for.

**Not proven, and not claimed:**

1. **The demo-account leg of the gate.** "One real MT5 demo market order and one pending
   order through `main_executor.py`" needs a Windows terminal and a broker login.
   `MT5Broker` is written and its retcode mapping is unit-tested, but it has never
   exchanged a byte with a real terminal.
2. **The guard's completeness.** §41/§56/§57 were unavailable, so the seventeen rules in
   decision 55 are inferred. The machinery around them is sound — a refused request
   provably never reaches the broker — but whether the *list* is complete is a question
   only the frozen spec can answer. **This is the one thing to resolve before a funded
   account.**

## Phase 5 — what is proven, and what is not

**Proven, against the emulator and FakeBroker:** every scenario the phase names — fill
then SL close, TP close, a close that happened while the monitor was down (reconstructed
from deals alone), partial close 0.25→0.10 and then to fully closed, an externally opened
position imported, a pending order that filled while offline, realized P&L equal to the
sum of the closing deals, MFE/MAE over a scripted price path, and reconstructed excursions
flagged as such. Plus §58: the monitor keeps recording with `trading_enabled=false`, and a
test asserts it never makes an order-placing broker call.

One real bug was caught and is recorded as decision 71: a position gone from the broker's
open list whose visible deals covered only *part* of its volume was marked `CLOSED` with a
partial `realized_pnl`. Since `CLOSED` is terminal, that wrong number could never be
corrected. A broker timestamp one second ahead of ours is enough to cause it. It now
records `PARTIALLY_CLOSED` and finishes once the remaining deals appear.

**Not proven, and not claimed:** the demo-account leg of the gate — open via the executor,
close from the MT5 mobile app, and see `trades/` show CLOSED with the right close_price,
close_reason and P&L within one poll; likewise for an SL hit and a position opened by hand
in the terminal. All three need a Windows terminal and a broker login. The code paths they
exercise are covered by the emulator suite against a fake broker, but no byte has been
exchanged with a real MT5 terminal.

## Phase 6 — what is proven, and what is not

**Proven.** All Discord logic lives in `service.py`, which imports no discord.py, so the
rules that protect real money are unit-tested directly: an unauthorized user is rejected,
the wrong user clicking CONFIRM is rejected, a stale quote re-prompts rather than refusing,
a double confirm produces one execution, and `/status` renders STALE at 46 seconds. Against
the emulator: a Discord draft becomes a real execution, `/trading disable` blocks the very
next request with `trading_disabled` and nothing reaches the broker, and a cancel that lost
its race to a fill reports FAILED rather than lying about it.

Two new boundary guards: Discord holds no broker or provider field on its context, and
cannot import the repositories that write detections, evaluations, sessions or reviews.
Both were verified to fail against a planted violation.

Two constraint contradictions had to be resolved rather than deferred (decisions 79, 80):
§39 and §42 require broker symbol metadata in Discord, and §40 and §27 require a quote —
while Discord may not call the broker and tick data may not go to Firestore. The observer
now publishes `symbol_specs/{symbol}` and a single `last_quote` snapshot in `system_state`.
Both are state, not streams, and both are advisory: the execution guard re-reads live
values regardless.

**Not proven, and not claimed:** the gate's live leg — placing a FOK trade and a stop order
*from Discord* on the demo account and seeing the result appear within seconds. That needs
a Discord application, a guild, a bot token and an MT5 terminal. No gateway connection has
been made; `bot.py`, the commands and the views have never rendered in a real client.

## Phase 7 — what is proven, and what is not

**Proven.** The gate, both halves. `/status` on a CLOSED market renders the stored weekly
review, read through `ReviewReader` — which has no write method at all, so the human
interface cannot rewrite the figures it displays. And `docs/PHASE2_BASELINE.md` now carries
a **Weekly review reconciliation** section: weekly reviews generated from the same replay
agree with the baseline's per-day detection counts, per-horizon COMPLETE counts, reached-3
counts, and both excluded counts — 70 detections, 55 in week 38 and 15 in week 39, 13
PENDING and 7 INVALID horizons excluded. `scripts/gen_baseline.py` refuses to write the
document when they disagree.

The reconciliation is two genuinely independent paths to the same number, and that was
verified by breaking each: bounding the week in UTC instead of on the broker clock moves one
detection between weeks and is caught by name, while the grand total stays right; folding
PENDING horizons into the denominator is caught on four horizons at once. Neither would have
looked anomalous in its own output.

Two boundary guards, both verified against a planted violation. An AST-precise one forbids
`aureon/reviews` from reading `.horizons` directly — precise rather than textual because the
first version produced seven false positives on docstrings and `rule.horizons`, and the fix
was to sharpen the guard rather than weaken it. The second denies the package any repository
that writes detections, trades, evaluations or sessions, so §50 rests on a capability the
code does not have rather than on a behavioural test's chosen path.

The rule that governs the whole package: reached counts come from `complete_horizons` only.
A detection whose horizon is still PENDING classifies as `UNKNOWN`, never as a miss, and
both excluded counts ride on every review. Inferred links live only on the review, require
all four criteria, and never mutate a trade — a test asserts a trade cannot carry an
inferred link at all. Reviews are idempotent: the same period overwrites the same document
id with byte-identical content, verified across re-runs, under input reordering, and through
Firestore.

One real bug, found by testing the CLI's own defaults rather than its output: the weekly
period defaulted to "seven days ago", which on Friday, Saturday and Sunday names the week
*before* the one that just traded. §63 asks for the review to be generated after Friday's
close — precisely when the old rule was wrong — so the cron the spec describes would have
produced last week's numbers under this week's heading. It now walks back to the most recent
completed Friday, with every weekday pinned and a year-long invariant that the default never
names a week still trading (decision 96).

Against the emulator: a review round-trips with its tuples, enum-keyed dicts and tz-aware
timestamps intact; the exact broker-midnight boundary is half-open (21:00 UTC belongs to the
next day, 20:59 to the previous); regeneration picks up a late detection; and `/status`
picks the *latest* weekly review rather than merely a weekly review — that last test exists
because reverting `max` to `min` in the reader passed the whole suite until it was added.

**Not proven, and not claimed:** nothing has been generated from live trading data. Every
review in this repository is built from the synthetic fixture or from documents a test
wrote, so the linking heuristic has never been shown a real human's trade. Its four
criteria are a reasonable rank, not a calibrated one, and `confidence` is explicitly
documented as an ordering, not a probability. Whether §61/§63 want these exact fields is
still unconfirmed (see the standing caveat above).

## Phase 2 real-session evidence

**Empty. Nothing has run against a real MT5 terminal.**

The tooling is in place and tested — `LiveCandleArchive` records the candles the observer
processed, and `scripts/compare_live_vs_replay.py` replays exactly those and diffs the
result against what reached Firestore, exiting non-zero on any difference. What is missing
is a session: this needs Windows and a running terminal, neither of which exists in this
environment.

`docs/MT5_SESSION_CHECKLIST.md` is the procedure, and it is now bracketed by two tools
rather than by a paragraph asking for a paste:

    python scripts/session_run.py            # preflight, then the observer
    python scripts/session_verify.py <date>  # six checks, into the evidence file

They write `docs/evidence/session_{market_date}_{symbol}.md`, carrying the market date, the terminal
build, the broker server, the collection prefix, the commit **and whether the tree was
dirty**, the comparison's full output and the day's outcomes. The commit hash matters most —
a comparison is evidence about one build, and without it the output is an anecdote about an
unknown one, which is why the tool records it rather than asking for it.

The **Evidence** column above is derived from that directory by
`python scripts/update_phases.py`: it says `⬜ missing` until a file exists that contains
`SESSION VERIFIED`, and it cannot be talked into saying otherwise.

Until then, every parity claim in this repository is a claim about the **engine** given a
fixture, not about the engine and a broker agreeing on what a candle is.
