# Aureon — phase plan and gates

Source of truth for scope is `docs/ARCHITECTURE.md` (frozen, §94). This file
tracks only *which* phase is open and *what gate* closes it.

**Freeze rule (§94): do not start a phase until the previous phase's "Done when"
passes.** Partial passes do not count.

| Phase | Scope | Gate ("Done when") | Status |
|---|---|---|---|
| 0 | Repo scaffolding, standing rules, boundary guards | `pytest tests/boundary` green | ✅ done |
| 1 | `aureon/models`, `aureon/config`, `aureon/storage/paths.py`, `docs/CONTRACTS.md`, `docs/PHASE1_DECISIONS.md` | Contracts generated, `--check` clean, suite green | ✅ done |
| 2 | Observer: market data, indicators, agents, engine, durable outbox | Parity + outbox + recovery tests pass; `docs/PHASE2_BASELINE.md` recorded | ✅ Parts A + B done (one MT5-only criterion outstanding) |
| 3 | Detection evaluation (`EMA_OUTCOME_V1`, no hindsight) | Backfill over the Phase 2 week completes; reached-3/5/10 counted from COMPLETE horizons only, PENDING reported separately | ✅ done (see the threshold-scale finding) |
| 4 | Execution safety core — exactly-once (**the money phase**) | Failure-injection scenarios A–H + the seeded acceptance suite pass under the emulator; one demo market order and one pending order end to end | ⬜ not started |
| 5 | Position lifecycle — MT5 is the truth | Demo: open via executor, close from mobile; `trades/` shows CLOSED with right close_price/close_reason/P&L within one poll | ⬜ not started |
| 6 | Discord — human interface over a safe backend | FOK trade and stop order placed from Discord; `/trading disable` blocks with a clear FAILED embed; every action audited | ⬜ not started |
| 7 | Reviews — machine observation vs human execution | `/status` on a CLOSED market renders the weekly review; baseline numbers reconcile | ⬜ not started |
| 8 | Aureon Vue — read-only dashboard | Dashboard matches `/status` at the same second; STALE banner on observer stop; non-allowlisted account sees nothing; rules tests pass | ⬜ not started |

## Cross-phase checklist (run after each phase)

- [ ] `pytest` green, **including** the boundary tests in `tests/boundary/`:
      no broker in the observer, no MetaTrader5 outside the two permitted
      modules, no Discord broker access, no Firestore client outside
      `aureon/storage`.
- [ ] `python scripts/gen_contracts.py` produces no diff — or the diff is
      committed together with the model change.
- [ ] `python scripts/gen_baseline.py` produces no diff — or the change to an
      agent, the indicators or the fixture that explains it is committed too.
- [ ] New env vars added to `.env.example`.
- [ ] New decisions appended to the decisions doc **with the spec §**.
- [ ] Nothing from a later phase was started early (§94).

## Standing caveat: `docs/ARCHITECTURE.md` is still absent

Phase 1 was built from the supplied `docs/PHASE1_DECISIONS.md`, which resolves the
spec's ambiguities by section number. The frozen spec itself is still not in this
repository, so anything the decisions doc does not cover was inferred and logged
as a new decision row (16–27).

Two rows want confirmation against the real section text before the phase that
depends on them:

- **row 17** — the `DailyReview` / `WeeklyReview` field sets, modelled as a
  superset because §61/§63 were unavailable. Confirm before Phase 7.
- **row 16** — `agent_version` excluded from `detection_id`. Confirm against §12
  before Phase 2 writes detections to a real project, since changing it later
  re-keys history.

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
