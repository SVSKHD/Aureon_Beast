# Phase 0 status — what landed, and what Phases 2–8 are waiting on

## What this commit contains

Phase 0 is scaffolding only. It contains **no invented domain logic**: every
file here is fully determined by the standing rules or by module paths the phase
prompts name explicitly.

- `CLAUDE.md` — the standing rules, verbatim.
- `pyproject.toml` — Python 3.11+, pydantic v2, pytest, pandas, Firestore.
  Windows-only `MetaTrader5` and `discord.py` are **optional extras**, so the
  suite installs and runs on any OS.
- `.env.example` — every environment variable named anywhere in Phases 2–8,
  grouped by the phase that introduces it, with defaults where the prompts give
  them. No secrets.
- `.gitignore` — excludes `.env`, service-account JSON, and the local-only
  runtime state (`observer_state.json`, the SQLite outbox) that must never be
  shared or committed.
- Package skeleton under `aureon/` matching the module paths the phase prompts
  name, so later phases add files rather than inventing layout.
- `tests/boundary/test_architecture_boundaries.py` — six executable guards for
  the CLAUDE.md non-negotiables.

## The boundary guards

These are the "cross-phase checklist" greps, installed *before* the code they
guard. They are AST-based for imports rather than plain text greps, so a comment
mentioning `MetaTrader5` does not fail the suite while a real lazy
`import MetaTrader5` inside a forbidden function does.

| Guard | Enforces |
|---|---|
| `test_observer_side_never_imports_execution` | A detection never creates a trade — `agents/engine/outbox/data/evaluation/reviews` cannot import `aureon.execution` |
| `test_observer_side_has_no_order_placing_calls` | …and cannot reference `order_send` / `BrokerInterface` / `send_*_order` by any other route |
| `test_metatrader5_imported_only_in_the_two_permitted_modules` | Only `data/mt5_provider.py` and `execution/mt5_broker.py` may import MetaTrader5 |
| `test_permitted_mt5_modules_import_it_lazily` | Those two import it *inside a function*, so collection works off-Windows |
| `test_discord_never_touches_broker_or_indicators` | Discord reads Firestore only |
| `test_firestore_clients_are_built_only_in_storage` | No raw Firestore client outside `aureon/storage` |

Each guard was verified to fail against a deliberately planted violation and to
pass once removed — a guard that cannot fail is not a guard.

## Blocker: the frozen spec and Phase 1 are both absent

This repository had **zero commits and zero branches** before Phase 0. The phase
prompts assume two things that are therefore not available:

1. **`docs/ARCHITECTURE.md`** — the frozen spec. Every phase opens with "Read
   ARCHITECTURE.md §x–§y", and the work is specified *by section number*, not by
   description. Examples that cannot be resolved without it:
   - Phase 2 asks the EMA-cross agent to implement "exactly §13", and the
     session/liquidity/wick/breakout agents "§18 / §15 / §16 / §17".
   - Phase 4's execution guard must implement "every bullet of §56 + §57 + §41".
     A guard that is missing a bullet is a money-losing bug, and the bullets are
     only in that document.
   - Phase 5's close-reason derivation is "§36"; Phase 6's confirmation embed is
     "every field in §40"; Phase 7's review models are "the exact fields in
     §61/§63".
2. **Phase 1** — `aureon/models`, `aureon/config`, `aureon/storage/paths.py`,
   `docs/CONTRACTS.md`, `docs/PHASE1_DECISIONS.md`. Phase 2 imports
   `aureon.models.identity.detection_id` and `assert_transition` on its first
   line of real work.

Guessing either one would produce code that *looks* finished and silently
disagrees with the frozen spec — the worst outcome for a system whose Phase 4 is
described as "the money phase". Under the freeze rule (§94) the correct action is
to stop at the Phase 1 gate rather than build Phases 2–8 on an invented
foundation.

### What unblocks it

Either:

- **Commit `docs/ARCHITECTURE.md`** (plus Phase 1 if it exists elsewhere) to this
  branch, and Phase 2 Part A starts immediately against the real §13; or
- **Say to reconstruct Phase 1 from the phase prompts.** The prompts do name a
  large amount of the contract surface — `Detection`, `Candle`, `QuoteSnapshot`,
  `SymbolInfo`, `MarketTime`, `TradeRequestStatus`, `FailureCode`, the
  `assert_*_transition` helpers, `EvaluationRule` / `Horizon`,
  `BrokerOrderRequest` / `BrokerOrderResult`, `Trade`, `SystemState.freshness()`,
  `ExecutionSettings`, `AuditRecord`, `DailyReview` / `WeeklyReview`,
  `control_requests`. That is enough to build a *coherent* Phase 1, but its field
  sets would be inferred, not the frozen ones — so every inference would be
  logged in `docs/PHASE1_DECISIONS.md` and would need review against the real
  spec before Phase 4 touches money.
