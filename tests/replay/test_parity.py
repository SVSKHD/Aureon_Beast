"""Replay/live parity (§82).

The claim under test: **the same candles produce the same detections whether they
arrive from a file or from a live feed.** Without it, nothing learned from replay
says anything about live behaviour, and the whole research loop is decoration.

Three comparisons:

1. replay twice -- determinism;
2. replay vs a live-shaped feed -- the actual parity claim;
3. full detection payloads, not just ids -- because an id that matches while the
   stored indicator snapshot differs would still make replay and live
   incomparable, and the id deliberately excludes the snapshot.
"""

from __future__ import annotations

from typing import Any

import pytest

from aureon.engine.analysis_engine import AnalysisEngine
from aureon.engine.market_engine import MarketEngine
from aureon.engine.replay_engine import ReplayEngine
from aureon.models.detection import Detection
from aureon.models.enums import Timeframe
from aureon.models.market import Candle
from tests.conftest import ACCOUNT_SCOPE, MARKET_TZ, FakeLiveProvider, cross_agent


def build_engine() -> AnalysisEngine:
    return AnalysisEngine(
        [cross_agent()], account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ
    )


def comparable(detection: Detection) -> dict[str, Any]:
    """The full stored form, for an exact comparison."""
    return detection.model_dump(mode="json")


def run_replay(historical: Any) -> list[Detection]:
    engine = build_engine()
    replay = ReplayEngine(historical, engine, symbol="XAUUSD", timeframe=Timeframe.M5)
    return replay.run()


def run_live(candles: list[Candle]) -> list[Detection]:
    """Drive MarketEngine candle by candle, as a live poll loop would.

    The clock is advanced to just past each candle's close and the engine polled, so
    the live path sees candles appear one at a time and never sees a forming bar.
    """
    provider = FakeLiveProvider(candles)
    engine = build_engine()
    collected: list[Detection] = []
    market = MarketEngine(
        provider,
        engine,
        symbols=["XAUUSD"],
        timeframes=[Timeframe.M5],
        on_detections=collected.extend,
    )
    # Start the cursor before the first candle so nothing is skipped and the live
    # run covers exactly the same range as the replay.
    first = candles[0]
    market.seed_cursor(
        "XAUUSD", Timeframe.M5, first.open_time.utc - (first.close_time - first.open_time.utc)
    )
    for candle in candles:
        provider.advance_to_close_of(candle)
        market.poll_once()
    return collected


def test_replay_is_deterministic(historical: Any) -> None:
    first = run_replay(historical)
    second = run_replay(historical)
    assert first, "the fixture produced no detections; parity would be vacuous"
    assert [d.detection_id for d in first] == [d.detection_id for d in second]
    assert [comparable(d) for d in first] == [comparable(d) for d in second]


def test_live_matches_replay_detection_for_detection(
    historical: Any, candles: list[Candle]
) -> None:
    """The parity claim itself."""
    replayed = run_replay(historical)
    live = run_live(candles)

    assert [d.detection_id for d in live] == [d.detection_id for d in replayed]
    assert [d.event_key for d in live] == [d.event_key for d in replayed]
    assert [comparable(d) for d in live] == [comparable(d) for d in replayed]


def test_parity_covers_a_meaningful_number_of_detections(historical: Any) -> None:
    """Guards against parity passing because both sides produced nothing.

    A test that compares two empty lists passes forever and protects nothing, so the
    fixture is asserted to actually exercise the agent.
    """
    detections = run_replay(historical)
    assert len(detections) >= 20, f"only {len(detections)} detections; fixture too quiet"
    assert {d.event_key for d in detections} == {"bullish", "bearish"}


def test_detection_ids_are_unique_across_a_week(historical: Any) -> None:
    """A collision would silently merge two market events into one document."""
    detections = run_replay(historical)
    ids = [d.detection_id for d in detections]
    assert len(set(ids)) == len(ids)


def test_the_engine_buffer_size_does_not_change_an_agents_output(
    candles: list[Candle],
) -> None:
    """The engine insulates each agent from how big its buffer happens to be.

    The indicators themselves remain window-dependent -- that property is pinned in
    ``test_indicators.test_ema_depends_on_how_much_history_it_is_given``, and it is
    why a fixed window is a correctness parameter at all. What this test pins is the
    protection built on top of it (decision 37): the engine hands each agent exactly
    ``agent.min_window()`` bars, so a wider buffer, whether from ``window_margin`` or
    from a hungrier sibling agent, cannot reach the agent and change what it detects.

    Without this, registering the liquidity agent (583 bars) beside the cross agent
    (64) would silently rewrite the cross agent's history and invalidate the recorded
    Phase 2 baseline.
    """
    agent = cross_agent()
    wide = AnalysisEngine(
        [agent], account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ, window_margin=200
    )
    narrow = AnalysisEngine([agent], account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ)
    assert wide.window_size > narrow.window_size, "the buffers must actually differ"

    wide_detections = wide.feed(candles)
    narrow_detections = narrow.feed(candles)
    assert wide_detections, "no detections; the comparison would be vacuous"
    assert [comparable(d) for d in wide_detections] == [
        comparable(d) for d in narrow_detections
    ]


def test_live_ignores_the_forming_bar(candles: list[Candle]) -> None:
    """A poll mid-candle must not see the bar that is still forming."""
    provider = FakeLiveProvider(candles)
    target = candles[100]
    # Clock sits INSIDE the target candle: it has opened but not closed.
    provider.set_clock(target.open_time.utc)
    visible = provider.get_closed_candles(
        "XAUUSD", Timeframe.M5, candles[0].open_time.utc, target.close_time
    )
    assert all(c.close_time <= target.open_time.utc for c in visible)
    assert target.open_time.utc not in {c.open_time.utc for c in visible}


@pytest.mark.parametrize("restart_at", [200, 600, 1200])
def test_splitting_a_replay_does_not_change_its_detections(
    candles: list[Candle], restart_at: int
) -> None:
    """Processing a week in two passes must equal processing it in one.

    The engine's window is rebuilt from the candles it is fed, so a split that does
    not re-feed enough history would silently change the indicators. This is the
    property the observer's backfill relies on at every restart.
    """
    whole = build_engine().feed(candles)

    engine = build_engine()
    engine.feed(candles[:restart_at])
    # A real restart re-feeds the window's worth of history before resuming, which is
    # what the observer's backfill does from its cursor.
    resumed_from = max(0, restart_at - engine.window_size)
    engine.reset()
    engine.feed(candles[resumed_from:restart_at])
    tail = engine.feed(candles[restart_at:])

    whole_tail = [d for d in whole if d.candle_open_time.utc >= candles[restart_at].open_time.utc]
    assert [d.detection_id for d in tail] == [d.detection_id for d in whole_tail]


# ── Part B: every agent must hold parity, not just the cross agent ────────────


def all_agents() -> list:
    """The full roster, with liquidity and breakout sharing one LevelTracker."""
    from aureon.agents.breakout_agent import BreakoutAgent
    from aureon.agents.liquidity_agent import LiquidityAgent
    from aureon.agents.rsi_agent import RsiAgent
    from aureon.agents.session_trend_agent import SessionTrendAgent
    from aureon.agents.wick_agent import WickAgent
    from aureon.engine.levels import LevelTracker

    levels = LevelTracker()
    return [
        cross_agent(),
        RsiAgent(),
        SessionTrendAgent(),
        WickAgent(),
        LiquidityAgent(level_tracker=levels),
        BreakoutAgent(level_tracker=levels),
    ]


AGENT_NAMES = ["ema_cross", "rsi", "session_trend", "wick", "liquidity", "breakout"]


@pytest.mark.parametrize("agent_name", AGENT_NAMES)
def test_every_agent_holds_replay_live_parity(
    agent_name: str, historical: Any, candles: list[Candle]
) -> None:
    """§82 for the whole roster.

    Run once with every agent registered, so the comparison also covers the engine's
    per-agent slicing: if slicing leaked between agents, the live and replay paths
    would still agree with each other, but a single-agent run would disagree with the
    combined run -- which the next test checks.
    """
    replay_engine = AnalysisEngine(
        all_agents(), account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ
    )
    replayed = [d for d in replay_engine.feed(candles) if d.agent_name == agent_name]

    provider = FakeLiveProvider(candles)
    live_engine = AnalysisEngine(
        all_agents(), account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ
    )
    collected: list[Detection] = []
    market = MarketEngine(
        provider,
        live_engine,
        symbols=["XAUUSD"],
        timeframes=[Timeframe.M5],
        on_detections=collected.extend,
    )
    first = candles[0]
    market.seed_cursor(
        "XAUUSD", Timeframe.M5, first.open_time.utc - (first.close_time - first.open_time.utc)
    )
    for candle in candles:
        provider.advance_to_close_of(candle)
        market.poll_once()
    live = [d for d in collected if d.agent_name == agent_name]

    assert replayed, f"{agent_name} produced no detections; parity would be vacuous"
    assert [d.detection_id for d in live] == [d.detection_id for d in replayed]
    assert [comparable(d) for d in live] == [comparable(d) for d in replayed]


@pytest.mark.parametrize("agent_name", AGENT_NAMES)
def test_an_agent_is_unaffected_by_its_siblings(
    agent_name: str, candles: list[Candle]
) -> None:
    """Adding an agent must not change what another agent detects.

    This is what the engine's per-agent window slicing buys. Without it, registering
    the liquidity agent (583 bars) alongside the cross agent (64) would widen the
    shared window and silently rewrite the cross agent's history -- invalidating the
    recorded Phase 2 baseline just by adding a sibling.
    """
    solo_agent = next(a for a in all_agents() if a.agent_name == agent_name)
    solo = AnalysisEngine(
        [solo_agent], account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ
    ).feed(candles)

    combined_engine = AnalysisEngine(
        all_agents(), account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ
    )
    combined = [
        d for d in combined_engine.feed(candles) if d.agent_name == agent_name
    ]

    assert [comparable(d) for d in solo] == [comparable(d) for d in combined]
