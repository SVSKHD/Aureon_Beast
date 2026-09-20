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

## Detection outcomes — `EMA_OUTCOME_V1` (HISTORICAL — invalid for research)

**Do not read these numbers as a result.** At `point = 0.01` this rule's
3-20 are $0.03-$0.20, well inside a single XAUUSD M5 candle, so almost every
threshold is crossed on the first bar. The table measures the instrument's tick
size, not the strategy, and its ~100% columns are an artefact of the scale.

It is kept, regenerated, for two reasons: results were stored under this
`rule_id` and a rule is frozen once shipped (§21, decision 47), and the
`XAU_OUTCOME_V2` section below is only interpretable next to what it replaced.
**`XAU_OUTCOME_V2` is the rule to read**, and it is what the running system
evaluates against (`AUREON_EVAL_RULE`).

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
(§21, decision 47) — which is what `XAU_OUTCOME_V2` below is.

---

## Detection outcomes — `XAU_OUTCOME_V2`

**The rule to read.** Reference `next_open`, thresholds $3 / $5 / $10 / $15 / $20 in **price** (§21),
converted to points with `point = 0.01` at evaluation time rather than
written down as a multiplier.

1172 detections from the whole roster, 808 evaluated, 804 with at least
one COMPLETE horizon. 364 carry `direction=None` and have no
favourable side to measure, so they are not evaluable at all (§16, decision 48)
— they are context for the reviews, not outcomes.

Read the columns literally:

* `complete` / `pending` / `invalid` are horizon counts, not detection counts: one
  detection contributes one row to each of the rule's seven horizons.
* every reached count, median and path figure is from **COMPLETE horizons only**;
  `pending` and `invalid` are never folded into a denominator.
* `invalid` here is almost entirely the fixture's 49-hour weekend gap: a horizon
  spanning it cannot be measured, so it is excluded rather than reported as a
  lower bound (§22).
* medians are in **price**, signed. A negative median MFE would mean the best
  price ever offered was still worse than the reference.
* `median t→$5` is over the horizons that **reached** $5, with that count beside
  it. It is not the time a typical detection takes; a non-reach has no duration.
* `MFE_FIRST` + `MAE_FIRST` does not equal `complete`: a horizon where neither
  side ever crossed $3 is classified `NONE` (§23).
* `ambiguous` counts horizons where both sides were first crossed inside one
  candle, so their order was never observed (decision 49).

### `breakout` — XAU_OUTCOME_V2

251 detections, 251 evaluated, 251 with at least one COMPLETE horizon

| session | horizon | complete | pending | invalid | reach $3 | reach $5 | reach $10 | reach $15 | reach $20 | median MFE (price) | median MAE (price) | median t→$5 | MFE_FIRST | MAE_FIRST | ambiguous |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `asia` | `c5` | 59 | 0 | 0 | 0/59 (0%) | 0/59 (0%) | 0/59 (0%) | 0/59 (0%) | 0/59 (0%) | +0.99 | -0.82 | — (n=0) | 0 | 3 | 0 |
| `asia` | `c10` | 59 | 0 | 0 | 4/59 (7%) | 0/59 (0%) | 0/59 (0%) | 0/59 (0%) | 0/59 (0%) | +1.21 | -1.29 | — (n=0) | 4 | 8 | 0 |
| `asia` | `c20` | 59 | 0 | 0 | 14/59 (24%) | 3/59 (5%) | 0/59 (0%) | 0/59 (0%) | 0/59 (0%) | +1.54 | -1.70 | 85m (n=3) | 14 | 14 | 0 |
| `asia` | `m60` | 59 | 0 | 0 | 6/59 (10%) | 0/59 (0%) | 0/59 (0%) | 0/59 (0%) | 0/59 (0%) | +1.35 | -1.38 | — (n=0) | 6 | 8 | 0 |
| `asia` | `session_close` | 59 | 0 | 0 | 20/59 (34%) | 8/59 (14%) | 2/59 (3%) | 0/59 (0%) | 0/59 (0%) | +2.35 | -2.76 | 182m (n=8) | 19 | 25 | 0 |
| `asia` | `day_close` | 35 | 16 | 8 | 32/35 (91%) | 28/35 (80%) | 16/35 (46%) | 13/35 (37%) | 11/35 (31%) | +8.53 | -10.27 | 412m (n=28) | 17 | 18 | 0 |
| `asia` | `opposite_cross` | 59 | 0 | 0 | 23/59 (39%) | 15/59 (25%) | 5/59 (8%) | 3/59 (5%) | 0/59 (0%) | +1.95 | -1.63 | 195m (n=15) | 23 | 12 | 0 |
| `london` | `c5` | 110 | 0 | 0 | 17/110 (15%) | 1/110 (1%) | 0/110 (0%) | 0/110 (0%) | 0/110 (0%) | +1.64 | -1.56 | 20m (n=1) | 17 | 16 | 0 |
| `london` | `c10` | 107 | 3 | 0 | 37/107 (35%) | 7/107 (7%) | 0/107 (0%) | 0/107 (0%) | 0/107 (0%) | +2.27 | -2.39 | 35m (n=7) | 36 | 42 | 0 |
| `london` | `c20` | 107 | 3 | 0 | 50/107 (47%) | 23/107 (21%) | 2/107 (2%) | 0/107 (0%) | 0/107 (0%) | +2.75 | -3.34 | 60m (n=23) | 44 | 54 | 0 |
| `london` | `m60` | 107 | 3 | 0 | 43/107 (40%) | 15/107 (14%) | 0/107 (0%) | 0/107 (0%) | 0/107 (0%) | +2.30 | -2.56 | 55m (n=15) | 41 | 44 | 0 |
| `london` | `session_close` | 104 | 6 | 0 | 53/104 (51%) | 28/104 (27%) | 14/104 (13%) | 4/104 (4%) | 0/104 (0%) | +3.14 | -6.48 | 60m (n=28) | 42 | 51 | 0 |
| `london` | `day_close` | 80 | 6 | 24 | 62/80 (78%) | 50/80 (62%) | 24/80 (30%) | 20/80 (25%) | 7/80 (9%) | +7.24 | -6.79 | 185m (n=50) | 36 | 44 | 0 |
| `london` | `opposite_cross` | 105 | 5 | 0 | 51/105 (49%) | 30/105 (29%) | 15/105 (14%) | 12/105 (11%) | 3/105 (3%) | +2.77 | -3.51 | 62m (n=30) | 43 | 57 | 0 |
| `new_york` | `c5` | 66 | 0 | 0 | 10/66 (15%) | 3/66 (5%) | 0/66 (0%) | 0/66 (0%) | 0/66 (0%) | +1.92 | -1.35 | 15m (n=3) | 10 | 5 | 0 |
| `new_york` | `c10` | 66 | 0 | 0 | 26/66 (39%) | 5/66 (8%) | 0/66 (0%) | 0/66 (0%) | 0/66 (0%) | +2.79 | -1.96 | 15m (n=5) | 24 | 17 | 0 |
| `new_york` | `c20` | 64 | 0 | 2 | 38/64 (59%) | 17/64 (27%) | 0/64 (0%) | 0/64 (0%) | 0/64 (0%) | +3.68 | -2.44 | 70m (n=17) | 33 | 28 | 0 |
| `new_york` | `m60` | 66 | 0 | 0 | 32/66 (48%) | 8/66 (12%) | 0/66 (0%) | 0/66 (0%) | 0/66 (0%) | +2.97 | -2.03 | 50m (n=8) | 30 | 21 | 0 |
| `new_york` | `session_close` | 66 | 0 | 0 | 39/66 (59%) | 32/66 (48%) | 3/66 (5%) | 0/66 (0%) | 0/66 (0%) | +4.01 | -2.50 | 105m (n=32) | 32 | 25 | 0 |
| `new_york` | `day_close` | 54 | 0 | 12 | 30/54 (56%) | 24/54 (44%) | 0/54 (0%) | 0/54 (0%) | 0/54 (0%) | +3.76 | -3.57 | 110m (n=24) | 25 | 27 | 0 |
| `new_york` | `opposite_cross` | 54 | 0 | 12 | 29/54 (54%) | 22/54 (41%) | 2/54 (4%) | 0/54 (0%) | 0/54 (0%) | +3.37 | -2.43 | 108m (n=22) | 25 | 22 | 0 |
| `off` | `c5` | 16 | 0 | 0 | 1/16 (6%) | 0/16 (0%) | 0/16 (0%) | 0/16 (0%) | 0/16 (0%) | +1.07 | -0.87 | — (n=0) | 1 | 2 | 0 |
| `off` | `c10` | 16 | 0 | 0 | 3/16 (19%) | 1/16 (6%) | 0/16 (0%) | 0/16 (0%) | 0/16 (0%) | +1.19 | -1.74 | 50m (n=1) | 3 | 4 | 0 |
| `off` | `c20` | 14 | 0 | 2 | 4/14 (29%) | 3/14 (21%) | 0/14 (0%) | 0/14 (0%) | 0/14 (0%) | +1.29 | -2.84 | 70m (n=3) | 4 | 7 | 0 |
| `off` | `m60` | 14 | 0 | 2 | 4/14 (29%) | 1/14 (7%) | 0/14 (0%) | 0/14 (0%) | 0/14 (0%) | +1.07 | -2.17 | 50m (n=1) | 4 | 5 | 0 |
| `off` | `session_close` | 14 | 0 | 2 | 3/14 (21%) | 2/14 (14%) | 0/14 (0%) | 0/14 (0%) | 0/14 (0%) | +1.27 | -2.84 | 60m (n=2) | 3 | 7 | 0 |
| `off` | `day_close` | 11 | 2 | 3 | 4/11 (36%) | 2/11 (18%) | 1/11 (9%) | 0/11 (0%) | 0/11 (0%) | +1.58 | -2.33 | 202m (n=2) | 3 | 5 | 0 |
| `off` | `opposite_cross` | 14 | 0 | 2 | 6/14 (43%) | 5/14 (36%) | 1/14 (7%) | 1/14 (7%) | 0/14 (0%) | +1.07 | -2.11 | 80m (n=5) | 5 | 5 | 0 |
| **all** | `c5` | 251 | 0 | 0 | 28/251 (11%) | 4/251 (2%) | 0/251 (0%) | 0/251 (0%) | 0/251 (0%) | +1.40 | -1.27 | 15m (n=4) | 28 | 26 | 0 |
| **all** | `c10` | 248 | 3 | 0 | 70/248 (28%) | 13/248 (5%) | 0/248 (0%) | 0/248 (0%) | 0/248 (0%) | +2.03 | -1.95 | 35m (n=13) | 67 | 71 | 0 |
| **all** | `c20` | 244 | 3 | 4 | 106/244 (43%) | 46/244 (19%) | 2/244 (1%) | 0/244 (0%) | 0/244 (0%) | +2.66 | -2.60 | 60m (n=46) | 95 | 103 | 0 |
| **all** | `m60` | 246 | 3 | 2 | 85/246 (35%) | 24/246 (10%) | 0/246 (0%) | 0/246 (0%) | 0/246 (0%) | +2.25 | -2.09 | 50m (n=24) | 81 | 78 | 0 |
| **all** | `session_close` | 243 | 6 | 2 | 115/243 (47%) | 70/243 (29%) | 19/243 (8%) | 4/243 (2%) | 0/243 (0%) | +2.87 | -3.32 | 85m (n=70) | 96 | 108 | 0 |
| **all** | `day_close` | 180 | 24 | 47 | 128/180 (71%) | 104/180 (58%) | 41/180 (23%) | 33/180 (18%) | 18/180 (10%) | +5.99 | -5.81 | 182m (n=104) | 81 | 94 | 0 |
| **all** | `opposite_cross` | 232 | 5 | 14 | 109/232 (47%) | 72/232 (31%) | 23/232 (10%) | 16/232 (7%) | 3/232 (1%) | +2.83 | -2.84 | 92m (n=72) | 96 | 96 | 0 |

### `ema_cross` — XAU_OUTCOME_V2

29 detections, 29 evaluated, 29 with at least one COMPLETE horizon

| session | horizon | complete | pending | invalid | reach $3 | reach $5 | reach $10 | reach $15 | reach $20 | median MFE (price) | median MAE (price) | median t→$5 | MFE_FIRST | MAE_FIRST | ambiguous |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `asia` | `c5` | 9 | 0 | 0 | 0/9 (0%) | 0/9 (0%) | 0/9 (0%) | 0/9 (0%) | 0/9 (0%) | +0.90 | -0.63 | — (n=0) | 0 | 0 | 0 |
| `asia` | `c10` | 9 | 0 | 0 | 2/9 (22%) | 0/9 (0%) | 0/9 (0%) | 0/9 (0%) | 0/9 (0%) | +0.95 | -1.18 | — (n=0) | 2 | 2 | 0 |
| `asia` | `c20` | 9 | 0 | 0 | 5/9 (56%) | 1/9 (11%) | 0/9 (0%) | 0/9 (0%) | 0/9 (0%) | +3.07 | -1.18 | 65m (n=1) | 5 | 2 | 0 |
| `asia` | `m60` | 9 | 0 | 0 | 2/9 (22%) | 0/9 (0%) | 0/9 (0%) | 0/9 (0%) | 0/9 (0%) | +1.71 | -1.18 | — (n=0) | 2 | 2 | 0 |
| `asia` | `session_close` | 9 | 0 | 0 | 6/9 (67%) | 2/9 (22%) | 1/9 (11%) | 0/9 (0%) | 0/9 (0%) | +3.16 | -1.31 | 88m (n=2) | 6 | 3 | 0 |
| `asia` | `day_close` | 4 | 4 | 1 | 4/4 (100%) | 3/4 (75%) | 2/4 (50%) | 1/4 (25%) | 1/4 (25%) | +10.02 | -13.14 | 185m (n=3) | 3 | 1 | 0 |
| `asia` | `opposite_cross` | 9 | 0 | 0 | 6/9 (67%) | 3/9 (33%) | 1/9 (11%) | 1/9 (11%) | 0/9 (0%) | +3.16 | -1.48 | 110m (n=3) | 6 | 0 | 0 |
| `london` | `c5` | 13 | 0 | 0 | 2/13 (15%) | 0/13 (0%) | 0/13 (0%) | 0/13 (0%) | 0/13 (0%) | +1.71 | -1.50 | — (n=0) | 2 | 2 | 0 |
| `london` | `c10` | 13 | 0 | 0 | 6/13 (46%) | 0/13 (0%) | 0/13 (0%) | 0/13 (0%) | 0/13 (0%) | +2.41 | -2.15 | — (n=0) | 5 | 4 | 0 |
| `london` | `c20` | 13 | 0 | 0 | 8/13 (62%) | 2/13 (15%) | 0/13 (0%) | 0/13 (0%) | 0/13 (0%) | +3.14 | -3.17 | 65m (n=2) | 6 | 7 | 0 |
| `london` | `m60` | 13 | 0 | 0 | 7/13 (54%) | 1/13 (8%) | 0/13 (0%) | 0/13 (0%) | 0/13 (0%) | +3.05 | -2.85 | 60m (n=1) | 6 | 5 | 0 |
| `london` | `session_close` | 11 | 2 | 0 | 6/11 (55%) | 4/11 (36%) | 2/11 (18%) | 1/11 (9%) | 1/11 (9%) | +3.97 | -3.06 | 100m (n=4) | 5 | 3 | 0 |
| `london` | `day_close` | 9 | 2 | 2 | 7/9 (78%) | 5/9 (56%) | 3/9 (33%) | 2/9 (22%) | 2/9 (22%) | +7.59 | -6.39 | 190m (n=5) | 4 | 5 | 0 |
| `london` | `opposite_cross` | 12 | 1 | 0 | 8/12 (67%) | 5/12 (42%) | 2/12 (17%) | 2/12 (17%) | 2/12 (17%) | +3.98 | -3.52 | 130m (n=5) | 6 | 6 | 0 |
| `new_york` | `c5` | 6 | 0 | 0 | 1/6 (17%) | 0/6 (0%) | 0/6 (0%) | 0/6 (0%) | 0/6 (0%) | +1.27 | -1.60 | — (n=0) | 1 | 0 | 0 |
| `new_york` | `c10` | 6 | 0 | 0 | 2/6 (33%) | 0/6 (0%) | 0/6 (0%) | 0/6 (0%) | 0/6 (0%) | +1.72 | -1.99 | — (n=0) | 2 | 1 | 0 |
| `new_york` | `c20` | 6 | 0 | 0 | 4/6 (67%) | 1/6 (17%) | 0/6 (0%) | 0/6 (0%) | 0/6 (0%) | +3.39 | -1.99 | 95m (n=1) | 4 | 2 | 0 |
| `new_york` | `m60` | 6 | 0 | 0 | 2/6 (33%) | 0/6 (0%) | 0/6 (0%) | 0/6 (0%) | 0/6 (0%) | +2.35 | -1.99 | — (n=0) | 2 | 2 | 0 |
| `new_york` | `session_close` | 6 | 0 | 0 | 4/6 (67%) | 3/6 (50%) | 1/6 (17%) | 0/6 (0%) | 0/6 (0%) | +4.48 | -1.99 | 115m (n=3) | 4 | 1 | 0 |
| `new_york` | `day_close` | 5 | 0 | 1 | 3/5 (60%) | 2/5 (40%) | 0/5 (0%) | 0/5 (0%) | 0/5 (0%) | +3.80 | -4.11 | 105m (n=2) | 3 | 2 | 0 |
| `new_york` | `opposite_cross` | 5 | 0 | 1 | 3/5 (60%) | 3/5 (60%) | 1/5 (20%) | 0/5 (0%) | 0/5 (0%) | +5.17 | -4.11 | 115m (n=3) | 3 | 2 | 0 |
| `off` | `c5` | 1 | 0 | 0 | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | +1.17 | -0.99 | — (n=0) | 0 | 0 | 0 |
| `off` | `c10` | 1 | 0 | 0 | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | +2.81 | -0.99 | — (n=0) | 0 | 0 | 0 |
| `off` | `c20` | 1 | 0 | 0 | 1/1 (100%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | +3.60 | -1.45 | — (n=0) | 1 | 0 | 0 |
| `off` | `m60` | 1 | 0 | 0 | 1/1 (100%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | +3.60 | -0.99 | — (n=0) | 1 | 0 | 0 |
| `off` | `session_close` | 1 | 0 | 0 | 1/1 (100%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | +3.60 | -1.81 | — (n=0) | 1 | 0 | 0 |
| `off` | `day_close` | 1 | 0 | 0 | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | +1.17 | -0.99 | — (n=0) | 0 | 0 | 0 |
| `off` | `opposite_cross` | 1 | 0 | 0 | 1/1 (100%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | 0/1 (0%) | +3.60 | -2.38 | — (n=0) | 1 | 0 | 0 |
| **all** | `c5` | 29 | 0 | 0 | 3/29 (10%) | 0/29 (0%) | 0/29 (0%) | 0/29 (0%) | 0/29 (0%) | +1.43 | -1.46 | — (n=0) | 3 | 2 | 0 |
| **all** | `c10` | 29 | 0 | 0 | 10/29 (34%) | 0/29 (0%) | 0/29 (0%) | 0/29 (0%) | 0/29 (0%) | +1.99 | -1.61 | — (n=0) | 9 | 7 | 0 |
| **all** | `c20` | 29 | 0 | 0 | 18/29 (62%) | 4/29 (14%) | 0/29 (0%) | 0/29 (0%) | 0/29 (0%) | +3.14 | -2.39 | 68m (n=4) | 16 | 11 | 0 |
| **all** | `m60` | 29 | 0 | 0 | 12/29 (41%) | 1/29 (3%) | 0/29 (0%) | 0/29 (0%) | 0/29 (0%) | +2.27 | -1.62 | 60m (n=1) | 11 | 9 | 0 |
| **all** | `session_close` | 27 | 2 | 0 | 17/27 (63%) | 9/27 (33%) | 4/27 (15%) | 1/27 (4%) | 1/27 (4%) | +3.72 | -2.81 | 110m (n=9) | 16 | 7 | 0 |
| **all** | `day_close` | 19 | 6 | 4 | 14/19 (74%) | 10/19 (53%) | 5/19 (26%) | 3/19 (16%) | 3/19 (16%) | +5.17 | -5.68 | 150m (n=10) | 10 | 8 | 0 |
| **all** | `opposite_cross` | 27 | 1 | 1 | 18/27 (67%) | 11/27 (41%) | 4/27 (15%) | 3/27 (11%) | 2/27 (7%) | +3.71 | -2.95 | 115m (n=11) | 16 | 8 | 0 |

### `liquidity` — XAU_OUTCOME_V2

528 detections, 528 evaluated, 524 with at least one COMPLETE horizon

| session | horizon | complete | pending | invalid | reach $3 | reach $5 | reach $10 | reach $15 | reach $20 | median MFE (price) | median MAE (price) | median t→$5 | MFE_FIRST | MAE_FIRST | ambiguous |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `asia` | `c5` | 137 | 0 | 0 | 1/137 (1%) | 0/137 (0%) | 0/137 (0%) | 0/137 (0%) | 0/137 (0%) | +0.80 | -0.85 | — (n=0) | 1 | 1 | 0 |
| `asia` | `c10` | 137 | 0 | 0 | 13/137 (9%) | 0/137 (0%) | 0/137 (0%) | 0/137 (0%) | 0/137 (0%) | +1.18 | -1.22 | — (n=0) | 13 | 10 | 0 |
| `asia` | `c20` | 137 | 0 | 0 | 39/137 (28%) | 3/137 (2%) | 0/137 (0%) | 0/137 (0%) | 0/137 (0%) | +1.96 | -1.63 | 80m (n=3) | 39 | 32 | 0 |
| `asia` | `m60` | 137 | 0 | 0 | 16/137 (12%) | 0/137 (0%) | 0/137 (0%) | 0/137 (0%) | 0/137 (0%) | +1.34 | -1.22 | — (n=0) | 16 | 15 | 0 |
| `asia` | `session_close` | 137 | 0 | 0 | 64/137 (47%) | 35/137 (26%) | 0/137 (0%) | 0/137 (0%) | 0/137 (0%) | +2.75 | -2.26 | 195m (n=35) | 64 | 35 | 0 |
| `asia` | `day_close` | 87 | 37 | 13 | 77/87 (89%) | 70/87 (80%) | 40/87 (46%) | 29/87 (33%) | 22/87 (25%) | +8.54 | -9.23 | 322m (n=70) | 55 | 32 | 0 |
| `asia` | `opposite_cross` | 137 | 0 | 0 | 18/137 (13%) | 3/137 (2%) | 0/137 (0%) | 0/137 (0%) | 0/137 (0%) | +1.30 | -1.40 | 80m (n=3) | 17 | 33 | 0 |
| `london` | `c5` | 216 | 4 | 0 | 39/216 (18%) | 10/216 (5%) | 0/216 (0%) | 0/216 (0%) | 0/216 (0%) | +1.59 | -1.44 | 18m (n=10) | 39 | 41 | 0 |
| `london` | `c10` | 216 | 4 | 0 | 74/216 (34%) | 26/216 (12%) | 0/216 (0%) | 0/216 (0%) | 0/216 (0%) | +1.97 | -2.21 | 32m (n=26) | 72 | 80 | 0 |
| `london` | `c20` | 215 | 5 | 0 | 110/215 (51%) | 57/215 (27%) | 3/215 (1%) | 0/215 (0%) | 0/215 (0%) | +3.13 | -3.46 | 55m (n=57) | 103 | 105 | 0 |
| `london` | `m60` | 216 | 4 | 0 | 85/216 (39%) | 38/216 (18%) | 0/216 (0%) | 0/216 (0%) | 0/216 (0%) | +2.20 | -2.44 | 40m (n=38) | 83 | 89 | 0 |
| `london` | `session_close` | 202 | 18 | 0 | 135/202 (67%) | 112/202 (55%) | 39/202 (19%) | 2/202 (1%) | 0/202 (0%) | +5.43 | -3.98 | 130m (n=112) | 79 | 102 | 0 |
| `london` | `day_close` | 150 | 18 | 52 | 117/150 (78%) | 88/150 (59%) | 48/150 (32%) | 32/150 (21%) | 25/150 (17%) | +5.57 | -7.26 | 158m (n=88) | 65 | 85 | 0 |
| `london` | `opposite_cross` | 214 | 6 | 0 | 78/214 (36%) | 45/214 (21%) | 1/214 (0%) | 0/214 (0%) | 0/214 (0%) | +2.06 | -1.78 | 70m (n=45) | 73 | 76 | 0 |
| `new_york` | `c5` | 124 | 0 | 0 | 11/124 (9%) | 0/124 (0%) | 0/124 (0%) | 0/124 (0%) | 0/124 (0%) | +1.27 | -1.77 | — (n=0) | 11 | 28 | 0 |
| `new_york` | `c10` | 124 | 0 | 0 | 25/124 (20%) | 10/124 (8%) | 0/124 (0%) | 0/124 (0%) | 0/124 (0%) | +1.47 | -2.72 | 35m (n=10) | 25 | 57 | 0 |
| `new_york` | `c20` | 122 | 0 | 2 | 42/122 (34%) | 25/122 (20%) | 0/122 (0%) | 0/122 (0%) | 0/122 (0%) | +2.20 | -4.13 | 65m (n=25) | 32 | 78 | 0 |
| `new_york` | `m60` | 124 | 0 | 0 | 25/124 (20%) | 10/124 (8%) | 0/124 (0%) | 0/124 (0%) | 0/124 (0%) | +1.65 | -3.22 | 35m (n=10) | 25 | 65 | 0 |
| `new_york` | `session_close` | 124 | 0 | 0 | 49/124 (40%) | 38/124 (31%) | 6/124 (5%) | 2/124 (2%) | 0/124 (0%) | +2.23 | -4.50 | 90m (n=38) | 31 | 76 | 0 |
| `new_york` | `day_close` | 103 | 0 | 21 | 45/103 (44%) | 36/103 (35%) | 2/103 (2%) | 0/103 (0%) | 0/103 (0%) | +2.54 | -4.48 | 92m (n=36) | 27 | 63 | 0 |
| `new_york` | `opposite_cross` | 109 | 0 | 15 | 12/109 (11%) | 4/109 (4%) | 0/109 (0%) | 0/109 (0%) | 0/109 (0%) | +1.52 | -3.10 | 58m (n=4) | 12 | 51 | 0 |
| `off` | `c5` | 47 | 0 | 0 | 3/47 (6%) | 0/47 (0%) | 0/47 (0%) | 0/47 (0%) | 0/47 (0%) | +1.09 | -0.80 | — (n=0) | 3 | 1 | 0 |
| `off` | `c10` | 47 | 0 | 0 | 13/47 (28%) | 0/47 (0%) | 0/47 (0%) | 0/47 (0%) | 0/47 (0%) | +1.48 | -1.19 | — (n=0) | 13 | 4 | 0 |
| `off` | `c20` | 47 | 0 | 0 | 18/47 (38%) | 2/47 (4%) | 0/47 (0%) | 0/47 (0%) | 0/47 (0%) | +2.31 | -1.79 | 95m (n=2) | 18 | 6 | 0 |
| `off` | `m60` | 47 | 0 | 0 | 13/47 (28%) | 0/47 (0%) | 0/47 (0%) | 0/47 (0%) | 0/47 (0%) | +1.53 | -1.43 | — (n=0) | 13 | 4 | 0 |
| `off` | `session_close` | 47 | 0 | 0 | 16/47 (34%) | 0/47 (0%) | 0/47 (0%) | 0/47 (0%) | 0/47 (0%) | +2.43 | -1.29 | — (n=0) | 16 | 6 | 0 |
| `off` | `day_close` | 30 | 4 | 13 | 18/30 (60%) | 15/30 (50%) | 13/30 (43%) | 9/30 (30%) | 6/30 (20%) | +5.61 | -2.30 | 200m (n=15) | 16 | 4 | 0 |
| `off` | `opposite_cross` | 47 | 0 | 0 | 16/47 (34%) | 1/47 (2%) | 0/47 (0%) | 0/47 (0%) | 0/47 (0%) | +1.98 | -1.53 | 90m (n=1) | 16 | 10 | 0 |
| **all** | `c5` | 524 | 4 | 0 | 54/524 (10%) | 10/524 (2%) | 0/524 (0%) | 0/524 (0%) | 0/524 (0%) | +1.25 | -1.22 | 18m (n=10) | 54 | 71 | 0 |
| **all** | `c10` | 524 | 4 | 0 | 125/524 (24%) | 36/524 (7%) | 0/524 (0%) | 0/524 (0%) | 0/524 (0%) | +1.57 | -1.75 | 35m (n=36) | 123 | 151 | 0 |
| **all** | `c20` | 521 | 5 | 2 | 209/521 (40%) | 87/521 (17%) | 3/521 (1%) | 0/521 (0%) | 0/521 (0%) | +2.32 | -2.64 | 60m (n=87) | 192 | 221 | 0 |
| **all** | `m60` | 524 | 4 | 0 | 139/524 (27%) | 48/524 (9%) | 0/524 (0%) | 0/524 (0%) | 0/524 (0%) | +1.78 | -1.96 | 38m (n=48) | 137 | 173 | 0 |
| **all** | `session_close` | 510 | 18 | 0 | 264/510 (52%) | 185/510 (36%) | 45/510 (9%) | 4/510 (1%) | 0/510 (0%) | +3.10 | -3.09 | 130m (n=185) | 190 | 219 | 0 |
| **all** | `day_close` | 370 | 59 | 99 | 257/370 (69%) | 209/370 (56%) | 103/370 (28%) | 70/370 (19%) | 53/370 (14%) | +5.45 | -7.08 | 185m (n=209) | 163 | 184 | 0 |
| **all** | `opposite_cross` | 507 | 6 | 15 | 124/507 (24%) | 53/507 (10%) | 1/507 (0%) | 0/507 (0%) | 0/507 (0%) | +1.61 | -1.96 | 70m (n=53) | 118 | 170 | 0 |

### `rsi` — no outcomes

110 detections, none evaluable: this agent emits `direction=None`, so there is no favourable side to measure (§16, decision 48).

### `session_trend` — no outcomes

19 detections, none evaluable: this agent emits `direction=None`, so there is no favourable side to measure (§16, decision 48).

### `wick` — no outcomes

235 detections, none evaluable: this agent emits `direction=None`, so there is no favourable side to measure (§16, decision 48).

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
| `c5` reached at 3 | 3 | 3 |
| `c10` COMPLETE | 29 | 29 |
| `c10` reached at 3 | 10 | 10 |
| `c20` COMPLETE | 29 | 29 |
| `c20` reached at 3 | 18 | 18 |
| `m60` COMPLETE | 29 | 29 |
| `m60` reached at 3 | 12 | 12 |
| `session_close` COMPLETE | 27 | 27 |
| `session_close` reached at 3 | 17 | 17 |
| `day_close` COMPLETE | 19 | 19 |
| `day_close` reached at 3 | 14 | 14 |
| `opposite_cross` COMPLETE | 27 | 27 |
| `opposite_cross` reached at 3 | 18 | 18 |
| PENDING horizons excluded | 9 | 9 |
| INVALID horizons excluded | 5 | 5 |

Reconciled: every figure above agrees.

---

## Not yet measured

* **Anything from a real session.** Every number in this document is a replay of
  a synthetic fixture (decision 28). It proves the pipeline computes what it says
  it computes; it says nothing about gold. `docs/MT5_SESSION_CHECKLIST.md` is the
  manual leg that turns this into evidence about a market.
* **Whether the thresholds discriminate.** $3-$20 is a distance a gold trader
  holds for, not a distance a study showed separates good detections from bad.
* **Whether the level and wick parameters select anything real.** They are
  v1.0.0 placeholders researched on nothing (decision 120), so every `liquidity`,
  `breakout`, `wick` and `session_trend` count here is a count of what those
  arbitrary numbers happened to select.
