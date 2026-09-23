# Aureon

Market observation, detection evaluation, and **human-confirmed** execution.

The central invariant: **a detection never creates a trade.** Observation and
execution are separate halves of the system, and the boundary between them is
enforced by tests, not by convention — see `tests/boundary/`.

## Layout

```
aureon/
  models/       pydantic contracts + status-transition assertions (Phase 1)
  config/       typed settings from the environment (Phase 1)
  storage/      local SQLite application storage + repository boundary
  data/         market data providers; mt5_provider.py may import MetaTrader5
  engine/       indicators, analysis/market/replay engines, level tracking
  agents/       pure detection agents: same window + params -> same detections
  outbox/       durable SQLite outbox; never drops a detection
  services/     market state, heartbeats
  evaluation/   detection outcomes, COMPLETE vs PENDING horizons (Phase 3)
  execution/    BrokerInterface, guard, worker, reconciliation (Phase 4)
  positions/    lifecycle; MT5 is the truth (Phase 5)
  discord/      human interface; reads local storage, never trades (Phase 6)
  reviews/      machine observation vs human execution, kept apart (Phase 7)
```

## Getting started

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env     # then fill it in; never commit .env
pytest
```

`MetaTrader5` is a Windows-only optional extra (`pip install -e ".[mt5]"`). It is
imported lazily inside the only two modules permitted to touch it, so the full
test suite runs on any OS against a fake broker.

## Rules and status

- `CLAUDE.md` — standing rules. Read before any change.
- `docs/PHASES.md` — phase plan, gates, the cross-phase checklist, and an **Evidence**
  column derived from the files that actually exist (`make phases`).
- `docs/PHASE0_STATUS.md` — what has landed and what Phases 2–8 are waiting on.

## Local storage

Aureon currently runs entirely on the local Windows machine. Application state is stored in `data/aureon.db` using SQLite WAL mode; raw candle history stays in Parquet and the existing `outbox.db` remains the durable delivery queue. Firebase/Firestore is not required. PostgreSQL is intentionally only a placeholder for the next storage decision.

Normal startup remains:

```bash
python main_aureon.py
```

## Running it

- `docs/RUNBOOK.md` — **start here to operate it.** What to type, what each tool refuses
  to do, and what to do when something is wrong at an hour when reading source is not an
  option.
- `docs/MT5_SESSION_CHECKLIST.md` — the manual half of one observation session.
- `docs/DEMO_EXECUTION_CHECKLIST.md` — the nine execution drills, and the three that can
  only be done by hand.
- `docs/evidence/` — one generated, committed file per session. Nothing here is written by
  hand.
