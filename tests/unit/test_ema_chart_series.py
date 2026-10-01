"""EMA chart preparation tests for TODO 107-113."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aureon.services.ema_chart_series import (
    prepare_ema_chart_series,
    session_boundaries_for_bars,
)
from aureon.visuals.chart_renderer import ChartBar

START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def _bars(count: int = 260) -> tuple[ChartBar, ...]:
    made = []
    for index in range(count):
        if index < 180:
            close = 4200.0 - index * 0.10
        else:
            close = 4182.0 + (index - 180) * 0.35
        made.append(
            ChartBar(
                at=START + timedelta(minutes=5 * index),
                open=close - 0.10,
                high=close + 0.30,
                low=close - 0.30,
                close=close,
                tick_volume=100 + index,
            )
        )
    return tuple(made)


def test_chart_series_contains_all_three_emas_after_warmup() -> None:
    series = prepare_ema_chart_series(_bars())

    assert len(series.ema20) == 260
    assert len(series.ema50) == 260
    assert len(series.ema200) == 260
    assert series.ema20[-1] is not None
    assert series.ema50[-1] is not None
    assert series.ema200[-1] is not None


def test_chart_series_finds_real_ema20_50_and_price_ema200_crosses() -> None:
    series = prepare_ema_chart_series(_bars())
    kinds = {cross.kind for cross in series.crosses}
    labels = {cross.label for cross in series.crosses}

    assert "ema20_50" in kinds
    assert "price_ema200" in kinds
    assert any(label in labels for label in {"EMA20↑EMA50", "EMA20↓EMA50"})
    assert any(label in labels for label in {"Price↑EMA200", "Price↓EMA200"})


def test_session_boundaries_mark_close_and_open_at_transition() -> None:
    # Europe/Athens is UTC+3 on Oct 1. 07:00 UTC is 10:00 market time:
    # Asia closes and London opens with the current session configuration.
    bars = tuple(
        ChartBar(
            at=datetime(2026, 10, 1, 6, 50, tzinfo=UTC)
            + timedelta(minutes=5 * index),
            open=4200.0,
            high=4201.0,
            low=4199.0,
            close=4200.0,
        )
        for index in range(5)
    )

    boundaries = session_boundaries_for_bars(
        bars,
        market_tz="Europe/Athens",
    )
    labels = [boundary.label for boundary in boundaries]

    assert "ASIA CLOSE" in labels
    assert "LONDON OPEN" in labels
