"""Small deterministic helpers for Aureon V2 EMA cross notifications.

These helpers stay descriptive. They do not score a trade, recommend an entry,
or gate a detection. Live and replay agents call the same functions.
"""
from __future__ import annotations

import math

import pandas as pd

from aureon.engine.indicators import ema


V2_EMA_EVENT_KEYS: tuple[str, ...] = ("bullish", "bearish")
V2_EXCLUDED_LEGACY_ALERTS: tuple[str, ...] = (
    "setup_b",
    "hold_confirmation",
    "approach",
    "retest",
    "missed_move",
    "entry_calculation",
    "stop_loss_calculation",
    "target_calculation",
    "automatic_trade_recommendation",
)


def _true_range(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["close"].shift(1)
    return pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def recent_atr(frame: pd.DataFrame, period: int = 14) -> float:
    """Return a closed-candle ATR-like mean for descriptive normalization."""
    if len(frame) < 2:
        return 0.0
    values = _true_range(frame).tail(period)
    value = float(values.mean())
    return value if math.isfinite(value) and value > 0 else 0.0


def simple_trend_direction(
    frame: pd.DataFrame,
    *,
    ema_period: int = 20,
    lookback: int = 5,
) -> str:
    """Classify immediate chart direction as UP, DOWN or SIDEWAYS."""
    required = max(ema_period * 3, lookback + 1)
    if len(frame) < required:
        return "SIDEWAYS"

    close = frame["close"].astype(float)
    line = ema(close, ema_period)
    ema_now = float(line.iloc[-1])
    ema_then = float(line.iloc[-1 - lookback])
    close_move = float(close.iloc[-1] - close.iloc[-1 - lookback])
    atr = recent_atr(frame)
    threshold = atr * 0.10 if atr > 0 else 0.0

    if ema_now > ema_then and close_move > threshold:
        return "UP"
    if ema_now < ema_then and close_move < -threshold:
        return "DOWN"
    return "SIDEWAYS"


def cross_candle_quality(
    frame: pd.DataFrame,
    *,
    reference_value: float,
    direction: str,
) -> dict[str, float | str | bool]:
    """Describe how decisively the latest candle crossed a reference line."""
    row = frame.iloc[-1]
    open_price = float(row["open"])
    close_price = float(row["close"])
    high = float(row["high"])
    low = float(row["low"])
    body = abs(close_price - open_price)
    candle_range = max(0.0, high - low)
    atr = recent_atr(frame)
    body_atr = body / atr if atr > 0 else 0.0
    body_range_ratio = body / candle_range if candle_range > 0 else 0.0

    if direction == "bullish":
        close_beyond = close_price - reference_value
        directional_close = close_price > open_price
    elif direction == "bearish":
        close_beyond = reference_value - close_price
        directional_close = close_price < open_price
    else:
        raise ValueError("direction must be bullish or bearish")

    close_beyond_atr = close_beyond / atr if atr > 0 else 0.0
    clean_close = close_beyond > 0 and directional_close

    if (
        clean_close
        and body_atr >= 0.60
        and body_range_ratio >= 0.55
        and close_beyond_atr >= 0.10
    ):
        quality = "STRONG"
    elif clean_close and body_atr >= 0.25 and close_beyond_atr >= 0.02:
        quality = "NORMAL"
    else:
        quality = "WEAK"

    return {
        "quality": quality,
        "body": body,
        "atr": atr,
        "body_atr": body_atr,
        "body_range_ratio": body_range_ratio,
        "close_beyond": close_beyond,
        "close_beyond_atr": close_beyond_atr,
        "clean_close": clean_close,
    }



def classify_pre_cross_pattern(
    frame: pd.DataFrame,
    *,
    lookback: int = 12,
) -> str:
    """Classify the price behaviour immediately before the crossing candle.

    The crossing candle itself is excluded. This is intentionally a small descriptive
    vocabulary for Discord/research rather than a trading score.
    """
    if len(frame) < lookback + 2:
        return "CHOPPY"

    history = frame.iloc[-(lookback + 1):-1]
    close = history["close"].astype(float)
    moves = close.diff().dropna()
    atr = recent_atr(history)
    if moves.empty or atr <= 0:
        return "CHOPPY"

    net_move = float(close.iloc[-1] - close.iloc[0])
    path = float(moves.abs().sum())
    efficiency = abs(net_move) / path if path > 0 else 0.0
    direction_changes = int(((moves * moves.shift(1)) < 0).sum())
    reversal_rate = direction_changes / max(1, len(moves) - 1)
    range_size = float(history["high"].max() - history["low"].min())
    range_atr = range_size / atr if atr > 0 else 0.0

    first_half = close.iloc[: max(2, len(close) // 2)]
    second_half = close.iloc[-max(2, len(close) // 2):]
    first_move = float(first_half.iloc[-1] - first_half.iloc[0])
    second_move = float(second_half.iloc[-1] - second_half.iloc[0])

    # Tight range with low directional efficiency: consolidation.
    if efficiency < 0.30 and range_atr <= 3.0 and reversal_rate < 0.55:
        return "CONSOLIDATION"

    # Lots of alternating bars irrespective of the net move: chop.
    if reversal_rate >= 0.55:
        return "CHOPPY"

    # Direction already established before the cross.
    if efficiency >= 0.65 and abs(net_move) >= atr:
        return "TRENDING"

    # Opposite first/second-half direction with a meaningful late displacement.
    if first_move * second_move < 0 and abs(second_move) >= atr * 0.75:
        return "SHARP_REVERSAL"

    # A quieter but increasingly directional turn into the cross.
    if abs(second_move) > abs(first_move) and abs(second_move) >= atr * 0.35:
        return "GRADUAL_MOMENTUM_SHIFT"

    return "CHOPPY"
