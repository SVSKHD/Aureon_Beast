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

Bootstrap or verify the local application database before the first run:

```bash
python scripts/setup_local_sqlite.py
```

That command creates missing tables/additive columns and reports the exact file, WAL mode,
foreign-key state, table count, and current detection/setup row counts. To verify an existing
database without changing its schema:

```bash
python scripts/setup_local_sqlite.py --check
```

The intended local settings are:

```env
AUREON_STORAGE_BACKEND=sqlite
AUREON_LOCAL_DB_PATH=data/aureon.db
```

Normal startup remains:

```bash
python main_aureon.py
```

When automatic updates are enabled, keep the live checkout on a clean `main` branch and verify
eligibility with:

```bash
python scripts/guardian_status.py
```

A merged change on `origin/main` is then eligible for the Runtime Guardian's normal
fetch → fast-forward → preflight → graceful restart flow.


After detections begin accumulating, inspect whether every agent is writing normalized
training evidence:

```bash
python scripts/report_agent_evidence.py --symbol XAUUSD
```

At end of a completed broker day, the review process also builds an immutable training-memory
snapshot automatically. It can be regenerated manually with:

```bash
python main_review.py training --symbol XAUUSD --date YYYY-MM-DD
```

The status is stored in SQLite and can be inspected from Discord with:

```text
/training-status symbol:XAUUSD
```

The primary daily target remains a favourable +$6 XAUUSD price move by EOD, while the
movement ladder also records +$20, +$40, the maximum favourable move after the frozen detection
reference, and how much farther price travelled after first reaching +$6. Training also records
MFE/MAE and, when stored timeframe bars can prove it, maximum adverse price excursion before the
first +$6 reach. This is research memory only and never changes execution rules automatically.

The normal weekly review now appends an agent/timeframe movement table. The same report is
available in Discord:

```text
/training-weekly symbol:XAUUSD
```

### Offline movement model, walk-forward backtest, and shadow inference

The first ML layer is a dependency-free logistic baseline trained only from completed,
versioned `training_examples`. It predicts three research targets:

```text
P(reaches $6 by EOD)
P(reaches $20 by EOD)
P(reaches $40 by EOD)
```

Train a candidate model:

```bash
python scripts/train_model.py --symbol XAUUSD
```

Run a chronological expanding-window backtest:

```bash
python scripts/backtest_model.py --symbol XAUUSD
```

Backtest that exact candidate artifact, then explicitly activate the same model id in
shadow mode:

```bash
python scripts/backtest_model.py --symbol XAUUSD --model-id <MODEL_ID>
python scripts/activate_shadow_model.py --model-id <MODEL_ID>
```

Activation is refused unless that exact model id has a completed walk-forward backtest.

A shadow model predicts only when a setup reaches CONFIRMED. It writes a
`model_predictions` row and has no path to trade requests, the executor, MT5, SL/TP, or
setup lifecycle decisions. At EOD the review process reconciles those predictions with the
stored $6/$20/$40 and excursion outcomes.

Discord status:

```text
/model-status symbol:XAUUSD
/backtest-status symbol:XAUUSD
```

`/model-status` separates the latest trained candidate from the active shadow model and
shows live prediction/reconciliation counts. Training metrics are explicitly in-sample;
`/backtest-status` is the out-of-sample chronological measurement used to judge whether
the model generalizes.

For each EMA/RSI/Trend/Wick/Liquidity/Breakout decision and timeframe it shows decision count,
aligned decisions, +$6/+20/+40 reaches, median/maximum favourable movement, and median extension
after +$6. Movement outcomes are credited only to decisions aligned with the setup direction.

This report is deliberately about **data readiness**, not profitability. It shows the
agent/version population, evidence coverage, and the numeric/categorical/boolean feature
keys actually present in SQLite. Outcomes stay separate in the evaluation tables so future
training cannot leak hindsight into detections.

## Running it

- `docs/RUNBOOK.md` — **start here to operate it.** What to type, what each tool refuses
  to do, and what to do when something is wrong at an hour when reading source is not an
  option.
- `docs/MT5_SESSION_CHECKLIST.md` — the manual half of one observation session.
- `docs/DEMO_EXECUTION_CHECKLIST.md` — the nine execution drills, and the three that can
  only be done by hand.
- `docs/evidence/` — one generated, committed file per session. Nothing here is written by
  hand.
