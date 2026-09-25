"""Agent 17: large-move opportunity detection and hypothetical entry windows."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from aureon.models.agent_decision import ExpansionFamily, ExpansionPhase
from aureon.models.enums import Direction, Timeframe, TrendBias
from aureon.models.mtf import MtfContext, TimeframeRead
from aureon.services.expansion_opportunity_agent import (
    ExpansionInputs,
    ExpansionOpportunityAgent,
)
from aureon.services.higher_timeframe_agent import HigherTimeframeAgent


def _frame(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range(
        "2026-09-25 06:00:00+00:00", periods=len(closes), freq="5min", tz="UTC"
    )
    return pd.DataFrame(
        {
            "open": [c - 0.2 for c in closes],
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "tick_volume": [100.0] * len(closes),
            "real_volume": [0.0] * len(closes),
        },
        index=index,
    )


def _htf(bias: TrendBias):
    now = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
    reads = tuple(
        TimeframeRead(
            timeframe=tf,
            at=now,
            ema_fast=101.0 if bias is TrendBias.BULLISH else 99.0,
            ema_slow=99.0 if bias is TrendBias.BULLISH else 101.0,
            close=100.0,
            bias=bias,
        )
        for tf in (Timeframe.M15, Timeframe.H1, Timeframe.H4)
    )
    return HigherTimeframeAgent().assess(
        MtfContext(reads=reads, ema_fast_period=20, ema_slow_period=50)
    )


def test_bullish_reversal_expansion_creates_entry_window() -> None:
    closes = [100.0] * 20 + [96.0, 94.0, 95.0, 97.0, 98.5, 99.5, 100.5, 101.0]
    frame = _frame(closes)
    result = ExpansionOpportunityAgent(primary_move=10.0).assess(
        ExpansionInputs(
            frame=frame,
            price=101.0,
            ema_fast=100.3,
            ema_slow=99.7,
            previous_ema_fast=99.9,
            previous_ema_slow=99.8,
            rsi=58.0,
            atr=2.0,
            regime={"regime": "trend_expansion"},
            participation={"state": "expanding"},
            htf=_htf(TrendBias.BULLISH),
            candle_signals=(
                ("liquidity", Direction.BUY),
                ("wick", Direction.BUY),
            ),
            as_of=datetime(2026, 9, 25, 9, 0, tzinfo=UTC),
        )
    )

    assert result.direction is Direction.BUY
    assert result.family is ExpansionFamily.REVERSAL
    assert result.phase in {ExpansionPhase.ENTRY_WINDOW, ExpansionPhase.ARMED}
    assert result.earliest_entry is not None
    assert result.confirmation_entry is not None
    assert result.preferred_zone_low is not None
    assert result.signature is not None


def test_bearish_continuation_expansion_finds_pullback_zone() -> None:
    closes = [
        110.0, 109.5, 109.0, 108.5, 108.0, 107.0, 106.0, 105.0,
        104.0, 103.5, 103.0, 102.5, 102.0, 101.5, 101.0, 100.5,
        100.0, 99.5, 99.0, 98.5, 98.0, 97.8, 98.2, 98.0,
    ]
    frame = _frame(closes)
    # Force the latest candle to overlap the fast-EMA pullback zone.
    frame.iloc[-1, frame.columns.get_loc("high")] = 99.4
    result = ExpansionOpportunityAgent(primary_move=10.0).assess(
        ExpansionInputs(
            frame=frame,
            price=98.0,
            ema_fast=99.0,
            ema_slow=101.0,
            previous_ema_fast=99.4,
            previous_ema_slow=101.2,
            rsi=42.0,
            atr=1.6,
            regime={"regime": "trending"},
            participation={"state": "expanding"},
            htf=_htf(TrendBias.BEARISH),
            candle_signals=(("ema_cross", Direction.SELL),),
        )
    )

    assert result.direction is Direction.SELL
    assert result.family is ExpansionFamily.CONTINUATION
    assert result.pullback_entry is not None
    assert result.preferred_zone_low <= result.pullback_entry.price <= result.preferred_zone_high


def test_extended_move_without_entry_is_marked_missed_not_chased() -> None:
    closes = [100.0 + i * 0.45 for i in range(30)]
    frame = _frame(closes)
    result = ExpansionOpportunityAgent(primary_move=10.0).assess(
        ExpansionInputs(
            frame=frame,
            price=closes[-1],
            ema_fast=closes[-1] - 0.8,
            ema_slow=closes[-1] - 2.0,
            previous_ema_fast=closes[-1] - 1.2,
            previous_ema_slow=closes[-1] - 2.1,
            rsi=51.0,
            atr=1.0,
            regime={"regime": "unknown"},
            participation={"state": "normal"},
            htf=None,
            candle_signals=(),
        )
    )

    assert result.move_from_anchor >= 10.0
    assert result.phase in {ExpansionPhase.EXPANDING, ExpansionPhase.MISSED}
    # No fabricated fresh structure entry after the move has already run.
    assert result.earliest_entry is None
