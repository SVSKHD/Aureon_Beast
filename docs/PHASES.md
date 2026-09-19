# Aureon — phase plan and gates

Source of truth for scope is `docs/ARCHITECTURE.md` (frozen, §94). This file
tracks only *which* phase is open and *what gate* closes it.

**Freeze rule (§94): do not start a phase until the previous phase's "Done when"
passes.** Partial passes do not count.

| Phase | Scope | Gate ("Done when") | Status |
|---|---|---|---|
| 0 | Repo scaffolding, standing rules, boundary guards | `pytest tests/boundary` green | ✅ done |
| 1 | `aureon/models`, `aureon/config`, `aureon/storage/paths.py`, `docs/CONTRACTS.md`, `docs/PHASE1_DECISIONS.md` | Contracts generated and green | ⛔ **blocked — not present in this repo** |
| 2 | Observer: market data, indicators, agents, engine, durable outbox | Parity + outbox + recovery tests pass; a full session of EMA crosses lands in `detections/` with correct ids; `docs/PHASE2_BASELINE.md` recorded | ⬜ not started |
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

## Why Phase 1 is marked blocked

Phases 2–8 are each written against `docs/ARCHITECTURE.md` section numbers and
each builds on the Phase 1 contracts. Neither is present in this repository — it
had no commits at all before Phase 0. See `docs/PHASE0_STATUS.md`.
