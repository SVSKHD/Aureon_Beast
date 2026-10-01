"""Closed-candle participation features used by EMA V3 research.

MT5 provides tick volume, not centralized exchange volume. These features therefore
normalize activity relative to the same stream/session instead of treating raw counts
as directly comparable across time.
"""
from __future__ import annotations

import math

import pandas as pd


def volume_features(frame: pd.DataFrame, *, lookback: int = 20) -> tuple[dict[str, float], dict[str, str], dict[str, bool]]:
    if "tick_volume" not in frame.columns or frame.empty:
        return {}, {"volume_state": "unavailable", "volume_price_alignment": "unknown"}, {}

    volume = pd.to_numeric(frame["tick_volume"], errors="coerce").astype("float64")
    current = float(volume.iloc[-1])
    history = volume.iloc[max(0, len(volume) - lookback - 1) : -1].dropna()
    if history.empty or not math.isfinite(current):
        return {}, {"volume_state": "unavailable", "volume_price_alignment": "unknown"}, {}

    median = float(history.median())
    mean = float(history.mean())
    ratio = current / median if median > 0 else 0.0
    percentile = float((history <= current).mean())
    previous = float(volume.iloc[-2]) if len(volume) > 1 and math.isfinite(float(volume.iloc[-2])) else current
    expanding = current > previous and ratio >= 1.0
    contracting = current < previous and ratio <= 1.0

    open_ = float(frame["open"].iloc[-1])
    close = float(frame["close"].iloc[-1])
    direction = "bullish" if close > open_ else "bearish" if close < open_ else "flat"
    if expanding and direction != "flat":
        alignment = f"expanding_{direction}"
    elif contracting:
        alignment = "contracting"
    else:
        alignment = "neutral"

    numeric = {
        "tick_volume": current,
        "tick_volume_recent_mean": mean,
        "tick_volume_recent_median": median,
        "tick_volume_ratio_to_median": ratio,
        "tick_volume_percentile": percentile,
        "tick_volume_prev": previous,
        "tick_volume_3bar_mean": float(volume.iloc[-3:].mean()),
        "tick_volume_5bar_mean": float(volume.iloc[-5:].mean()),
    }
    categorical = {
        "volume_state": "expanding" if expanding else "contracting" if contracting else "normal",
        "volume_price_alignment": alignment,
    }
    flags = {
        "volume_expanding": expanding,
        "volume_contracting": contracting,
        "volume_above_median": current >= median,
    }
    if "spread" in frame.columns:
        spread = frame["spread"].iloc[-1]
        if spread is not None and not pd.isna(spread):
            numeric["spread_points"] = float(spread)
    return numeric, categorical, flags
