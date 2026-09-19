"""The shared LevelTracker (§15, §17).

This module is the single source of price levels for both the liquidity and breakout
agents, so its edge cases are tested directly rather than only through them.
"""

from __future__ import annotations

import pandas as pd
import pytest

from aureon.agents.base_agent import WINDOW_COLUMNS
from aureon.engine.levels import (
    LEVEL_ASIA_HIGH,
    LEVEL_PREVIOUS_DAY_HIGH,
    LEVEL_PREVIOUS_DAY_LOW,
    LEVEL_PREVIOUS_SESSION_HIGH,
    LEVEL_SWING_HIGH,
    LEVEL_SWING_LOW,
    LevelTracker,
)
from aureon.models.market import Candle
from tests.conftest import MARKET_TZ


def frame(
    highs: list[float], lows: list[float], *, start: str = "2026-09-14 00:00"
) -> pd.DataFrame:
    """A window from explicit highs and lows; opens/closes sit inside the bar."""
    index = pd.date_range(start, periods=len(highs), freq="5min", tz="UTC")
    mids = [(h + low) / 2 for h, low in zip(highs, lows, strict=True)]
    return pd.DataFrame(
        {
            "open": mids,
            "high": highs,
            "low": lows,
            "close": mids,
            "tick_volume": [100] * len(highs),
            "real_volume": [0] * len(highs),
        },
        index=index,
        columns=list(WINDOW_COLUMNS),
    )


def candles_frame(candles: list[Candle]) -> pd.DataFrame:
    index = pd.DatetimeIndex([pd.Timestamp(c.open_time.utc) for c in candles])
    return pd.DataFrame(
        {
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "tick_volume": [c.tick_volume for c in candles],
            "real_volume": [c.real_volume for c in candles],
        },
        index=index,
        columns=list(WINDOW_COLUMNS),
    )


# ── Swing pivots ──────────────────────────────────────────────────────────────


def test_a_swing_high_is_a_confirmed_fractal_pivot() -> None:
    # Peak at index 3, with three lower bars each side.
    highs = [10.0, 11.0, 12.0, 20.0, 12.0, 11.0, 10.0]
    lows = [h - 5 for h in highs]
    levels = LevelTracker(swing_strength=3).levels_for(frame(highs, lows), MARKET_TZ)
    assert levels.price(LEVEL_SWING_HIGH) == pytest.approx(20.0)


def test_an_unconfirmed_pivot_is_not_reported() -> None:
    """A pivot needs `strength` bars AFTER it, or reporting it uses future information.

    The peak here is only two bars from the end, so it might still be exceeded -- the
    "level" could vanish, and anything built on it would be acting on a guess.
    """
    highs = [10.0, 11.0, 12.0, 13.0, 14.0, 20.0, 12.0, 11.0]
    lows = [h - 5 for h in highs]
    levels = LevelTracker(swing_strength=3).levels_for(frame(highs, lows), MARKET_TZ)
    assert levels.price(LEVEL_SWING_HIGH) != pytest.approx(20.0)


def test_a_flat_top_is_not_a_pivot() -> None:
    """Equal highs are an already-tested level, not a fresh pivot.

    Counting one would double-count the same level.
    """
    highs = [10.0, 11.0, 12.0, 20.0, 20.0, 12.0, 11.0, 10.0]
    lows = [h - 5 for h in highs]
    levels = LevelTracker(swing_strength=3).levels_for(frame(highs, lows), MARKET_TZ)
    assert levels.price(LEVEL_SWING_HIGH) != pytest.approx(20.0)


def test_the_most_recent_confirmed_pivot_wins() -> None:
    highs = [10.0, 11.0, 12.0, 20.0, 12.0, 11.0, 12.0, 25.0, 12.0, 11.0, 10.0]
    lows = [h - 5 for h in highs]
    levels = LevelTracker(swing_strength=3).levels_for(frame(highs, lows), MARKET_TZ)
    assert levels.price(LEVEL_SWING_HIGH) == pytest.approx(25.0)


def test_a_swing_low_mirrors_a_swing_high() -> None:
    lows = [20.0, 19.0, 18.0, 5.0, 18.0, 19.0, 20.0]
    highs = [low + 5 for low in lows]
    levels = LevelTracker(swing_strength=3).levels_for(frame(highs, lows), MARKET_TZ)
    assert levels.price(LEVEL_SWING_LOW) == pytest.approx(5.0)


def test_swing_strength_must_be_positive() -> None:
    with pytest.raises(ValueError):
        LevelTracker(swing_strength=0)


def test_a_tiny_window_yields_no_levels() -> None:
    assert len(LevelTracker().levels_for(frame([1.0], [0.0]), MARKET_TZ)) == 0


# ── Day and session ranges ────────────────────────────────────────────────────


def test_previous_day_levels_come_from_a_complete_day(candles: list[Candle]) -> None:
    window = candles_frame(candles[:700])
    levels = LevelTracker().levels_for(window, MARKET_TZ)
    assert levels.price(LEVEL_PREVIOUS_DAY_HIGH) is not None
    assert levels.price(LEVEL_PREVIOUS_DAY_LOW) is not None
    assert levels.price(LEVEL_PREVIOUS_DAY_HIGH) > levels.price(LEVEL_PREVIOUS_DAY_LOW)


def test_a_truncated_previous_day_is_not_reported(candles: list[Candle]) -> None:
    """A day clipped by the window's edge would report the edge, not the day's high."""
    # Start mid-day so the first date present is incomplete.
    window = candles_frame(candles[100:400])
    levels = LevelTracker().levels_for(window, MARKET_TZ)
    assert levels.price(LEVEL_PREVIOUS_DAY_HIGH) is None


def test_previous_day_levels_do_not_move_as_today_progresses(
    candles: list[Candle],
) -> None:
    """A completed day's range is fixed; if it drifted, every level built on it would."""
    base = LevelTracker().levels_for(candles_frame(candles[:700]), MARKET_TZ)
    later = LevelTracker().levels_for(candles_frame(candles[:740]), MARKET_TZ)
    if later.price(LEVEL_PREVIOUS_DAY_HIGH) is not None:
        assert base.price(LEVEL_PREVIOUS_DAY_HIGH) == later.price(LEVEL_PREVIOUS_DAY_HIGH)


def test_a_session_still_in_progress_is_not_a_level(candles: list[Candle]) -> None:
    """Asia's range is only a level once Asia has closed.

    A range that is still forming is not a level, and a breakout agent acting on one
    would be reacting to noise.
    """
    from aureon.config.sessions import session_for
    from aureon.models.base import MarketTime

    window = candles_frame(candles[:700])
    last = MarketTime.from_utc(window.index[-1].to_pydatetime(), MARKET_TZ)
    levels = LevelTracker().levels_for(window, MARKET_TZ)
    if session_for(last.market).value == "asia":
        assert levels.price(LEVEL_ASIA_HIGH) is None


def test_previous_session_levels_are_present_mid_week(candles: list[Candle]) -> None:
    levels = LevelTracker().levels_for(candles_frame(candles[:900]), MARKET_TZ)
    assert levels.price(LEVEL_PREVIOUS_SESSION_HIGH) is not None


# ── Determinism and contract ──────────────────────────────────────────────────


def test_levels_are_a_pure_function_of_the_window(candles: list[Candle]) -> None:
    window = candles_frame(candles[:900])
    tracker = LevelTracker()
    assert tracker.levels_for(window, MARKET_TZ).as_prices() == tracker.levels_for(
        window, MARKET_TZ
    ).as_prices()


def test_highs_and_lows_are_classified_consistently(candles: list[Candle]) -> None:
    levels = LevelTracker().levels_for(candles_frame(candles[:900]), MARKET_TZ)
    for name, level in levels.levels.items():
        assert level.is_high == name.endswith("_high"), name
    assert set(levels.highs) | set(levels.lows) == set(levels.levels)


def test_level_types_are_a_stable_contract(candles: list[Candle]) -> None:
    """Level type names appear in agents' event_keys, so renaming one re-keys history."""
    levels = LevelTracker().levels_for(candles_frame(candles[:1100]), MARKET_TZ)
    assert set(levels.levels) <= {
        "swing_high",
        "swing_low",
        "previous_day_high",
        "previous_day_low",
        "previous_session_high",
        "previous_session_low",
        "asia_high",
        "asia_low",
        "london_high",
        "london_low",
    }
