"""Prepare EMA chart overlays from stored closed bars.

This is visualization preparation, not trading logic. It uses Aureon's shared EMA
implementation so the chart does not invent a second formula, and the renderer itself
continues to compute no indicators.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from aureon.engine.indicators import ema


@dataclass(frozen=True)
class EMACrossPoint:
    at: datetime
    price: float
    kind: str
    bullish: bool

    @property
    def label(self) -> str:
        if self.kind == "ema20_50":
            return "EMA20↑EMA50" if self.bullish else "EMA20↓EMA50"
        return "Price↑EMA200" if self.bullish else "Price↓EMA200"


@dataclass(frozen=True)
class EMAChartSeries:
    ema20: tuple[float | None, ...]
    ema50: tuple[float | None, ...]
    ema200: tuple[float | None, ...]
    crosses: tuple[EMACrossPoint, ...]


def _values(series: pd.Series) -> tuple[float | None, ...]:
    return tuple(None if pd.isna(value) else float(value) for value in series)


def prepare_ema_chart_series(
    bars: list[Any] | tuple[Any, ...],
    *,
    fast_period: int = 20,
    slow_period: int = 50,
    ema200_period: int = 200,
) -> EMAChartSeries:
    if not bars:
        return EMAChartSeries((), (), (), ())

    closes = pd.Series([float(bar.close) for bar in bars], dtype="float64")
    fast = ema(closes, fast_period)
    slow = ema(closes, slow_period)
    broad = ema(closes, ema200_period)

    crosses: list[EMACrossPoint] = []
    for index in range(1, len(bars)):
        f0, f1 = fast.iloc[index - 1], fast.iloc[index]
        s0, s1 = slow.iloc[index - 1], slow.iloc[index]
        e0, e1 = broad.iloc[index - 1], broad.iloc[index]
        c0, c1 = closes.iloc[index - 1], closes.iloc[index]

        if not any(pd.isna(value) for value in (f0, f1, s0, s1)):
            bullish = f0 <= s0 and f1 > s1
            bearish = f0 >= s0 and f1 < s1
            if bullish or bearish:
                crosses.append(
                    EMACrossPoint(
                        at=bars[index].at,
                        price=float((f1 + s1) / 2.0),
                        kind="ema20_50",
                        bullish=bullish,
                    )
                )

        if not any(pd.isna(value) for value in (e0, e1)):
            bullish = c0 <= e0 and c1 > e1
            bearish = c0 >= e0 and c1 < e1
            if bullish or bearish:
                crosses.append(
                    EMACrossPoint(
                        at=bars[index].at,
                        price=float(c1),
                        kind="price_ema200",
                        bullish=bullish,
                    )
                )

    return EMAChartSeries(
        ema20=_values(fast),
        ema50=_values(slow),
        ema200=_values(broad),
        crosses=tuple(crosses),
    )
