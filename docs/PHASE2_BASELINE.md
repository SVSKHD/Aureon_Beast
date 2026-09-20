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
| agent | `ema_cross` v2.0.0 |
| fast / slow EMA | 20 / 50 |
| RSI period | 14 (context only, never a gate) |
| warm-up bars | 150 (slow × 3) |
| engine window | 583 bars (fixed; parity contract) |

## Detections

| metric | count |
|---|---|
| total | 29 |
| bullish crosses | 14 |
| bearish crosses | 15 |
| unique detection ids | 29 |

### By session

| session | detections |
|---|---|
| `asia` | 9 |
| `london` | 13 |
| `new_york` | 6 |
| `off` | 1 |

### By broker trading day

| market date | detections |
|---|---|
| `2026-09-14` | 2 |
| `2026-09-15` | 6 |
| `2026-09-16` | 4 |
| `2026-09-17` | 5 |
| `2026-09-18` | 4 |
| `2026-09-21` | 2 |
| `2026-09-22` | 6 |

---

## All agents

Counts from one run with the whole roster registered. Because the engine gives
each agent its own window slice, these are identical to running each agent
alone -- adding an agent never changes another's output.

| agent | version | window | detections |
|---|---|---|---|
| `ema_cross` | 2.0.0 | 151 | 29 |
| `rsi` | 1.0.0 | 44 | 110 |
| `session_trend` | 1.0.0 | 98 | 19 |
| `wick` | 1.0.0 | 1 | 235 |
| `liquidity` | 1.0.0 | 583 | 528 |
| `breakout` | 1.0.0 | 583 | 251 |

### Crosses, sweeps and breakouts per session

The three counts the Phase 2 gate asks to be recorded.

| session | crosses | sweeps | breakouts |
|---|---|---|---|
| `asia` | 9 | 137 | 59 |
| `london` | 13 | 220 | 110 |
| `new_york` | 6 | 124 | 66 |
| `off` | 1 | 47 | 16 |
| **total** | **29** | **528** | **251** |

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
29 `ema_cross` detections evaluated.

**Reached counts come from COMPLETE horizons only.** Pending and invalid are
reported separately and are never counted as misses — an unknown answer is not
a failure, and treating it as one is the easiest way to make a strategy look
worse than it is.

| horizon | complete | pending | invalid | reach 3 | reach 5 | reach 10 |
|---|---|---|---|---|---|---|
| `c5` | 29 | 0 | 0 | 29/29 (100%) | 29/29 (100%) | 29/29 (100%) |
| `c10` | 29 | 0 | 0 | 29/29 (100%) | 29/29 (100%) | 29/29 (100%) |
| `c20` | 29 | 0 | 0 | 29/29 (100%) | 29/29 (100%) | 29/29 (100%) |
| `m60` | 29 | 0 | 0 | 29/29 (100%) | 29/29 (100%) | 29/29 (100%) |
| `session_close` | 27 | 2 | 0 | 27/27 (100%) | 27/27 (100%) | 27/27 (100%) |
| `day_close` | 19 | 6 | 4 | 19/19 (100%) | 19/19 (100%) | 19/19 (100%) |
| `opposite_cross` | 27 | 1 | 1 | 27/27 (100%) | 27/27 (100%) | 27/27 (100%) |

### Path classification (COMPLETE only)

| horizon | MFE_FIRST | MAE_FIRST | NONE | ambiguous |
|---|---|---|---|---|
| `c5` | 28 | 1 | 0 | 28 |
| `c10` | 28 | 1 | 0 | 28 |
| `c20` | 28 | 1 | 0 | 28 |
| `m60` | 28 | 1 | 0 | 28 |
| `session_close` | 26 | 1 | 0 | 26 |
| `day_close` | 18 | 1 | 0 | 18 |
| `opposite_cross` | 26 | 1 | 0 | 26 |

### Diagnostics — read before using these numbers

- thresholds ['3', '5', '10', '15', '20'] are reached >=95% of the time in every horizon. They are smaller than the instrument's typical candle range, so they measure almost nothing. The fix is a NEW rule_id with a larger scale -- never an edit to this one (§21).
- 182/189 path classifications are ambiguous: the favourable and adverse thresholds were first crossed within the SAME candle, so their order was never observed. MFE_FIRST here is a convention, not a measurement -- treat these as unknown.

In short: at `point = 0.01` the specified thresholds are $0.03–$0.20, well
inside a single XAUUSD M5 candle's range, so they are crossed on the first
candle almost every time. The 100% columns above measure the scale, not the
strategy. Correcting it means a **new `rule_id`**, never an edit to this one
(§21, decision 47).

---

## Historical: `ema_cross` v1.0.0 at 9/21

**Not the baseline.** The pair that shipped before 20/50, replayed over the same
fixture so the change is a visible diff in counts. Nothing reconciles against
these numbers.

Under §12 the agent version is part of the detection id, so these detections do
not collide with the current ones -- both pairs can describe the same week.

| metric | 9/21 (v1.0.0) | 20/50 (v2.0.0) |
|---|---|---|
| total crosses | 70 | 29 |
| bullish | 35 | 14 |
| bearish | 35 | 15 |
| warm-up bars | 63 | 150 |

### 9/21 by session

| session | detections |
|---|---|
| `asia` | 16 |
| `london` | 31 |
| `new_york` | 12 |
| `off` | 11 |

---

## Weekly review reconciliation (Phase 7)

Weekly reviews generated from this same replay, one per ISO week the fixture
spans. The review path counts independently of everything above: half-open
market-clock week windows, reached counts from COMPLETE horizons only. The
numbers must match, and the generator fails if they do not.

| ISO week | market dates | review detections | baseline days |
|---|---|---|---|
| `2026-W38` | `2026-09-14` … `2026-09-18` | 21 | 21 |
| `2026-W39` | `2026-09-21` … `2026-09-22` | 8 | 8 |
| **total** | 7 days | **29** | **29** |

| check | reviews | baseline |
|---|---|---|
| `c5` COMPLETE | 29 | 29 |
| `c5` reached at 3 | 29 | 29 |
| `c10` COMPLETE | 29 | 29 |
| `c10` reached at 3 | 29 | 29 |
| `c20` COMPLETE | 29 | 29 |
| `c20` reached at 3 | 29 | 29 |
| `m60` COMPLETE | 29 | 29 |
| `m60` reached at 3 | 29 | 29 |
| `session_close` COMPLETE | 27 | 27 |
| `session_close` reached at 3 | 27 | 27 |
| `day_close` COMPLETE | 19 | 19 |
| `day_close` reached at 3 | 19 | 19 |
| `opposite_cross` COMPLETE | 27 | 27 |
| `opposite_cross` reached at 3 | 27 | 27 |
| PENDING horizons excluded | 9 | 9 |
| INVALID horizons excluded | 5 | 5 |

Reconciled: every figure above agrees.

---

## Not yet measured

Outcomes for the Part B agents. `EMA_OUTCOME_V1` is written for `ema_cross`;
whether the same horizons and thresholds suit sweeps and breakouts is a question
for a rule of their own.
