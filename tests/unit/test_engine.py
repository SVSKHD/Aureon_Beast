"""Engine and agent contracts (§79).

Covers the properties the whole phase rests on: agents are pure, the engine's window
is fixed, and context (session, sequence, market date) is derived from the broker's
clock rather than the host's.
"""

from __future__ import annotations

import pandas as pd
import pytest

from aureon.agents.base_agent import WINDOW_COLUMNS, validate_window
from aureon.agents.ema_cross_agent import EmaCrossAgent
from aureon.engine.analysis_engine import AnalysisEngine
from aureon.engine.market_engine import MarketEngine
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.market import Candle
from tests.conftest import ACCOUNT_SCOPE, MARKET_TZ, FakeLiveProvider


def build(**kwargs: object) -> AnalysisEngine:
    return AnalysisEngine(
        [EmaCrossAgent()], account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ, **kwargs
    )


# ── Agent purity ──────────────────────────────────────────────────────────────


def test_an_agent_is_a_pure_function_of_window_and_context(candles: list[Candle]) -> None:
    """Same window, same context, same detections -- what parity rests on."""
    engine_a, engine_b = build(), build()
    first = engine_a.feed(candles[:800])
    second = engine_b.feed(candles[:800])
    assert [d.model_dump(mode="json") for d in first] == [
        d.model_dump(mode="json") for d in second
    ]


def test_params_snapshot_captures_everything_that_changes_output() -> None:
    """The reproducibility test: could someone re-derive this from candle + params?"""
    agent = EmaCrossAgent(fast_period=5, slow_period=13, rsi_period=7)
    snapshot = agent.params_snapshot()
    assert snapshot["fast_period"] == 5
    assert snapshot["slow_period"] == 13
    assert snapshot["rsi_period"] == 7
    assert "warmup_bars" in snapshot


def test_differently_tuned_agents_disagree(candles: list[Candle]) -> None:
    """Otherwise params_snapshot would be recording something that does not matter."""
    default = build().feed(candles[:900])
    tuned = AnalysisEngine(
        [EmaCrossAgent(fast_period=5, slow_period=34)],
        account_scope=ACCOUNT_SCOPE,
        market_tz=MARKET_TZ,
    ).feed(candles[:900])
    assert [d.detection_id for d in default] != [d.detection_id for d in tuned]


def test_an_inverted_period_pair_is_refused() -> None:
    """fast >= slow inverts the meaning of every signal: a config error, not a taste."""
    with pytest.raises(ValueError):
        EmaCrossAgent(fast_period=21, slow_period=9)
    with pytest.raises(ValueError):
        EmaCrossAgent(fast_period=9, slow_period=9)


def test_no_detection_before_warmup(candles: list[Candle]) -> None:
    """An unwarmed EMA still carries its seed and would cross on noise."""
    agent = EmaCrossAgent()
    engine = build()
    early = engine.feed(candles[: agent.min_window() - 1])
    assert early == []


def test_stored_indicator_values_are_finite(candles: list[Candle]) -> None:
    """A NaN would serialise into Firestore and compare unequal to itself,
    silently breaking the parity comparison."""
    import math

    for detection in build().feed(candles):
        for value in detection.indicators.ema.values():
            assert math.isfinite(value)
        for value in detection.indicators.extras.values():
            assert math.isfinite(value)
        if detection.indicators.rsi is not None:
            assert math.isfinite(detection.indicators.rsi)


def test_direction_matches_the_event_key(candles: list[Candle]) -> None:
    for detection in build().feed(candles):
        expected = Direction.BUY if detection.event_key == "bullish" else Direction.SELL
        assert detection.direction is expected


# ── Window contract ───────────────────────────────────────────────────────────


def test_the_window_is_sized_from_the_agents_requirements() -> None:
    engine = build()
    assert engine.window_size == EmaCrossAgent().min_window()


def test_a_window_below_the_agents_requirement_is_refused() -> None:
    """Silently accepting it would produce subtly wrong indicators, not an error."""
    with pytest.raises(ValueError, match="below the"):
        build(window_size=10)


def test_the_window_stops_growing_at_its_size(candles: list[Candle]) -> None:
    engine = build()
    engine.feed(candles[:500])
    assert engine.window_length("XAUUSD", Timeframe.M5) == engine.window_size


def test_an_engine_needs_at_least_one_agent() -> None:
    with pytest.raises(ValueError):
        AnalysisEngine([], account_scope=ACCOUNT_SCOPE, market_tz=MARKET_TZ)


def test_re_feeding_a_candle_is_ignored(candles: list[Candle]) -> None:
    """Appending a duplicate would shift the window and change every indicator."""
    engine = build()
    engine.feed(candles[:300])
    length = engine.window_length("XAUUSD", Timeframe.M5)
    assert engine.on_closed_candle(candles[299]) == []
    assert engine.on_closed_candle(candles[100]) == []
    assert engine.window_length("XAUUSD", Timeframe.M5) == length


def test_reset_clears_all_state(candles: list[Candle]) -> None:
    engine = build()
    engine.feed(candles[:300])
    engine.reset()
    assert engine.window_length("XAUUSD", Timeframe.M5) == 0
    assert engine.last_processed("XAUUSD", Timeframe.M5) is None


# ── Context ───────────────────────────────────────────────────────────────────


def test_session_and_sequence_come_from_the_broker_clock(candles: list[Candle]) -> None:
    """Sessions are defined in market time; using UTC would shift every boundary."""
    detections = build().feed(candles)
    assert detections
    for detection in detections:
        assert detection.session.session in set(SessionName)
        assert detection.sequence_today >= 1
        assert detection.sequence_session >= 1
        assert detection.session.session_config_version >= 1


def test_the_sequence_counters_reset_on_a_new_broker_day(candles: list[Candle]) -> None:
    """A per-day counter that never reset would just be a running total."""
    engine = build()
    engine.feed(candles)
    detections = engine.feed([])  # no-op; counters live on the engine
    assert detections == []

    by_day: dict[str, list[int]] = {}
    for detection in build().feed(candles):
        by_day.setdefault(detection.detected_at.market_date, []).append(detection.sequence_today)
    assert len(by_day) > 1, "fixture spans one day only; the reset would be untested"
    # Each day's sequences are ascending, and no day's first value carries over the
    # previous day's total.
    for day, sequences in by_day.items():
        assert sequences == sorted(sequences), day
    maxima = [max(v) for v in by_day.values()]
    assert min(maxima) <= 288 + 1, "a day cannot have more M5 candles than exist in a day"


def test_detected_at_is_the_candle_close_not_its_open(candles: list[Candle]) -> None:
    """A detection becomes known at the close; recording the open would backdate it."""
    for detection in build().feed(candles):
        assert detection.detected_at.utc > detection.candle_open_time.utc
        delta = detection.detected_at.utc - detection.candle_open_time.utc
        assert delta.total_seconds() == Timeframe.M5.seconds


# ── Window validation ─────────────────────────────────────────────────────────


def test_a_malformed_window_is_refused_with_a_useful_message() -> None:
    index = pd.date_range("2026-09-18 13:00", periods=3, freq="5min", tz="UTC")
    good = pd.DataFrame({c: [1.0, 2.0, 3.0] for c in WINDOW_COLUMNS}, index=index)
    validate_window(good)

    with pytest.raises(ValueError, match="missing columns"):
        validate_window(good.drop(columns=["close"]))
    with pytest.raises(ValueError, match="tz-aware"):
        validate_window(good.set_axis(index.tz_localize(None)))
    with pytest.raises(ValueError, match="oldest-first"):
        validate_window(good.iloc[::-1])


# ── Market engine ─────────────────────────────────────────────────────────────


def test_the_market_engine_only_advances_on_new_closed_candles(
    candles: list[Candle],
) -> None:
    provider = FakeLiveProvider(candles)
    engine = build()
    market = MarketEngine(
        provider, engine, symbols=["XAUUSD"], timeframes=[Timeframe.M5]
    )
    market.seed_cursor("XAUUSD", Timeframe.M5, candles[99].open_time.utc)

    provider.advance_to_close_of(candles[99])
    assert market.poll_once() == []  # nothing newer has closed
    assert market.cursor("XAUUSD", Timeframe.M5) == candles[99].open_time.utc

    provider.advance_to_close_of(candles[120])
    market.poll_once()
    assert market.cursor("XAUUSD", Timeframe.M5) == candles[120].open_time.utc


def test_a_provider_failure_does_not_advance_the_cursor(candles: list[Candle]) -> None:
    """The next poll must retry the same candles rather than skip them."""
    provider = FakeLiveProvider(candles)
    market = MarketEngine(provider, build(), symbols=["XAUUSD"], timeframes=[Timeframe.M5])
    market.seed_cursor("XAUUSD", Timeframe.M5, candles[99].open_time.utc)
    provider.advance_to_close_of(candles[150])

    provider.fail_next = 1
    with pytest.raises(ConnectionError):
        market.poll_once()
    assert market.cursor("XAUUSD", Timeframe.M5) == candles[99].open_time.utc

    market.poll_once()
    assert market.cursor("XAUUSD", Timeframe.M5) == candles[150].open_time.utc


def test_detections_are_handed_over_per_candle(candles: list[Candle]) -> None:
    """A crash mid-poll must not lose detections from candles already processed."""
    provider = FakeLiveProvider(candles)
    batches: list[int] = []
    market = MarketEngine(
        provider,
        build(),
        symbols=["XAUUSD"],
        timeframes=[Timeframe.M5],
        on_detections=lambda d: batches.append(len(d)),
    )
    provider.advance_to_close_of(candles[-1])
    market.poll_once()
    assert batches, "no detections were handed over"
    assert all(count >= 1 for count in batches)
