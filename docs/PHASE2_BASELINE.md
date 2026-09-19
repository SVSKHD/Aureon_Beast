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
| engine window | 64 bars (fixed; parity contract) |

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

## Not yet measured

Part B agents (RSI context, session trend, liquidity sweeps, wick rejections,
breakouts) are not implemented, so sweeps-per-session and breakouts-per-session
are absent from this table. They are added here as each agent lands, alongside
its own parity test.

Phase 3 adds reached-3/5/10 counts from COMPLETE horizons only, reported
separately from PENDING counts.
