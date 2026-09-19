#!/usr/bin/env python3
"""Generate the XAUUSD M5 candle fixture for replay and tests.

**Synthetic, and deliberately labelled as such** (decision 28). No real broker
export was available, so this builds one week of plausible M5 gold candles from a
seeded random walk. It is reproducible -- same seed, same file -- so the parity
tests and the Phase 2 baseline counts are stable.

What it models, because each one exercises code that would otherwise go untested:

* **the weekend gap** -- trading runs Sunday 22:00 UTC to Friday 21:00 UTC, so the
  observer's gap handling and the market-state service meet a real discontinuity;
* **session-varying volatility** -- Asia quiet, London active, New York active
  with the London overlap busiest, so session aggregates differ from each other;
* **trend segments** -- slow drifts that reverse, so EMA crosses actually occur
  rather than the series oscillating around a flat mean and producing either none
  or thousands.

Prices are rounded to 2 decimals, matching XAUUSD's digits.
"""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "aureon" / "data" / "fixtures" / "XAUUSD_M5.csv"

SEED = 20260918
START_PRICE = 2400.00
DIGITS = 2

# The trading week: Sunday 22:00 UTC open, Friday 21:00 UTC close.
WEEK_OPEN_WEEKDAY = 6  # Sunday
WEEK_OPEN_HOUR = 22
WEEK_CLOSE_WEEKDAY = 4  # Friday
WEEK_CLOSE_HOUR = 21


def is_market_open(moment: datetime) -> bool:
    """Whether the gold market is open at ``moment`` (UTC)."""
    weekday = moment.weekday()  # Mon=0 .. Sun=6
    if weekday == 5:  # Saturday: always closed
        return False
    if weekday == WEEK_OPEN_WEEKDAY:  # Sunday: opens at 22:00
        return moment.hour >= WEEK_OPEN_HOUR
    if weekday == WEEK_CLOSE_WEEKDAY:  # Friday: closes at 21:00
        return moment.hour < WEEK_CLOSE_HOUR
    return True


def session_volatility(moment: datetime) -> float:
    """Per-candle volatility in price units, by session (market hours, UTC-ish).

    Rough but purposeful: the London/New York overlap is the busiest stretch of a
    gold day, and Asia the quietest, so session-level aggregates in the Phase 2
    baseline differ from one another instead of all looking alike.
    """
    hour = moment.hour
    if 12 <= hour < 15:  # London/NY overlap
        return 1.10
    if 7 <= hour < 12:  # London
        return 0.85
    if 15 <= hour < 20:  # New York
        return 0.80
    if 23 <= hour or hour < 7:  # Asia + rollover
        return 0.45
    return 0.60


def generate(seed: int = SEED) -> list[dict[str, object]]:
    rng = np.random.default_rng(seed)

    # Start at the Sunday 22:00 UTC open preceding a fixed Monday.
    start = datetime(2026, 9, 13, 22, 0, tzinfo=UTC)  # Sunday
    # Deliberately past Friday's close and into the NEXT week's open, so the file
    # actually contains the weekend discontinuity (Fri 21:00 -> Sun 22:00, ~49h).
    # Ending at Friday's close would leave the gap handling and the INVALID-horizon
    # path with no data to exercise them.
    end = start + timedelta(days=8, hours=14)

    # Trend segments: each is (bars_remaining, drift_per_bar). Reversing drift is
    # what produces genuine EMA crossovers rather than noise around a flat mean.
    price = START_PRICE
    rows: list[dict[str, object]] = []
    drift = 0.0
    segment_left = 0

    moment = start
    while moment < end:
        if not is_market_open(moment):
            moment += timedelta(minutes=5)
            continue

        if segment_left <= 0:
            # 6-30 hours per trend segment at M5 = 72-360 bars.
            segment_left = int(rng.integers(72, 360))
            drift = float(rng.normal(0.0, 0.022))

        vol = session_volatility(moment)
        step = float(rng.normal(drift, vol))
        open_price = price
        close_price = open_price + step

        # Wicks: a fraction of the bar's move plus noise, so highs/lows are always
        # consistent with open/close by construction.
        span = abs(step)
        up_wick = abs(float(rng.normal(0.0, vol * 0.55))) + span * 0.18
        dn_wick = abs(float(rng.normal(0.0, vol * 0.55))) + span * 0.18
        high = max(open_price, close_price) + up_wick
        low = min(open_price, close_price) - dn_wick

        volume = int(abs(rng.normal(420, 160)) * (vol / 0.7)) + 20

        rows.append(
            {
                "open_time": moment.isoformat(),
                "open": round(open_price, DIGITS),
                "high": round(high, DIGITS),
                "low": round(low, DIGITS),
                "close": round(close_price, DIGITS),
                "tick_volume": volume,
            }
        )

        price = close_price
        segment_left -= 1
        moment += timedelta(minutes=5)

    # Rounding can push open/close a hair outside the rounded high/low; repair so
    # every row satisfies the Candle model's OHLC validator.
    for row in rows:
        row["high"] = round(max(row["high"], row["open"], row["close"]), DIGITS)
        row["low"] = round(min(row["low"], row["open"], row["close"]), DIGITS)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    rows = generate(args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["open_time", "open", "high", "low", "close", "tick_volume"]
        )
        writer.writeheader()
        writer.writerows(rows)

    first, last = rows[0]["open_time"], rows[-1]["open_time"]
    print(f"wrote {args.out.relative_to(REPO_ROOT)}: {len(rows)} candles, {first} .. {last}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
