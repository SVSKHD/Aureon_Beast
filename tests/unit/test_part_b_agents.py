"""Part B agents: rsi, session_trend, liquidity, wick, breakout (§14-§18).

Each agent is checked for the properties Phase 2 requires of all of them -- version
stamped, full params snapshot, numeric values stored alongside labels, purity -- and
then for its own rule.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import pandas as pd
import pytest

from aureon.agents.base_agent import WINDOW_COLUMNS, BaseAgent
from aureon.agents.breakout_agent import BreakoutAgent
from aureon.agents.ema_cross_agent import EmaCrossAgent
from aureon.agents.liquidity_agent import LiquidityAgent
from aureon.agents.rsi_agent import (
    ZONE_NEUTRAL,
    ZONE_OVERBOUGHT,
    ZONE_OVERSOLD,
    RsiAgent,
    rsi_zone,
)
from aureon.agents.session_trend_agent import SessionTrendAgent, summary_from_detection
from aureon.agents.wick_agent import WickAgent
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.engine.levels import LevelTracker
from aureon.models.base import MarketTime
from aureon.models.detection import CandleContext, Detection, SessionContext
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.market import Candle
from tests.conftest import ACCOUNT_SCOPE, MARKET_TZ

PART_B = [RsiAgent, SessionTrendAgent, LiquidityAgent, WickAgent, BreakoutAgent]


def run(agent: BaseAgent, candles: list[Candle]) -> list[Detection]:
    engine = AnalysisEngine(
        [agent], account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ
    )
    return engine.feed(candles)


def ctx_for(window: pd.DataFrame, *, session: SessionName = SessionName.LONDON) -> CandleContext:
    stamp = MarketTime.from_utc(window.index[-1].to_pydatetime(), MARKET_TZ)
    return CandleContext(
        account_scope=ACCOUNT_SCOPE,
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        market_tz=MARKET_TZ,
        closed_at=stamp,
        candle_open_time=stamp,
        session=SessionContext(session=session, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
    )


def one_candle(
    *, open_: float, high: float, low: float, close: float, bars: int = 3
) -> pd.DataFrame:
    """A window whose LAST bar has the given shape."""
    index = pd.date_range("2026-09-14 10:00", periods=bars, freq="5min", tz="UTC")
    opens = [open_] * bars
    return pd.DataFrame(
        {
            "open": opens,
            "high": [high] * bars,
            "low": [low] * bars,
            "close": [close] * bars,
            "tick_volume": [100] * bars,
            "real_volume": [0] * bars,
        },
        index=index,
        columns=list(WINDOW_COLUMNS),
    )


# ── Properties every Part B agent must have ───────────────────────────────────


@pytest.mark.parametrize("agent_class", PART_B)
def test_every_agent_is_versioned_and_named(agent_class: type[BaseAgent]) -> None:
    agent = agent_class()
    assert agent.agent_version == "1.0.0"
    assert agent.agent_name not in {"", "base"}


@pytest.mark.parametrize("agent_class", PART_B)
def test_every_agent_snapshots_its_full_configuration(
    agent_class: type[BaseAgent],
) -> None:
    """The reproducibility test: candle + params must be enough to re-derive the output."""
    snapshot = agent_class().params_snapshot()
    assert snapshot
    assert "warmup_bars" in snapshot
    assert all(isinstance(k, str) for k in snapshot)


@pytest.mark.parametrize("agent_class", PART_B)
def test_every_agent_is_pure(agent_class: type[BaseAgent], candles: list[Candle]) -> None:
    """Same candles, same detections -- what replay/live parity rests on."""
    subset = candles[:900]
    first = run(agent_class(), subset)
    second = run(agent_class(), subset)
    assert [d.model_dump(mode="json") for d in first] == [
        d.model_dump(mode="json") for d in second
    ]


@pytest.mark.parametrize("agent_class", PART_B)
def test_every_agent_produces_unique_ids(
    agent_class: type[BaseAgent], candles: list[Candle]
) -> None:
    detections = run(agent_class(), candles)
    ids = [d.detection_id for d in detections]
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("agent_class", PART_B)
def test_every_agent_stores_finite_numbers(
    agent_class: type[BaseAgent], candles: list[Candle]
) -> None:
    """A NaN would serialise to Firestore and then compare unequal to itself."""
    import math

    for detection in run(agent_class(), candles):
        for value in detection.indicators.extras.values():
            assert math.isfinite(value), detection.event_key
        for value in detection.levels.values():
            assert math.isfinite(value), detection.event_key


# ── RSI agent (§14) ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [(75.0, ZONE_OVERBOUGHT), (70.0, ZONE_OVERBOUGHT), (50.0, ZONE_NEUTRAL), (30.0, ZONE_OVERSOLD)],
)
def test_rsi_zone_boundaries_are_inclusive(value: float, expected: str) -> None:
    assert rsi_zone(value, overbought=70.0, oversold=30.0) == expected


def test_rsi_detections_are_context_only(candles: list[Candle]) -> None:
    """§14: RSI never asserts a direction."""
    detections = run(RsiAgent(), candles)
    assert detections
    assert all(d.is_context_only for d in detections)
    assert all(d.direction is None for d in detections)


def test_rsi_fires_on_transitions_not_on_state(candles: list[Candle]) -> None:
    """Emitting while a zone held would drown the bar where it changed.

    With ~1900 candles, a state-based agent would emit hundreds; transitions are far
    rarer, which is the point.
    """
    detections = run(RsiAgent(), candles)
    assert 0 < len(detections) < len(candles) // 5
    assert set(Counter(d.event_key for d in detections)) <= {
        "overbought_entry",
        "overbought_exit",
        "oversold_entry",
        "oversold_exit",
    }


def test_rsi_stores_the_value_that_triggered_it(candles: list[Candle]) -> None:
    for detection in run(RsiAgent(), candles):
        assert detection.indicators.rsi is not None
        assert "rsi_previous" in detection.indicators.extras
        # The thresholds in force at the time, so a later retune stays interpretable.
        assert detection.indicators.extras["overbought"] == 70.0


def test_rsi_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValueError):
        RsiAgent(overbought=30.0, oversold=70.0)
    with pytest.raises(ValueError):
        RsiAgent(overbought=120.0, oversold=30.0)


# ── Session trend agent (§18) ─────────────────────────────────────────────────


def test_session_summaries_are_context_only(candles: list[Candle]) -> None:
    """A session summary describes the past; a direction would attract spurious
    inferred links in Phase 7 (§50)."""
    detections = run(SessionTrendAgent(), candles)
    assert detections
    assert all(d.direction is None for d in detections)


def test_a_session_is_summarised_once_at_its_close(candles: list[Candle]) -> None:
    detections = run(SessionTrendAgent(), candles)
    keys = [(d.detected_at.market_date, d.event_key.split("|")[0]) for d in detections]
    assert len(keys) == len(set(keys)), "a session was summarised more than once"


def test_session_ohlc_is_internally_coherent(candles: list[Candle]) -> None:
    for detection in run(SessionTrendAgent(), candles):
        extras = detection.indicators.extras
        assert extras["low"] <= extras["open"] <= extras["high"]
        assert extras["low"] <= extras["close"] <= extras["high"]
        assert extras["range"] == pytest.approx(extras["high"] - extras["low"])
        assert extras["candle_count"] >= 1


def test_session_trend_label_matches_its_numbers(candles: list[Candle]) -> None:
    agent = SessionTrendAgent()
    for detection in run(agent, candles):
        trend = detection.event_key.split("|")[1]
        change = detection.indicators.extras["change_points"]
        if trend == "flat":
            assert abs(change) < agent.flat_points
        elif trend == "up":
            assert change >= agent.flat_points
        else:
            assert change <= -agent.flat_points


def test_a_session_summary_document_can_be_built(candles: list[Candle]) -> None:
    """The observer writes sessions/; the agent stays pure and only supplies numbers."""
    detections = run(SessionTrendAgent(), candles)
    summary = summary_from_detection(detections[0])
    assert summary.session_id == f"{summary.market_date}__{summary.session.value}"
    assert summary.candle_count >= 1
    assert summary.ended_at.utc > summary.started_at.utc


def test_summary_rejects_a_foreign_detection(candles: list[Candle]) -> None:
    cross = run(EmaCrossAgent(), candles)[0]
    with pytest.raises(ValueError, match="session_trend"):
        summary_from_detection(cross)


def test_a_timeframe_mismatch_is_refused(candles: list[Candle]) -> None:
    """Bars-per-session is derived from the configured timeframe, so a mismatch would
    silently mis-size the window rather than fail."""
    agent = SessionTrendAgent(timeframe=Timeframe.M15)
    window = one_candle(open_=2400, high=2401, low=2399, close=2400, bars=200)
    with pytest.raises(ValueError, match="configured for M15"):
        agent.on_closed_candle(window, ctx_for(window))


# ── Wick agent (§16) ──────────────────────────────────────────────────────────


def test_wick_detections_are_context_only(candles: list[Candle]) -> None:
    detections = run(WickAgent(), candles)
    assert detections
    assert all(d.is_context_only for d in detections)


def test_only_rejections_are_emitted(candles: list[Candle]) -> None:
    """§16: an ordinary candle produces nothing, or the interesting ones drown."""
    detections = run(WickAgent(), candles)
    assert set(d.event_key for d in detections) <= {"upper_rejection", "lower_rejection"}
    assert len(detections) < len(candles) // 3


def test_a_clean_upper_rejection_is_classified() -> None:
    # Long upper wick, small body, close near the low.
    window = one_candle(open_=2400.0, high=2405.0, low=2399.5, close=2399.8)
    detections = WickAgent().on_closed_candle(window, ctx_for(window))
    assert [d.event_key for d in detections] == ["upper_rejection"]


def test_a_clean_lower_rejection_is_classified() -> None:
    window = one_candle(open_=2400.0, high=2400.5, low=2395.0, close=2400.2)
    detections = WickAgent().on_closed_candle(window, ctx_for(window))
    assert [d.event_key for d in detections] == ["lower_rejection"]


def test_a_strong_directional_candle_is_not_a_rejection() -> None:
    """A big body with a small tail is momentum, not rejection."""
    window = one_candle(open_=2400.0, high=2410.2, low=2399.9, close=2410.0)
    assert WickAgent().on_closed_candle(window, ctx_for(window)) == []


def test_a_mid_range_close_is_indecision_not_rejection() -> None:
    window = one_candle(open_=2400.0, high=2405.0, low=2395.0, close=2400.0)
    assert WickAgent().on_closed_candle(window, ctx_for(window)) == []


def test_a_tick_sized_candle_is_ignored() -> None:
    """Dividing by a near-zero range would blow up, and there is no structure to read."""
    window = one_candle(open_=2400.0, high=2400.01, low=2399.99, close=2400.0)
    assert WickAgent().on_closed_candle(window, ctx_for(window)) == []


def test_at_most_one_classification_per_candle(candles: list[Candle]) -> None:
    per_candle: dict[object, int] = defaultdict(int)
    for detection in run(WickAgent(), candles):
        per_candle[detection.candle_open_time.utc] += 1
    assert per_candle and max(per_candle.values()) == 1


# ── Liquidity and breakout (§15, §17) ─────────────────────────────────────────


def test_both_level_agents_share_one_tracker() -> None:
    """§15/§17 are explicit: never two implementations of where a level is."""
    tracker = LevelTracker()
    liquidity = LiquidityAgent(level_tracker=tracker)
    breakout = BreakoutAgent(level_tracker=tracker)
    assert liquidity.level_tracker is breakout.level_tracker is tracker


def test_one_detection_per_swept_level(candles: list[Candle]) -> None:
    """§15. A candle sweeping two levels is two distinct facts, and Phase 3 evaluates
    outcomes per detection."""
    detections = run(LiquidityAgent(), candles)
    per_candle: dict[object, list[str]] = defaultdict(list)
    for detection in detections:
        per_candle[detection.candle_open_time.utc].append(detection.event_key)
    multi = [v for v in per_candle.values() if len(v) > 1]
    assert multi, "no multi-level sweep in the fixture; the rule would be untested"
    for keys in per_candle.values():
        assert len(keys) == len(set(keys)), "the same level was swept twice in one candle"


def test_event_keys_follow_the_direction_pipe_level_shape(candles: list[Candle]) -> None:
    """§15/§17 name this format explicitly, and the 0x1F id separator exists because
    of the pipe in it (decision 3)."""
    for agent_class in (LiquidityAgent, BreakoutAgent):
        for detection in run(agent_class(), candles):
            direction, _, level_type = detection.event_key.partition("|")
            assert direction in {"up", "down"}
            assert level_type


def test_a_sweep_implies_a_reversal_and_a_break_a_continuation(
    candles: list[Candle],
) -> None:
    """The two agents read the same event in opposite directions, on purpose."""
    for detection in run(LiquidityAgent(), candles):
        swept_up = detection.event_key.startswith("up|")
        assert detection.direction is (Direction.SELL if swept_up else Direction.BUY)
    for detection in run(BreakoutAgent(), candles):
        broke_up = detection.event_key.startswith("up|")
        assert detection.direction is (Direction.BUY if broke_up else Direction.SELL)


def test_a_candle_never_both_sweeps_and_breaks_the_same_level(
    candles: list[Candle],
) -> None:
    """They are mutually exclusive by construction: the close is either beyond the
    level or it is not. A bar reported as both would double-count in every review."""
    tracker = LevelTracker()
    engine = AnalysisEngine(
        [LiquidityAgent(level_tracker=tracker), BreakoutAgent(level_tracker=tracker)],
        account_scope=ACCOUNT_SCOPE,
        market_tz=MARKET_TZ,
    )
    detections = engine.feed(candles)

    def key(detection: Detection) -> tuple[object, str]:
        return detection.candle_open_time.utc, detection.event_key.split("|")[1]

    sweeps = {key(d) for d in detections if d.agent_name == "liquidity"}
    breaks = {key(d) for d in detections if d.agent_name == "breakout"}
    assert sweeps and breaks
    assert sweeps & breaks == set()


def test_a_sweep_closes_back_inside_the_level(candles: list[Candle]) -> None:
    for detection in run(LiquidityAgent(), candles):
        level = detection.indicators.extras["level_price"]
        if detection.event_key.startswith("up|"):
            assert detection.price < level, "an up-sweep must close BELOW the level"
        else:
            assert detection.price > level


def test_a_breakout_closes_beyond_the_level(candles: list[Candle]) -> None:
    agent = BreakoutAgent()
    for detection in run(agent, candles):
        level = detection.indicators.extras["level_price"]
        beyond = detection.indicators.extras["close_beyond_points"]
        assert beyond >= agent.min_close_beyond_points
        if detection.event_key.startswith("up|"):
            assert detection.price > level
        else:
            assert detection.price < level


def test_a_breakout_fires_once_not_every_bar_it_stays_beyond(
    candles: list[Candle],
) -> None:
    """Without the previous-close check, price holding above a level would report a
    breakout on every subsequent bar."""
    detections = run(BreakoutAgent(), candles)
    for detection in detections:
        extras = detection.indicators.extras
        level = extras["level_price"]
        if detection.event_key.startswith("up|"):
            assert extras["previous_close"] <= level
        else:
            assert extras["previous_close"] >= level


def test_a_candle_cannot_sweep_a_level_it_created(candles: list[Candle]) -> None:
    """Levels are taken as of the PREVIOUS bar. Otherwise a candle sweeps its own high,
    which is trivially true and meaningless."""
    for detection in run(LiquidityAgent(), candles):
        level = detection.indicators.extras["level_price"]
        assert detection.indicators.extras["penetration_points"] > 0
        assert level != pytest.approx(detection.price)
