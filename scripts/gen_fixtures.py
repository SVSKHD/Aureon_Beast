#!/usr/bin/env python3
"""Generate the M5 candle fixtures for replay and tests (XAUUSD, XAGUSD).

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

Prices are rounded to the symbol's digits (gold 2, silver 3).

## The second symbol is not a scaled copy of the first

XAGUSD uses its own seed. A rescaled gold series would make the two symbols perfectly
correlated, and every multi-symbol test would then pass for a reason that has nothing to do
with the code: two engines fed the same shape produce the same shape. What IS shared is the
structure -- the same market hours, the same session volatility profile, the same trend
segmentation -- because those are facts about the trading week rather than about gold.

Silver's per-candle volatility is gold's scaled by ``price_scale``: the ratio of prices
(~30/2400) times ~1.6, because silver's daily range is a larger share of its price than
gold's (~2% vs ~1.25%). That 1.6 is the same factor decision 143 records, from the other
end. Tick VOLUME is deliberately not scaled -- it is a count of ticks, not a price.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "aureon" / "data" / "fixtures"

SEED = 20260918


@dataclass(frozen=True)
class SymbolSpec:
    """What differs between the two fixtures, and nothing that does not."""

    symbol: str
    start_price: float
    digits: int
    #: Multiplies gold's per-candle volatility and drift. 1.0 keeps XAUUSD's series
    #: BYTE-IDENTICAL to the committed file, which the reproducibility test pins.
    price_scale: float
    seed: int


SPECS: dict[str, SymbolSpec] = {
    "XAUUSD": SymbolSpec("XAUUSD", 2400.00, 2, 1.0, SEED),
    # ~30/2400 for the price, times ~1.6 because silver's daily range is a larger share
    # of its price than gold's. Its own seed: a rescaled copy of gold would make every
    # multi-symbol test pass for a reason unrelated to the code.
    "XAGUSD": SymbolSpec("XAGUSD", 30.00, 3, 0.02, SEED + 1),
}

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


def generate(
    seed: int | None = None, *, spec: SymbolSpec | None = None
) -> list[dict[str, object]]:
    spec = spec or SPECS["XAUUSD"]
    rng = np.random.default_rng(SEED if seed is None else seed)

    # Start at the Sunday 22:00 UTC open preceding a fixed Monday.
    start = datetime(2026, 9, 13, 22, 0, tzinfo=UTC)  # Sunday
    # Deliberately past Friday's close and into the NEXT week's open, so the file
    # actually contains the weekend discontinuity (Fri 21:00 -> Sun 22:00, ~49h).
    # Ending at Friday's close would leave the gap handling and the INVALID-horizon
    # path with no data to exercise them.
    end = start + timedelta(days=8, hours=14)

    # Trend segments: each is (bars_remaining, drift_per_bar). Reversing drift is
    # what produces genuine EMA crossovers rather than noise around a flat mean.
    price = spec.start_price
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
            drift = float(rng.normal(0.0, 0.022 * spec.price_scale))

        # Two volatilities on purpose: `vol` is the session's activity level, which drives
        # tick VOLUME and is a count rather than a price; `price_vol` is that same level
        # expressed in the symbol's own money.
        vol = session_volatility(moment)
        price_vol = vol * spec.price_scale
        step = float(rng.normal(drift, price_vol))
        open_price = price
        close_price = open_price + step

        # Wicks: a fraction of the bar's move plus noise, so highs/lows are always
        # consistent with open/close by construction.
        span = abs(step)
        up_wick = abs(float(rng.normal(0.0, price_vol * 0.55))) + span * 0.18
        dn_wick = abs(float(rng.normal(0.0, price_vol * 0.55))) + span * 0.18
        high = max(open_price, close_price) + up_wick
        low = min(open_price, close_price) - dn_wick

        volume = int(abs(rng.normal(420, 160)) * (vol / 0.7)) + 20

        rows.append(
            {
                "open_time": moment.isoformat(),
                "open": round(open_price, spec.digits),
                "high": round(high, spec.digits),
                "low": round(low, spec.digits),
                "close": round(close_price, spec.digits),
                "tick_volume": volume,
            }
        )

        price = close_price
        segment_left -= 1
        moment += timedelta(minutes=5)

    # Rounding can push open/close a hair outside the rounded high/low; repair so
    # every row satisfies the Candle model's OHLC validator.
    for row in rows:
        row["high"] = round(max(row["high"], row["open"], row["close"]), spec.digits)
        row["low"] = round(min(row["low"], row["open"], row["close"]), spec.digits)
    return rows


def fixture_path(symbol: str) -> Path:
    return FIXTURE_DIR / f"{symbol}_M5.csv"


def write(spec: SymbolSpec, out: Path, *, seed: int | None = None) -> int:
    rows = generate(seed if seed is not None else spec.seed, spec=spec)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["open_time", "open", "high", "low", "close", "tick_volume"]
        )
        writer.writeheader()
        writer.writerows(rows)

    first, last = rows[0]["open_time"], rows[-1]["open_time"]
    print(f"wrote {out.relative_to(REPO_ROOT)}: {len(rows)} candles, {first} .. {last}")
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbol",
        default="all",
        choices=("all", *SPECS),
        help="which fixture to write (default: every one)",
    )
    parser.add_argument("--out", type=Path, default=None, help="only with --symbol")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    if args.symbol == "all":
        if args.out is not None:
            parser.error("--out needs a single --symbol")
        for spec in SPECS.values():
            write(spec, fixture_path(spec.symbol), seed=args.seed)
        return 0

    spec = SPECS[args.symbol]
    write(spec, args.out or fixture_path(spec.symbol), seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
