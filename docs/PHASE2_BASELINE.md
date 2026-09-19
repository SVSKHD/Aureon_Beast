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

## Not yet measured

Phase 3 adds reached-3/5/10 counts from COMPLETE horizons only, reported
separately from PENDING counts.
