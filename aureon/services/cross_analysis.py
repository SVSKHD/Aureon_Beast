"""Small deterministic helpers for Aureon V2 EMA cross notifications.

These helpers stay descriptive. They do not score a trade, recommend an entry,
or gate a detection. Live and replay agents call the same functions.
"""
from __future__ import annotations

import math

import pandas as pd

from aureon.engine.indicators import ema


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

    if clean_close and body_atr >= 0.60 and body_range_ratio >= 0.55 and close_beyond_atr >= 0.10:
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
