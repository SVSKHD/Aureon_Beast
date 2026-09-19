#!/usr/bin/env python3
"""Generate ``docs/PHASE2_BASELINE.md`` from a replay of the fixture.

The Phase 2 gate asks for stable counts recorded from one week of replay. Those
numbers matter beyond documentation: Phase 3 reconciles its evaluation counts against
them, and Phase 7's weekly review must agree with them for the same week. A silent
change here means something changed in the agent, the indicators, or the fixture.

Deterministic, like ``gen_contracts.py``: no timestamp in the output, so a re-run
produces no diff unless the detections actually changed.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.agents.ema_cross_agent import EmaCrossAgent  # noqa: E402
from aureon.config.sessions import SESSION_CONFIG_VERSION  # noqa: E402
from aureon.data.historical_provider import HistoricalDataProvider  # noqa: E402
from aureon.engine.analysis_engine import AnalysisEngine  # noqa: E402
from aureon.engine.indicators import min_warmup  # noqa: E402

FIXTURE = REPO_ROOT / "aureon" / "data" / "fixtures" / "XAUUSD_M5.csv"
OUTPUT = REPO_ROOT / "docs" / "PHASE2_BASELINE.md"
MARKET_TZ = "Europe/Athens"
ACCOUNT_SCOPE = "primary"


def build() -> str:
    provider = HistoricalDataProvider(FIXTURE, market_tz=MARKET_TZ)
    candles = provider.candles
    agent = EmaCrossAgent()
    engine = AnalysisEngine(
        [agent], account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ
    )
    detections = engine.feed(candles)

    by_event = Counter(d.event_key for d in detections)
    by_session = Counter(d.session.session.value for d in detections)
    by_day = Counter(d.detected_at.market_date for d in detections)

    gaps = [
        (a.open_time.utc, b.open_time.utc)
        for a, b in zip(candles, candles[1:], strict=False)
        if (b.open_time.utc - a.open_time.utc).total_seconds() > 300
    ]

    lines = [
        "# Phase 2 baseline",
        "",
        "**Generated — do not edit by hand.** Run `python scripts/gen_baseline.py`.",
        "",
        "Counts from replaying the committed fixture through the observer's agents.",
        "Phase 3 reconciles its evaluation counts against these, and Phase 7's weekly",
        "review must agree with them for the same week. A change here without a",
        "corresponding change to an agent, the indicators or the fixture is a bug.",
        "",
        "## Input",
        "",
        "| property | value |",
        "|---|---|",
        f"| fixture | `{FIXTURE.relative_to(REPO_ROOT)}` (synthetic, decision 28) |",
        f"| candles | {len(candles)} |",
        "| symbol / timeframe | XAUUSD M5 |",
        f"| first candle open | `{candles[0].open_time.utc.isoformat()}` |",
        f"| last candle open | `{candles[-1].open_time.utc.isoformat()}` |",
        f"| market timezone | `{MARKET_TZ}` |",
        f"| account scope | `{ACCOUNT_SCOPE}` |",
        f"| session config version | {SESSION_CONFIG_VERSION} |",
        f"| gaps > 1 candle | {len(gaps)} |",
        "",
    ]
    if gaps:
        lines += ["Gaps (weekend discontinuity):", ""]
        for start, end in gaps:
            hours = (end - start).total_seconds() / 3600
            lines.append(f"- `{start.isoformat()}` → `{end.isoformat()}` ({hours:.1f}h)")
        lines.append("")

    lines += [
        "## Agent configuration",
        "",
        "| property | value |",
        "|---|---|",
        f"| agent | `{agent.agent_name}` v{agent.agent_version} |",
        f"| fast / slow EMA | {agent.fast_period} / {agent.slow_period} |",
        f"| RSI period | {agent.rsi_period} (context only, never a gate) |",
        f"| warm-up bars | {min_warmup(agent.slow_period)} (slow × 3) |",
        f"| engine window | {engine.window_size} bars (fixed; parity contract) |",
        "",
        "## Detections",
        "",
        "| metric | count |",
        "|---|---|",
        f"| total | {len(detections)} |",
        f"| bullish crosses | {by_event.get('bullish', 0)} |",
        f"| bearish crosses | {by_event.get('bearish', 0)} |",
        f"| unique detection ids | {len({d.detection_id for d in detections})} |",
        "",
        "### By session",
        "",
        "| session | detections |",
        "|---|---|",
    ]
    for session in ("asia", "london", "new_york", "off"):
        lines.append(f"| `{session}` | {by_session.get(session, 0)} |")

    lines += [
        "",
        "### By broker trading day",
        "",
        "| market date | detections |",
        "|---|---|",
    ]
    for day in sorted(by_day):
        lines.append(f"| `{day}` | {by_day[day]} |")

    lines += [
        "",
        "## Not yet measured",
        "",
        "Part B agents (RSI context, session trend, liquidity sweeps, wick rejections,",
        "breakouts) are not implemented, so sweeps-per-session and breakouts-per-session",
        "are absent from this table. They are added here as each agent lands, alongside",
        "its own parity test.",
        "",
        "Phase 3 adds reached-3/5/10 counts from COMPLETE horizons only, reported",
        "separately from PENDING counts.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit non-zero if stale")
    args = parser.parse_args()

    content = build()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != content:
            print(
                f"{OUTPUT.relative_to(REPO_ROOT)} is stale; run "
                "`python scripts/gen_baseline.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT.relative_to(REPO_ROOT)} is up to date.")
        return 0

    OUTPUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
