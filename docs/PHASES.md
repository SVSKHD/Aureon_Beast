# Aureon — phase plan and gates

Source of truth for scope is `docs/ARCHITECTURE.md` (frozen, §94). This file
tracks only *which* phase is open and *what gate* closes it.

**Freeze rule (§94): do not start a phase until the previous phase's "Done when"
passes.** Partial passes do not count.

| Phase | Scope | Gate ("Done when") | Status |
|---|---|---|---|
| 0 | Repo scaffolding, standing rules, boundary guards | `pytest tests/boundary` green | ✅ done |
| 1 | `aureon/models`, `aureon/config`, `aureon/storage/paths.py`, `docs/CONTRACTS.md`, `docs/PHASE1_DECISIONS.md` | Contracts generated, `--check` clean, suite green | ✅ done |
| 2 | Observer: market data, indicators, agents, engine, durable outbox | Parity + outbox + recovery tests pass; a full session of EMA crosses lands in `detections/` with correct ids; `docs/PHASE2_BASELINE.md` recorded | ⏭️ next |
| 3 | Detection evaluation (`EMA_OUTCOME_V1`, no hindsight) | Backfill over the Phase 2 week completes; reached-3/5/10 counted from COMPLETE horizons only, PENDING reported separately | ⬜ not started |
| 4 | Execution safety core — exactly-once | Failure-injection scenarios A–H + the seeded acceptance suite pass under the emulator; one demo market order and one pending order end to end | ⬜ not started |
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
