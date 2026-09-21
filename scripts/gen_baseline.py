#!/usr/bin/env python3
"""Generate ``docs/PHASE2_BASELINE.md`` from a replay of the fixture.

The Phase 2 gate asks for stable counts recorded from one week of replay. Those
numbers matter beyond documentation: Phase 3 reconciles its evaluation counts against
them, and Phase 7's weekly review must agree with them for the same week. A silent
change here means something changed in the agent, the indicators, or the fixture.

Deterministic, like ``gen_contracts.py``: no timestamp in the output, so a re-run
produces no diff unless the detections actually changed.

## Both symbols (9A)

XAUUSD is the whole document above the XAGUSD section; XAGUSD gets its own section of the
same shape, replayed from its own fixture with its own tuning entry and evaluated under its
own frozen rule. The two are **not** comparable threshold for threshold -- `XAG_OUTCOME_V1`'s
$0.10 is roughly the fraction of silver's price that $8 is of gold's (decision 142) -- so
what a reader may compare is the shape: how many detections each roster selects, and how
often each rule's own thresholds were reached.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from aureon.agents.breakout_agent import BreakoutAgent  # noqa: E402
from aureon.agents.ema_cross_agent import EmaCrossAgent  # noqa: E402
from aureon.agents.liquidity_agent import LiquidityAgent  # noqa: E402
from aureon.agents.rsi_agent import RsiAgent  # noqa: E402
from aureon.agents.session_trend_agent import SessionTrendAgent  # noqa: E402
from aureon.agents.wick_agent import WickAgent  # noqa: E402
from aureon.config.sessions import SESSION_CONFIG_VERSION  # noqa: E402
from aureon.data.historical_provider import HistoricalDataProvider  # noqa: E402
from aureon.engine.analysis_engine import AnalysisEngine  # noqa: E402
from aureon.engine.indicators import min_warmup  # noqa: E402
from aureon.engine.levels import LevelTracker  # noqa: E402
from aureon.evaluation.backfill import (  # noqa: E402
    BackfillResult,
    build_report,
    diagnose,
    run_backfill,
)
from aureon.evaluation.outcome_report import aggregate, render_markdown  # noqa: E402
from aureon.evaluation.rules import (  # noqa: E402
    EMA_OUTCOME_V1,
    XAG_OUTCOME_V1,
    XAU_OUTCOME_V2,
)
from aureon.reviews.reconcile import reconcile  # noqa: E402

FIXTURE = REPO_ROOT / "aureon" / "data" / "fixtures" / "XAUUSD_M5.csv"
SILVER_FIXTURE = REPO_ROOT / "aureon" / "data" / "fixtures" / "XAGUSD_M5.csv"
SILVER = "XAGUSD"
OUTPUT = REPO_ROOT / "docs" / "PHASE2_BASELINE.md"
_FAILURES: list[str] = []
MARKET_TZ = "Europe/Athens"
ACCOUNT_SCOPE = "primary"
#: XAUUSD tick. The V2 thresholds are in PRICE, so this is what converts them.
POINT = 0.01

# The shipped pair (AUREON_EMA_FAST / AUREON_EMA_SLOW defaults) and the pair that
# shipped before it. Both are replayed: the current one is the baseline Phase 3 and
# Phase 7 reconcile against, and the historical one stays so the change from 9/21 to
# 20/50 is a visible diff in counts rather than a claim in a commit message.
EMA_FAST = 20
EMA_SLOW = 50
HISTORICAL_EMA = (9, 21)


def roster() -> list:
    """A FRESH whole-roster agent list.

    Fresh every call, never reused: ``LevelTracker`` and every agent carry state from
    the candles they have seen, so feeding one roster to two replays would make the
    second one's counts depend on the first having run.
    """
    levels = LevelTracker()  # liquidity and breakout share ONE tracker (§15, §17)
    return [
        EmaCrossAgent(fast_period=EMA_FAST, slow_period=EMA_SLOW),
        RsiAgent(),
        SessionTrendAgent(),
        WickAgent(),
        LiquidityAgent(level_tracker=levels),
        BreakoutAgent(level_tracker=levels),
    ]


def build() -> str:
    provider = HistoricalDataProvider(FIXTURE, market_tz=MARKET_TZ)
    candles = provider.candles

    agents = roster()
    agent = agents[0]
    engine = AnalysisEngine(agents, account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ)
    all_detections = engine.feed(candles)

    by_agent = Counter(d.agent_name for d in all_detections)
    # The cross agent's own numbers, kept broken out because Phase 3 evaluates
    # EMA_OUTCOME_V1 against exactly these.
    detections = [d for d in all_detections if d.agent_name == "ema_cross"]
    by_event = Counter(d.event_key for d in detections)
    by_session = Counter(d.session.session.value for d in detections)
    by_day = Counter(d.detected_at.market_date for d in detections)

    SESSIONS = ("asia", "london", "new_york", "off")

    def per_session(agent_name: str) -> Counter:
        return Counter(
            d.session.session.value
            for d in all_detections
            if d.agent_name == agent_name
        )

    def per_event(agent_name: str) -> Counter:
        return Counter(
            d.event_key for d in all_detections if d.agent_name == agent_name
        )

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

    # ── Part B ────────────────────────────────────────────────────────────────
    lines += [
        "",
        "---",
        "",
        "## All agents",
        "",
        "Counts from one run with the whole roster registered. Because the engine gives",
        "each agent its own window slice, these are identical to running each agent",
        "alone -- adding an agent never changes another's output.",
        "",
        "| agent | version | window | detections |",
        "|---|---|---|---|",
    ]
    for registered in agents:
        lines.append(
            f"| `{registered.agent_name}` | {registered.agent_version} | "
            f"{registered.min_window()} | {by_agent.get(registered.agent_name, 0)} |"
        )

    lines += [
        "",
        "### Crosses, sweeps and breakouts per session",
        "",
        "The three counts the Phase 2 gate asks to be recorded.",
        "",
        "| session | crosses | sweeps | breakouts |",
        "|---|---|---|---|",
    ]
    sweeps_by_session = per_session("liquidity")
    breaks_by_session = per_session("breakout")
    for session in SESSIONS:
        lines.append(
            f"| `{session}` | {by_session.get(session, 0)} | "
            f"{sweeps_by_session.get(session, 0)} | {breaks_by_session.get(session, 0)} |"
        )
    lines.append(
        f"| **total** | **{len(detections)}** | **{sum(sweeps_by_session.values())}** | "
        f"**{sum(breaks_by_session.values())}** |"
    )

    for title, agent_name in (
        ("Liquidity sweeps by level", "liquidity"),
        ("Breakouts by level", "breakout"),
        ("RSI zone transitions", "rsi"),
        ("Session trends", "session_trend"),
        ("Wick rejections", "wick"),
    ):
        events = per_event(agent_name)
        lines += ["", f"### {title}", "", "| event_key | count |", "|---|---|"]
        for key in sorted(events):
            lines.append(f"| `{key}` | {events[key]} |")

    # ── Phase 3: outcomes ─────────────────────────────────────────────────────
    rule = EMA_OUTCOME_V1
    backfill = run_backfill(
        candles,
        [EmaCrossAgent(fast_period=EMA_FAST, slow_period=EMA_SLOW)],
        rule,
        account_scope=ACCOUNT_SCOPE,
        market_tz=MARKET_TZ,
        point=POINT,
    )
    reports = build_report(backfill.evaluations, rule)

    lines += [
        "",
        "---",
        "",
        "## Detection outcomes — `EMA_OUTCOME_V1` (HISTORICAL — invalid for research)",
        "",
        "**Do not read these numbers as a result.** At `point = 0.01` this rule's",
        "3-20 are $0.03-$0.20, well inside a single XAUUSD M5 candle, so almost every",
        "threshold is crossed on the first bar. The table measures the instrument's tick",
        "size, not the strategy, and its ~100% columns are an artefact of the scale.",
        "",
        "It is kept, regenerated, for two reasons: results were stored under this",
        "`rule_id` and a rule is frozen once shipped (§21, decision 47), and the",
        "`XAU_OUTCOME_V2` section below is only interpretable next to what it replaced.",
        "**`XAU_OUTCOME_V2` is the rule to read**, and it is what the running system",
        "evaluates against (`AUREON_EVAL_RULE`).",
        "",
        f"Rule `{rule.rule_id}`, frozen: reference `{rule.reference_price.value}`,",
        f"thresholds {list(rule.thresholds)} in **points**.",
        f"{backfill.evaluated} `ema_cross` detections evaluated.",
        "",
        "**Reached counts come from COMPLETE horizons only.** Pending and invalid are",
        "reported separately and are never counted as misses — an unknown answer is not",
        "a failure, and treating it as one is the easiest way to make a strategy look",
        "worse than it is.",
        "",
        "| horizon | complete | pending | invalid | reach 3 | reach 5 | reach 10 |",
        "|---|---|---|---|---|---|---|",
    ]
    for horizon in rule.horizons:
        report = reports[horizon.id]

        def cell(key: str, report=report) -> str:
            entry = report.thresholds[key]
            if entry.rate is None:
                return "— (no data)"
            return f"{entry.reached}/{entry.evaluated} ({entry.rate * 100:.0f}%)"

        lines.append(
            f"| `{report.horizon_id}` | {report.complete} | {report.pending} | "
            f"{report.invalid} | {cell('3')} | {cell('5')} | {cell('10')} |"
        )

    lines += [
        "",
        "### Path classification (COMPLETE only)",
        "",
        "| horizon | MFE_FIRST | MAE_FIRST | NONE | ambiguous |",
        "|---|---|---|---|---|",
    ]
    for horizon in rule.horizons:
        report = reports[horizon.id]
        lines.append(
            f"| `{report.horizon_id}` | {report.mfe_first} | {report.mae_first} | "
            f"{report.path_none} | {report.path_ambiguous} |"
        )

    warnings = diagnose(reports, rule)
    if warnings:
        lines += [
            "",
            "### Diagnostics — read before using these numbers",
            "",
        ]
        for warning in warnings:
            lines.append(f"- {warning}")
        lines += [
            "",
            "In short: at `point = 0.01` the specified thresholds are $0.03–$0.20, well",
            "inside a single XAUUSD M5 candle's range, so they are crossed on the first",
            "candle almost every time. The 100% columns above measure the scale, not the",
            "strategy. Correcting it means a **new `rule_id`**, never an edit to this one",
            "(§21, decision 47) — which is what `XAU_OUTCOME_V2` below is.",
        ]

    # ── P-1: XAU_OUTCOME_V2, the rule to read ────────────────────────────────
    # ONE whole-roster replay, used twice: for the per-agent section and, sliced to
    # ema_cross, for the reconciliation below. Re-running it would cost half a minute
    # and could not produce different numbers -- the engine gives each agent its own
    # window slice, and a detection's horizons are evaluated independently of every
    # other agent's detections.
    v2 = run_backfill(
        candles,
        roster(),
        XAU_OUTCOME_V2,
        account_scope=ACCOUNT_SCOPE,
        market_tz=MARKET_TZ,
        point=POINT,
    )
    lines += _v2_section(v2)

    # ── 9B: outcomes split by tick-volume and volatility context ─────────────
    lines += _context_outcomes_section(v2, XAU_OUTCOME_V2, "XAUUSD")

    # ── Historical: the pair that shipped before 20/50 ────────────────────────
    lines += _historical_section(candles)

    # ── Phase 7: weekly reviews must agree ───────────────────────────────────
    # Against V2, because that is the rule the reviews actually run
    # (AUREON_EVAL_RULE defaults to XAU_OUTCOME_V2). Reconciling V1 would prove two
    # code paths agree about a rule nothing evaluates any more -- and would agree
    # trivially, since every V1 reached count is saturated at 100%.
    cross = BackfillResult(
        detections=[d for d in v2.detections if d.agent_name == "ema_cross"],
        evaluations=[
            e
            for e in v2.evaluations
            if e.detection_id in {d.detection_id for d in v2.detections
                                  if d.agent_name == "ema_cross"}
        ],
    )
    lines += _reconciliation_section(
        cross,
        build_report(cross.evaluations, XAU_OUTCOME_V2),
        XAU_OUTCOME_V2,
        by_day,
    )

    # ── XAGUSD: the same shape, its own numbers (9A) ─────────────────────────
    lines += _silver_section(v2)

    lines += [
        "",
        "---",
        "",
        "## Not yet measured",
        "",
        "* **Anything from a real session.** Every number in this document is a replay of",
        "  a synthetic fixture (decision 28). It proves the pipeline computes what it says",
        "  it computes; it says nothing about gold. `docs/MT5_SESSION_CHECKLIST.md` is the",
        "  manual leg that turns this into evidence about a market.",
        "* **Whether the thresholds discriminate.** $3-$20 is a distance a gold trader",
        "  holds for, not a distance a study showed separates good detections from bad.",
        "* **Whether the level and wick parameters select anything real.** They are",
        "  v1.0.0 placeholders researched on nothing (decision 120), so every `liquidity`,",
        "  `breakout`, `wick` and `session_trend` count here is a count of what those",
        "  arbitrary numbers happened to select.",
        "* **Anything about silver specifically.** XAGUSD's tuning is gold's thresholds",
        "  scaled by the ratio of prices and `XAG_OUTCOME_V1` is gold's distances scaled the",
        "  same way (decisions 142, 143). Both are arithmetic, not research, and its fixture",
        "  is a second random walk rather than a second market.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _v2_section(v2: BackfillResult) -> list[str]:
    """`XAU_OUTCOME_V2` per agent, per session and per horizon (P-1).

    The rule the running system evaluates against, over the same fixture week, with the
    **whole roster** rather than ``ema_cross`` alone -- a sweep and a cross are different
    events and averaging them produces a number that describes neither.

    Everything here comes from ``aureon.evaluation.outcome_report``, the same module
    ``scripts/report_outcomes.py`` renders for a real week, so the document and the
    operator's terminal cannot disagree about a figure.
    """
    rule = XAU_OUTCOME_V2
    report = aggregate(v2.detections, v2.evaluations, rule, point=POINT)
    total = report.total()

    lines = [
        "",
        "---",
        "",
        "## Detection outcomes — `XAU_OUTCOME_V2`",
        "",
        f"**The rule to read.** Reference `{rule.reference_price.value}`, thresholds "
        + " / ".join(f"${t:g}" for t in rule.thresholds)
        + " in **price** (§21),",
        f"converted to points with `point = {POINT}` at evaluation time rather than",
        "written down as a multiplier.",
        "",
        f"{total.detections} detections from the whole roster, {total.evaluated} "
        f"evaluated, {total.with_complete} with at least",
        f"one COMPLETE horizon. {total.context_only} carry `direction=None` and have no",
        "favourable side to measure, so they are not evaluable at all (§16, decision 48)",
        "— they are context for the reviews, not outcomes.",
        "",
        "Read the columns literally:",
        "",
        "* `complete` / `pending` / `invalid` are horizon counts, not detection counts: one",
        "  detection contributes one row to each of the rule's seven horizons.",
        "* every reached count, median and path figure is from **COMPLETE horizons only**;",
        "  `pending` and `invalid` are never folded into a denominator.",
        "* `invalid` here is almost entirely the fixture's 49-hour weekend gap: a horizon",
        "  spanning it cannot be measured, so it is excluded rather than reported as a",
        "  lower bound (§22).",
        "* medians are in **price**, signed. A negative median MFE would mean the best",
        "  price ever offered was still worse than the reference.",
        "* `median t→$5` is over the horizons that **reached** $5, with that count beside",
        "  it. It is not the time a typical detection takes; a non-reach has no duration.",
        "* `MFE_FIRST` + `MAE_FIRST` does not equal `complete`: a horizon where neither",
        "  side ever crossed $3 is classified `NONE` (§23).",
        "* `ambiguous` counts horizons where both sides were first crossed inside one",
        "  candle, so their order was never observed (decision 49).",
    ]
    lines += render_markdown(report)
    return lines


def _historical_section(candles) -> list[str]:
    """The 9/21 counts, kept so the change to 20/50 is visible rather than asserted.

    Regenerated from the same fixture on every run, so it cannot drift into a stale
    claim about what the old pair did. It is NOT the baseline: Phase 3 and Phase 7
    reconcile against the current pair, and nothing here is load-bearing.

    Worth keeping because the two pairs are not a tuning preference. 9/21 crosses
    roughly twice as often on this fixture, and each of those crosses is a different
    market event with a different outcome -- so a reader comparing a Phase 3 report
    from before this change with one from after needs to see that the denominator
    moved, not just the percentages.
    """
    fast, slow = HISTORICAL_EMA
    agent = EmaCrossAgent(fast_period=fast, slow_period=slow)
    engine = AnalysisEngine([agent], account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ)
    detections = engine.feed(candles)
    by_event = Counter(d.event_key for d in detections)
    by_session = Counter(d.session.session.value for d in detections)

    lines = [
        "",
        "---",
        "",
        f"## Historical: `ema_cross` v1.0.0 at {fast}/{slow}",
        "",
        "**Not the baseline.** The pair that shipped before 20/50, replayed over the same",
        "fixture so the change is a visible diff in counts. Nothing reconciles against",
        "these numbers.",
        "",
        "Under §12 the agent version is part of the detection id, so these detections do",
        "not collide with the current ones -- both pairs can describe the same week.",
        "",
        "| metric | 9/21 (v1.0.0) | 20/50 (v2.0.0) |",
        "|---|---|---|",
    ]
    current = Counter(
        d.event_key
        for d in AnalysisEngine(
            [EmaCrossAgent(fast_period=EMA_FAST, slow_period=EMA_SLOW)],
            account_scope=ACCOUNT_SCOPE,
            market_tz=MARKET_TZ,
        ).feed(candles)
    )
    lines += [
        f"| total crosses | {len(detections)} | {sum(current.values())} |",
        f"| bullish | {by_event.get('bullish', 0)} | {current.get('bullish', 0)} |",
        f"| bearish | {by_event.get('bearish', 0)} | {current.get('bearish', 0)} |",
        f"| warm-up bars | {min_warmup(slow)} | {min_warmup(EMA_SLOW)} |",
        "",
        "### 9/21 by session",
        "",
        "| session | detections |",
        "|---|---|",
    ]
    for session in ("asia", "london", "new_york", "off"):
        lines.append(f"| `{session}` | {by_session.get(session, 0)} |")
    return lines


def _reconciliation_section(backfill, reports, rule, by_day: Counter) -> list[str]:
    """The §63 cross-check: weekly reviews against the counts recorded above.

    The reviews are built from the replay's own detections through
    ``aureon.reviews``, which slices the week on the **broker** clock and takes reached
    counts from COMPLETE horizons only. The right-hand column is the plain
    ``Counter`` from the sections above. Two code paths, one set of numbers.

    Any disagreement is written into the document *and* fails the generator, so the
    baseline cannot be regenerated while it is unreconciled.
    """
    evaluations = {e.detection_id: e for e in backfill.evaluations}
    key = rule.threshold_keys[0]
    result = reconcile(
        backfill.detections,
        evaluations,
        rule,
        market_tz=MARKET_TZ,
        by_market_date=dict(by_day),
        complete_by_horizon={h.id: reports[h.id].complete for h in rule.horizons},
        reached_by_horizon={
            h.id: reports[h.id].thresholds[key].reached for h in rule.horizons
        },
        pending_total=sum(reports[h.id].pending for h in rule.horizons),
        invalid_total=sum(reports[h.id].invalid for h in rule.horizons),
        threshold_key=key,
    )

    lines = [
        "",
        "---",
        "",
        "## Weekly review reconciliation (Phase 7)",
        "",
        "Weekly reviews generated from this same replay, one per ISO week the fixture",
        "spans. The review path counts independently of everything above: half-open",
        "market-clock week windows, reached counts from COMPLETE horizons only. The",
        "numbers must match, and the generator fails if they do not.",
        "",
        "| ISO week | market dates | review detections | baseline days |",
        "|---|---|---|---|",
    ]
    for iso in result.weeks:
        dates = result.per_week_dates[iso]
        expected = sum(by_day[d] for d in dates)
        lines.append(
            f"| `{iso[0]}-W{iso[1]:02d}` | `{dates[0]}` … `{dates[-1]}` | "
            f"{result.per_week_detections[iso]} | {expected} |"
        )
    lines += [
        f"| **total** | {len(by_day)} days | **{result.detections_total}** | "
        f"**{result.baseline_detections_total}** |",
        "",
        "| check | reviews | baseline |",
        "|---|---|---|",
    ]
    for horizon in rule.horizons:
        report = reports[horizon.id]
        lines.append(
            f"| `{horizon.id}` COMPLETE | "
            f"{result.complete_by_horizon.get(horizon.id, 0)} | {report.complete} |"
        )
        lines.append(
            f"| `{horizon.id}` reached at {key} | "
            f"{result.reached_by_horizon.get(horizon.id, 0)} | "
            f"{report.thresholds[key].reached} |"
        )
    lines += [
        f"| PENDING horizons excluded | {result.pending_excluded} | "
        f"{sum(reports[h.id].pending for h in rule.horizons)} |",
        f"| INVALID horizons excluded | {result.invalid_excluded} | "
        f"{sum(reports[h.id].invalid for h in rule.horizons)} |",
        "",
    ]
    if result.reconciled:
        lines.append("Reconciled: every figure above agrees.")
    else:
        lines += ["**NOT RECONCILED:**", ""]
        lines += [f"- {m}" for m in result.mismatches]
        _FAILURES.extend(result.mismatches)
    return lines


def silver_roster() -> list:
    """XAGUSD's roster, with XAGUSD's tuning (9A).

    Built through the observer's own ``default_agents`` rather than assembled here, so the
    baseline is a replay of what the running system would do. The thresholds are not
    dimensionless: gold's roster on silver's candles would be measuring 5-point penetrations
    of an instrument whose tick is a tenth of gold's (decision 143).
    """
    from aureon.config import AureonConfig
    from main_observer import default_agents

    config = AureonConfig(
        symbols=("XAUUSD", SILVER),
        evaluation_rules={"XAUUSD": "XAU_OUTCOME_V2", SILVER: "XAG_OUTCOME_V1"},
        ema_fast=EMA_FAST,
        ema_slow=EMA_SLOW,
    )
    return default_agents(config, symbol=SILVER)


def _silver_section(gold: BackfillResult) -> list[str]:
    """XAGUSD's counts and outcomes, of the same shape as gold's (9A).

    Gold's replay is passed in rather than re-run: the diagnostics compare the two ladders,
    and a comparison against numbers produced by a second replay could differ from the ones
    the document printed above it.
    """
    from aureon.config.symbol_tuning import require_tuning

    point = require_tuning(SILVER).point
    provider = HistoricalDataProvider(SILVER_FIXTURE, market_tz=MARKET_TZ)
    candles = provider.candles

    result = run_backfill(
        candles,
        silver_roster(),
        XAG_OUTCOME_V1,
        account_scope=ACCOUNT_SCOPE,
        market_tz=MARKET_TZ,
        point=point,
    )
    by_agent = Counter(d.agent_name for d in result.detections)
    by_session = Counter(d.session.session.value for d in result.detections)
    by_day = Counter(d.detected_at.market_date for d in result.detections)
    report = aggregate(result.detections, result.evaluations, XAG_OUTCOME_V1, point=point)
    total = report.total()

    lines = [
        "",
        "---",
        "",
        "# XAGUSD",
        "",
        "The same pipeline, the same week's trading hours, a different instrument. Read this",
        "section **beside** gold's rather than against it: `XAG_OUTCOME_V1`'s thresholds are",
        "gold's scaled by the ratio of prices, so $0.10 of silver is not $3 of gold in any",
        "sense a study would recognise (decision 142). What is comparable is the shape --",
        "how much each roster selects, and how often each rule's own thresholds were reached.",
        "",
        "## Input",
        "",
        "| property | value |",
        "|---|---|",
        f"| fixture | `{SILVER_FIXTURE.relative_to(REPO_ROOT)}` (synthetic, decision 148) |",
        f"| candles | {len(candles)} |",
        f"| symbol / timeframe | {SILVER} M5 |",
        f"| tick (`point`) | {point} |",
        f"| outcome rule | `{XAG_OUTCOME_V1.rule_id}` |",
        "| thresholds | " + " / ".join(f"${t:g}" for t in XAG_OUTCOME_V1.thresholds)
        + " in **price** |",
        "",
        "## Detections",
        "",
        f"{len(result.detections)} detections from the whole roster.",
        "",
        "| agent | detections |",
        "|---|---|",
    ]
    lines += [f"| `{name}` | {count} |" for name, count in sorted(by_agent.items())]
    lines += [
        "",
        "### By session",
        "",
        "| session | detections |",
        "|---|---|",
    ]
    lines += [
        f"| {name} | {by_session.get(name, 0)} |"
        for name in ("asia", "london", "new_york", "off")
    ]
    lines += [
        "",
        "### By broker trading day",
        "",
        "| market date | detections |",
        "|---|---|",
    ]
    lines += [f"| {day} | {count} |" for day, count in sorted(by_day.items())]
    lines += [
        "",
        f"## Detection outcomes — `{XAG_OUTCOME_V1.rule_id}`",
        "",
        f"{total.detections} detections, {total.evaluated} evaluated, "
        f"{total.with_complete} with at least",
        f"one COMPLETE horizon. {total.context_only} carry `direction=None` and have no",
        "favourable side to measure (§16, decision 48).",
        "",
        "Every column is read exactly as gold's is: horizon counts rather than detection",
        "counts, reached figures from COMPLETE horizons only, and `invalid` dominated by the",
        "fixture's weekend gap.",
    ]
    lines += render_markdown(report)
    lines += _silver_diagnostics(report, candles, point, gold)
    lines += _context_outcomes_section(result, XAG_OUTCOME_V1, SILVER)
    return lines


def _reach_by_threshold(report, rule) -> tuple[int, dict[float, int]]:
    """COMPLETE horizons, and how many reached each threshold, across the whole roster."""
    from aureon.models.evaluation import threshold_key

    total = report.total()
    complete = sum(h.complete for h in total.horizons.values())
    reached = {
        t: sum(h.reached.get(threshold_key(t), 0) for h in total.horizons.values())
        for t in rule.thresholds
    }
    return complete, reached


def _context_outcomes_section(result: BackfillResult, rule, symbol: str) -> list[str]:
    """Outcomes split by 9B's volume and volatility tags, for one symbol (§19, §23).

    Both sides of every split, always: a "with" figure alone is unreadable until the
    "without" number is beside it. The horizon and threshold are the first of each, named in
    the heading rather than left implicit -- a reached-N split is about one threshold, and a
    table that hides which one invites reading it as all of them.
    """
    from aureon.evaluation.context_tags import (
        REGIME_TAGS,
        TAG_AT_LVN,
        TAG_AT_POC,
        TAG_PRICE_ABOVE_ASIA_VA,
        TAG_PRICE_BELOW_ASIA_VA,
    )
    from aureon.reviews.aggregate import PeriodData, compare_by_tag

    tags = (
        TAG_PRICE_ABOVE_ASIA_VA,
        TAG_PRICE_BELOW_ASIA_VA,
        TAG_AT_POC,
        TAG_AT_LVN,
        *REGIME_TAGS.values(),
    )
    data = PeriodData(
        detections=list(result.detections),
        evaluations={e.detection_id: e for e in result.evaluations},
    )
    comparisons = compare_by_tag(data, rule, tags=tags)
    if not comparisons:
        return []

    horizon = comparisons[0].horizon_id
    threshold = comparisons[0].threshold_key
    lines = [
        "",
        f"### Outcomes by tick-volume and volatility context — {symbol}",
        "",
        f"Horizon `{horizon}`, threshold `{threshold}`, COMPLETE horizons only. Every row",
        "gives both sides, because a `with` rate means nothing until the `without` rate is",
        "beside it — and most rows here are far too small to read as anything but anecdote.",
        "",
        "| tag | with | without |",
        "|---|---|---|",
    ]
    for comparison in sorted(comparisons, key=lambda c: c.tag):
        with_rate = comparison.with_tag_rate
        without_rate = comparison.without_tag_rate
        left = "—" if with_rate is None else f"{with_rate * 100:.0f}%"
        right = "—" if without_rate is None else f"{without_rate * 100:.0f}%"
        note = "" if comparison.comparable else " ⚠︎" 
        lines.append(
            f"| `{comparison.tag}` | {left} "
            f"({comparison.with_tag_reached}/{comparison.with_tag_complete}){note} "
            f"| {right} "
            f"({comparison.without_tag_reached}/{comparison.without_tag_complete}) |"
        )
    lines += [
        "",
        "⚠︎ marks a split with too few COMPLETE horizons on one side to compare at all.",
    ]
    return lines


def _silver_diagnostics(report, candles, point: float, gold: BackfillResult) -> list[str]:
    """What the table above actually shows about `XAG_OUTCOME_V1` (9A).

    Generated from the report rather than written down, so it cannot claim yesterday's
    finding about today's numbers.
    """
    complete, reached = _reach_by_threshold(report, XAG_OUTCOME_V1)
    gold_report = aggregate(gold.detections, gold.evaluations, XAU_OUTCOME_V2, point=POINT)
    gold_complete, gold_reached = _reach_by_threshold(gold_report, XAU_OUTCOME_V2)

    # Fraction of price, which is what makes the two rules' thresholds comparable at all.
    # Taken from the fixture's first close rather than a written-down price.
    silver_price = candles[0].close
    gold_price = HistoricalDataProvider(FIXTURE, market_tz=MARKET_TZ).candles[0].close

    def share(count: int, of: int) -> float | None:
        return count / of if of else None

    dead = [t for t in XAG_OUTCOME_V1.thresholds if complete and reached[t] / complete < 0.01]

    lines = [
        "",
        "### Diagnostics — read before using these numbers",
        "",
        "Each threshold as a fraction of price, beside gold's, because that is the only",
        "sense in which the two rules' distances can be compared:",
        "",
        "| rung | XAGUSD | % of price | reached | XAUUSD | % of price | reached |",
        "|---|---|---|---|---|---|---|",
    ]
    for rung, (silver_t, gold_t) in enumerate(
        zip(XAG_OUTCOME_V1.thresholds, XAU_OUTCOME_V2.thresholds, strict=False), start=1
    ):
        silver_share = share(reached[silver_t], complete)
        gold_share = share(gold_reached[gold_t], gold_complete)
        lines.append(
            f"| {rung} "
            f"| ${silver_t:g} | {silver_t / silver_price * 100:.2f}% "
            f"| {reached[silver_t]}/{complete}"
            + (f" ({silver_share * 100:.1f}%)" if silver_share is not None else "")
            + f" | ${gold_t:g} | {gold_t / gold_price * 100:.2f}% "
            f"| {gold_reached[gold_t]}/{gold_complete}"
            + (f" ({gold_share * 100:.1f}%)" if gold_share is not None else "")
            + " |"
        )

    lines += [
        "",
        "Both `reached` columns are COMPLETE horizons only, whole roster, whole fixture week.",
        "",
    ]

    if dead:
        lines += [
            "**A finding, not a defect to patch.** "
            + " and ".join(f"${t:g}" for t in dead)
            + (" is" if len(dead) == 1 else " are")
            + " reached in under 1% of COMPLETE horizons here, where",
            "gold's fourth and fifth rungs still catch a few per cent. The reason is arithmetic:",
            "`XAG_OUTCOME_V1`'s distances are gold's scaled by the ratio of prices and then",
            "**rounded to numbers a silver trader would name** (decision 142),"
            "and every rung ended up a larger fraction of price than gold's — the table above",
            "gives the multiple, rung by rung, beside what each one actually caught.",
            "",
            "The rule is **frozen** (§21). It is not edited to fix this: a better ladder is a",
            "new `rule_id` evaluated alongside, which is the same discipline that kept",
            "`EMA_OUTCOME_V1`'s saturated columns in this document rather than quietly",
            "rescaling them (decision 121). Until then, read silver's top thresholds as",
            "\"not measured here\" rather than as \"silver does not move\" — and remember the",
            "fixture is a random walk, so none of this is evidence about the metal.",
        ]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit non-zero if stale")
    args = parser.parse_args()

    content = build()
    if _FAILURES:
        # A stale document is recoverable; a document asserting counts that do not
        # reconcile is worse than none, so nothing is written.
        for failure in _FAILURES:
            print(f"reconciliation failed: {failure}", file=sys.stderr)
        return 2
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
