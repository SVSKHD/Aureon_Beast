# Phase 2 baseline

**Generated — do not edit by hand.** Run `python scripts/gen_baseline.py`.

Counts from replaying the committed fixture through the observer's agents.
Phase 3 reconciles its evaluation counts against these, and Phase 7's weekly
review must agree with them for the same week. A change here without a
corresponding change to an agent, the indicators or the fixture is a bug.

## Input

| property | value |
|---|---|
| fixture | `aureon/data/fixtures/XAUUSD_M5.csv` (synthetic, decision 28) |
| candles | 1884 |
| symbol / timeframe | XAUUSD M5 |
| first candle open | `2026-09-13T22:00:00+00:00` |
| last candle open | `2026-09-22T11:55:00+00:00` |
| market timezone | `Europe/Athens` |
| account scope | `primary` |
| session config version | 1 |
| gaps > 1 candle | 1 |

Gaps (weekend discontinuity):

- `2026-09-18T20:55:00+00:00` → `2026-09-20T22:00:00+00:00` (49.1h)

## Agent configuration

| property | value |
|---|---|
| agent | `ema_cross` v1.0.0 |
| fast / slow EMA | 9 / 21 |
| RSI period | 14 (context only, never a gate) |
| warm-up bars | 63 (slow × 3) |
| engine window | 583 bars (fixed; parity contract) |

## Detections

| metric | count |
|---|---|
| total | 70 |
| bullish crosses | 35 |
| bearish crosses | 35 |
| unique detection ids | 70 |

### By session

| session | detections |
|---|---|
| `asia` | 16 |
| `london` | 31 |
| `new_york` | 12 |
| `off` | 11 |

### By broker trading day

| market date | detections |
|---|---|
| `2026-09-14` | 10 |
| `2026-09-15` | 12 |
| `2026-09-16` | 11 |
| `2026-09-17` | 16 |
| `2026-09-18` | 6 |
| `2026-09-21` | 8 |
| `2026-09-22` | 7 |

---

## All agents

Counts from one run with the whole roster registered. Because the engine gives
each agent its own window slice, these are identical to running each agent
alone -- adding an agent never changes another's output.

| agent | version | window | detections |
|---|---|---|---|
| `ema_cross` | 1.0.0 | 64 | 70 |
| `rsi` | 1.0.0 | 44 | 110 |
| `session_trend` | 1.0.0 | 98 | 19 |
| `wick` | 1.0.0 | 1 | 235 |
| `liquidity` | 1.0.0 | 583 | 528 |
| `breakout` | 1.0.0 | 583 | 251 |

### Crosses, sweeps and breakouts per session

The three counts the Phase 2 gate asks to be recorded.

| session | crosses | sweeps | breakouts |
|---|---|---|---|
| `asia` | 16 | 137 | 59 |
| `london` | 31 | 220 | 110 |
| `new_york` | 12 | 124 | 66 |
| `off` | 11 | 47 | 16 |
| **total** | **70** | **528** | **251** |

### Liquidity sweeps by level

| event_key | count |
|---|---|
| `down|asia_low` | 28 |
| `down|london_low` | 19 |
| `down|previous_day_low` | 31 |
| `down|previous_session_low` | 68 |
| `down|swing_low` | 98 |
| `up|asia_high` | 51 |
| `up|london_high` | 10 |
| `up|previous_day_high` | 4 |
| `up|previous_session_high` | 65 |
| `up|swing_high` | 154 |

### Breakouts by level

| event_key | count |
|---|---|
| `down|asia_low` | 14 |
| `down|london_low` | 11 |
| `down|previous_day_low` | 11 |
| `down|previous_session_low` | 35 |
| `down|swing_low` | 66 |
| `up|asia_high` | 19 |
| `up|london_high` | 7 |
| `up|previous_day_high` | 2 |
| `up|previous_session_high` | 26 |
| `up|swing_high` | 60 |

### RSI zone transitions

| event_key | count |
|---|---|
| `overbought_entry` | 21 |
| `overbought_exit` | 25 |
| `oversold_entry` | 32 |
| `oversold_exit` | 32 |

### Session trends

| event_key | count |
|---|---|
| `asia|down` | 2 |
| `asia|up` | 5 |
| `london|down` | 3 |
| `london|flat` | 1 |
| `london|up` | 2 |
| `new_york|down` | 2 |
| `new_york|flat` | 1 |
| `new_york|up` | 3 |

### Wick rejections

| event_key | count |
|---|---|
| `lower_rejection` | 118 |
| `upper_rejection` | 117 |

---

## Detection outcomes (Phase 3)

Rule `EMA_OUTCOME_V1`, frozen: reference `next_open`,
thresholds [3.0, 5.0, 10.0, 15.0, 20.0] in **points**.
70 `ema_cross` detections evaluated.

**Reached counts come from COMPLETE horizons only.** Pending and invalid are
reported separately and are never counted as misses — an unknown answer is not
a failure, and treating it as one is the easiest way to make a strategy look
worse than it is.

| horizon | complete | pending | invalid | reach 3 | reach 5 | reach 10 |
|---|---|---|---|---|---|---|
| `c5` | 70 | 0 | 0 | 70/70 (100%) | 70/70 (100%) | 70/70 (100%) |
| `c10` | 70 | 0 | 0 | 70/70 (100%) | 70/70 (100%) | 70/70 (100%) |
| `c20` | 68 | 2 | 0 | 68/68 (100%) | 68/68 (100%) | 68/68 (100%) |
| `m60` | 70 | 0 | 0 | 70/70 (100%) | 70/70 (100%) | 70/70 (100%) |
| `session_close` | 67 | 3 | 0 | 67/67 (100%) | 67/67 (100%) | 67/67 (100%) |
| `day_close` | 57 | 7 | 6 | 57/57 (100%) | 57/57 (100%) | 57/57 (100%) |
| `opposite_cross` | 68 | 1 | 1 | 68/68 (100%) | 68/68 (100%) | 68/68 (100%) |

### Path classification (COMPLETE only)

| horizon | MFE_FIRST | MAE_FIRST | NONE | ambiguous |
|---|---|---|---|---|
| `c5` | 70 | 0 | 0 | 69 |
| `c10` | 70 | 0 | 0 | 69 |
| `c20` | 68 | 0 | 0 | 67 |
| `m60` | 70 | 0 | 0 | 69 |
| `session_close` | 67 | 0 | 0 | 66 |
| `day_close` | 57 | 0 | 0 | 56 |
| `opposite_cross` | 68 | 0 | 0 | 67 |

### Diagnostics — read before using these numbers

- thresholds ['3', '5', '10', '15', '20'] are reached >=95% of the time in every horizon. They are smaller than the instrument's typical candle range, so they measure almost nothing. The fix is a NEW rule_id with a larger scale -- never an edit to this one (§21).
- 463/470 path classifications are ambiguous: the favourable and adverse thresholds were first crossed within the SAME candle, so their order was never observed. MFE_FIRST here is a convention, not a measurement -- treat these as unknown.

In short: at `point = 0.01` the specified thresholds are $0.03–$0.20, well
inside a single XAUUSD M5 candle's range, so they are crossed on the first
candle almost every time. The 100% columns above measure the scale, not the
strategy. Correcting it means a **new `rule_id`**, never an edit to this one
(§21, decision 47).

---

## Weekly review reconciliation (Phase 7)

Weekly reviews generated from this same replay, one per ISO week the fixture
spans. The review path counts independently of everything above: half-open
market-clock week windows, reached counts from COMPLETE horizons only. The
numbers must match, and the generator fails if they do not.

| ISO week | market dates | review detections | baseline days |
|---|---|---|---|
| `2026-W38` | `2026-09-14` … `2026-09-18` | 55 | 55 |
| `2026-W39` | `2026-09-21` … `2026-09-22` | 15 | 15 |
| **total** | 7 days | **70** | **70** |

| check | reviews | baseline |
|---|---|---|
| `c5` COMPLETE | 70 | 70 |
| `c5` reached at 3 | 70 | 70 |
| `c10` COMPLETE | 70 | 70 |
| `c10` reached at 3 | 70 | 70 |
| `c20` COMPLETE | 68 | 68 |
| `c20` reached at 3 | 68 | 68 |
| `m60` COMPLETE | 70 | 70 |
| `m60` reached at 3 | 70 | 70 |
| `session_close` COMPLETE | 67 | 67 |
| `session_close` reached at 3 | 67 | 67 |
| `day_close` COMPLETE | 57 | 57 |
| `day_close` reached at 3 | 57 | 57 |
| `opposite_cross` COMPLETE | 68 | 68 |
| `opposite_cross` reached at 3 | 68 | 68 |
| PENDING horizons excluded | 13 | 13 |
| INVALID horizons excluded | 7 | 7 |

Reconciled: every figure above agrees.

---

## Not yet measured

Outcomes for the Part B agents. `EMA_OUTCOME_V1` is written for `ema_cross`;
whether the same horizons and thresholds suit sweeps and breakouts is a question
for a rule of their own.
