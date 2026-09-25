"""Weekly training report groups move outcomes by detector and timeframe."""

from __future__ import annotations

from datetime import UTC, datetime

from aureon.models.enums import DirectionContext, SetupFamily, Timeframe
from aureon.models.training import TrainingExample
from aureon.services.weekly_training_report import build_weekly_training_report

NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)


class _Memory:
    def __init__(self, rows):
        self.rows = rows

    def examples_between(self, symbol: str, start_market_date: str, end_market_date: str):
        return [
            row
            for row in self.rows
            if row.symbol == symbol
            and start_market_date <= row.market_date < end_market_date
        ]


def _example(
    setup_id: str,
    *,
    market_date: str,
    timeframe: Timeframe,
    six: bool,
    twenty: bool,
    forty: bool,
    max_move: float,
    ema_alignment: str = "aligned",
    rsi_alignment: str = "aligned",
) -> TrainingExample:
    return TrainingExample(
        example_id=setup_id,
        market_date=market_date,
        symbol="XAUUSD",
        timeframe=timeframe,
        setup_id=setup_id,
        family=SetupFamily.MOMENTUM_TRANSITION,
        direction_context=DirectionContext.BULLISH,
        setup_version="1",
        context={},
        agent_read={
            "ema_cross": {
                "stance": "bullish",
                "alignment": ema_alignment,
                "observation": "ema",
            },
            "rsi": {
                "stance": "bullish" if rsi_alignment == "aligned" else "bearish",
                "alignment": rsi_alignment,
                "observation": "rsi",
            },
            "wick": {
                "stance": "neutral",
                "alignment": "neutral",
                "observation": "none",
            },
        },
        six_dollar_status="reached" if six else "not_reached_eod",
        six_dollar_reached=six,
        twenty_dollar_reached=twenty,
        forty_dollar_reached=forty,
        max_favourable_move_price=max_move,
        extension_after_six_price=max_move - 6.0 if six else None,
        generated_at=NOW,
    )


def test_weekly_report_groups_agent_timeframe_move_ladder() -> None:
    memory = _Memory(
        [
            _example(
                "a",
                market_date="2026-09-21",
                timeframe=Timeframe.M5,
                six=True,
                twenty=True,
                forty=False,
                max_move=28.0,
            ),
            _example(
                "b",
                market_date="2026-09-22",
                timeframe=Timeframe.M5,
                six=True,
                twenty=False,
                forty=False,
                max_move=11.0,
                rsi_alignment="opposed",
            ),
            _example(
                "c",
                market_date="2026-09-23",
                timeframe=Timeframe.M15,
                six=True,
                twenty=True,
                forty=True,
                max_move=44.0,
            ),
        ]
    )

    report = build_weekly_training_report(
        memory,
        symbol="XAUUSD",
        iso_year=2026,
        iso_week=39,
    )

    ema_m5 = next(
        row
        for row in report.rows
        if row.agent_name == "ema_cross" and row.timeframe is Timeframe.M5
    )
    assert ema_m5.decisions == 2
    assert ema_m5.aligned_decisions == 2
    assert ema_m5.reached_six == 2
    assert ema_m5.reached_twenty == 1
    assert ema_m5.reached_forty == 0
    assert ema_m5.median_max_move_price == 19.5
    assert ema_m5.maximum_move_price == 28.0
    assert ema_m5.median_extension_after_six_price == 13.5

    rsi_m5 = next(
        row
        for row in report.rows
        if row.agent_name == "rsi" and row.timeframe is Timeframe.M5
    )
    assert rsi_m5.decisions == 2
    assert rsi_m5.aligned_decisions == 1
    assert rsi_m5.opposed_decisions == 1
    assert rsi_m5.reached_six == 1
    assert rsi_m5.reached_twenty == 1

    ema_m15 = next(
        row
        for row in report.rows
        if row.agent_name == "ema_cross" and row.timeframe is Timeframe.M15
    )
    assert ema_m15.reached_forty == 1
    assert ema_m15.maximum_move_price == 44.0
