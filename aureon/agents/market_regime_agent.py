"""Agent 10: non-directional market-regime classification.

The regime agent answers only what kind of market is present. It never says BUY or
SELL. Classification uses closed-candle path efficiency, reversal rate and a short
true-range median relative to an earlier baseline.
"""

from __future__ import annotations

import math

import pandas as pd

from aureon.agents.base_agent import BaseAgent, validate_window
from aureon.models.detection import AgentEvidence, CandleContext, Detection, IndicatorSnapshot


class MarketRegimeAgent(BaseAgent):
    agent_name = "market_regime"
    agent_version = "1.0.0"

    def __init__(
        self,
        *,
        point: float,
        baseline_bars: int = 96,
        short_bars: int = 24,
        trend_bars: int = 48,
        compression_ratio: float = 0.75,
        expansion_ratio: float = 1.25,
        trend_efficiency: float = 0.50,
        range_efficiency: float = 0.30,
        messy_reversal_rate: float = 0.55,
    ) -> None:
        if point <= 0:
            raise ValueError("point must be positive")
        if min(baseline_bars, short_bars, trend_bars) < 3:
            raise ValueError("bar windows must be at least 3")
        if not 0 < compression_ratio < 1 < expansion_ratio:
            raise ValueError("compression_ratio < 1 < expansion_ratio is required")
        if not 0 < range_efficiency < trend_efficiency <= 1:
            raise ValueError("efficiency thresholds are not ordered")
        if not 0 <= messy_reversal_rate <= 1:
            raise ValueError("messy_reversal_rate must be in [0, 1]")
        self.point = point
        self.baseline_bars = baseline_bars
        self.short_bars = short_bars
        self.trend_bars = trend_bars
        self.compression_ratio = compression_ratio
        self.expansion_ratio = expansion_ratio
        self.trend_efficiency = trend_efficiency
        self.range_efficiency = range_efficiency
        self.messy_reversal_rate = messy_reversal_rate

    def params_snapshot(self) -> dict[str, object]:
        return {
            "point": self.point,
            "baseline_bars": self.baseline_bars,
            "short_bars": self.short_bars,
            "trend_bars": self.trend_bars,
            "compression_ratio": self.compression_ratio,
            "expansion_ratio": self.expansion_ratio,
            "trend_efficiency": self.trend_efficiency,
            "range_efficiency": self.range_efficiency,
            "messy_reversal_rate": self.messy_reversal_rate,
            "warmup_bars": self.min_window(),
        }

    def min_window(self) -> int:
        # +2 lets us classify the previous candle from window[:-1] as well.
        return self.baseline_bars + self.short_bars + 2

    def on_closed_candle(self, window: pd.DataFrame, ctx: CandleContext) -> list[Detection]:
        validate_window(window)
        if len(window) < self.min_window():
            return []

        current = classify_market_regime(
            window,
            point=self.point,
            baseline_bars=self.baseline_bars,
            short_bars=self.short_bars,
            trend_bars=self.trend_bars,
            compression_ratio=self.compression_ratio,
            expansion_ratio=self.expansion_ratio,
            trend_efficiency=self.trend_efficiency,
            range_efficiency=self.range_efficiency,
            messy_reversal_rate=self.messy_reversal_rate,
        )
        previous = classify_market_regime(
            window.iloc[:-1],
            point=self.point,
            baseline_bars=self.baseline_bars,
            short_bars=self.short_bars,
            trend_bars=self.trend_bars,
            compression_ratio=self.compression_ratio,
            expansion_ratio=self.expansion_ratio,
            trend_efficiency=self.trend_efficiency,
            range_efficiency=self.range_efficiency,
            messy_reversal_rate=self.messy_reversal_rate,
        )
        if current is None or previous is None or current["state"] == previous["state"]:
            return []

        numeric = {
            key: float(value)
            for key, value in current.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        state = str(current["state"])
        evidence = AgentEvidence(
            numeric=numeric,
            categorical={
                "regime": state,
                "volatility_state": str(current["volatility_state"]),
                "structure_state": str(current["structure_state"]),
            },
            flags={
                "is_trending": state in {"trending", "trend_expansion"},
                "is_ranging": state in {"ranging", "range_compression"},
                "is_compressed": bool(current["is_compressed"]),
                "is_expanding": bool(current["is_expanding"]),
                "is_messy": state in {"volatile_chop", "structurally_messy"},
            },
        )
        return [
            self.build_detection(
                ctx=ctx,
                event_key=f"regime|{state}",
                price=float(window["close"].iloc[-1]),
                direction=None,
                indicators=IndicatorSnapshot(extras=numeric),
                evidence=evidence,
            )
        ]


def classify_market_regime(
    window: pd.DataFrame,
    *,
    point: float,
    baseline_bars: int,
    short_bars: int,
    trend_bars: int,
    compression_ratio: float,
    expansion_ratio: float,
    trend_efficiency: float,
    range_efficiency: float,
    messy_reversal_rate: float,
) -> dict[str, object] | None:
    needed = baseline_bars + short_bars
    if len(window) < needed:
        return None

    tr = _true_range(window)
    short = tr.iloc[-short_bars:]
    reference = tr.iloc[-needed:-short_bars]
    short_median = float(short.median())
    baseline_median = float(reference.median())
    if not math.isfinite(short_median) or not math.isfinite(baseline_median) or baseline_median <= 0:
        return None
    volatility_ratio = short_median / baseline_median

    closes = window["close"].astype(float).iloc[-trend_bars:]
    if len(closes) < 3:
        return None
    deltas = closes.diff().dropna()
    path_distance = float(deltas.abs().sum())
    net_move = float(closes.iloc[-1] - closes.iloc[0])
    efficiency = 0.0 if path_distance <= 0 else abs(net_move) / path_distance

    signs = deltas.apply(lambda value: 1 if value > 0 else (-1 if value < 0 else 0))
    active = [int(value) for value in signs if int(value) != 0]
    reversals = sum(1 for left, right in zip(active, active[1:]) if left != right)
    reversal_rate = 0.0 if len(active) < 2 else reversals / (len(active) - 1)

    is_compressed = volatility_ratio <= compression_ratio
    is_expanding = volatility_ratio >= expansion_ratio
    if is_compressed:
        volatility_state = "compressed"
    elif is_expanding:
        volatility_state = "expanded"
    else:
        volatility_state = "normal"

    if efficiency >= trend_efficiency and volatility_ratio >= 1.15:
        state = "trend_expansion"
        structure_state = "trend"
    elif efficiency >= trend_efficiency:
        state = "trending"
        structure_state = "trend"
    elif efficiency <= 0.22 and reversal_rate >= messy_reversal_rate and is_expanding:
        state = "volatile_chop"
        structure_state = "chop"
    elif efficiency <= 0.25 and reversal_rate >= messy_reversal_rate:
        state = "structurally_messy"
        structure_state = "chop"
    elif is_compressed and efficiency < trend_efficiency:
        state = "range_compression"
        structure_state = "range"
    elif is_expanding and efficiency < range_efficiency:
        state = "expanding"
        structure_state = "transition"
    elif efficiency < range_efficiency:
        state = "ranging"
        structure_state = "range"
    else:
        state = "transition"
        structure_state = "transition"

    return {
        "state": state,
        "volatility_state": volatility_state,
        "structure_state": structure_state,
        "short_true_range": short_median,
        "baseline_true_range": baseline_median,
        "volatility_ratio": volatility_ratio,
        "path_efficiency": efficiency,
        "reversal_rate": reversal_rate,
        "net_move_points": net_move / point,
        "path_distance_points": path_distance / point,
        "is_compressed": is_compressed,
        "is_expanding": is_expanding,
    }


def _true_range(window: pd.DataFrame) -> pd.Series:
    high = window["high"].astype(float)
    low = window["low"].astype(float)
    close = window["close"].astype(float)
    previous = close.shift(1)
    return pd.concat(
        [high - low, (high - previous).abs(), (low - previous).abs()], axis=1
    ).max(axis=1)
