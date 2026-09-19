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
  storage/      the ONLY place Firestore clients are built (Phase 1+)
  data/         market data providers; mt5_provider.py may import MetaTrader5
  engine/       indicators, analysis/market/replay engines, level tracking
  agents/       pure detection agents: same window + params -> same detections
  outbox/       durable SQLite outbox; never drops a detection
  services/     market state, heartbeats
  evaluation/   detection outcomes, COMPLETE vs PENDING horizons (Phase 3)
  execution/    BrokerInterface, guard, worker, reconciliation (Phase 4)
  positions/    lifecycle; MT5 is the truth (Phase 5)
  discord/      human interface; reads Firestore, never trades (Phase 6)
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
- `docs/PHASES.md` — phase plan, gates, and the cross-phase checklist.
- `docs/PHASE0_STATUS.md` — what has landed and what Phases 2–8 are waiting on.
