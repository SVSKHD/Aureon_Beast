"""The six-agent confidence meter is transparent confluence, never a success probability."""

from __future__ import annotations

from types import SimpleNamespace

from aureon.models.enums import DirectionContext, TrendBias
from aureon.services.agent_confluence import build_agent_confluence


def inputs(
    *,
    ema_fast: float | None = None,
    ema_slow: float | None = None,
    rsi: float | None = None,
    trend: TrendBias = TrendBias.SIDEWAYS,
    detections: tuple[object, ...] = (),
):
    return SimpleNamespace(
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        rsi=rsi,
        trend=trend,
        detections=detections,
    )


def detection(agent: str, event_key: str, *, direction=None):
    return SimpleNamespace(agent_name=agent, event_key=event_key, direction=direction)


def test_all_six_can_align_bullish_without_becoming_a_probability() -> None:
    snapshot = build_agent_confluence(
        inputs(
            ema_fast=2420.0,
            ema_slow=2418.0,
            rsi=61.0,
            trend=TrendBias.BULLISH,
            detections=(
                detection("wick", "lower_rejection"),
                detection("liquidity", "down|previous_day_low"),
                detection("breakout", "up|session_high"),
            ),
        ),
        DirectionContext.BULLISH,
    )

    assert snapshot.confidence_pct == 100
    assert snapshot.aligned_count == 6
    assert snapshot.opposed_count == 0
    assert snapshot.neutral_count == 0
    assert [vote.agent_name for vote in snapshot.votes] == [
        "ema_cross",
        "rsi",
        "session_trend",
        "wick",
        "liquidity",
        "breakout",
    ]


def test_the_meter_counts_aligned_opposed_and_neutral_separately() -> None:
    snapshot = build_agent_confluence(
        inputs(
            ema_fast=2410.0,
            ema_slow=2412.0,
            rsi=50.0,
            trend=TrendBias.BULLISH,
            detections=(
                detection("wick", "upper_rejection"),
                detection("breakout", "up|session_high"),
            ),
        ),
        DirectionContext.BULLISH,
    )

    assert snapshot.confidence_pct == 33
    assert snapshot.aligned_count == 2
    assert snapshot.opposed_count == 2
    assert snapshot.neutral_count == 2


def test_event_driven_evidence_survives_until_that_agent_speaks_again() -> None:
    first = build_agent_confluence(
        inputs(detections=(detection("liquidity", "up|previous_day_high"),)),
        DirectionContext.BEARISH,
    )
    liquidity_before = next(v for v in first.votes if v.agent_name == "liquidity")
    assert liquidity_before.alignment == "aligned"

    later = build_agent_confluence(
        inputs(ema_fast=2410.0, ema_slow=2412.0, rsi=42.0, trend=TrendBias.BEARISH),
        DirectionContext.BEARISH,
        previous=first,
    )
    liquidity_after = next(v for v in later.votes if v.agent_name == "liquidity")

    assert liquidity_after.stance is DirectionContext.BEARISH
    assert liquidity_after.observation == liquidity_before.observation
    assert liquidity_after.alignment == "aligned"


def test_a_new_event_replaces_the_previous_event_driven_read() -> None:
    previous = build_agent_confluence(
        inputs(detections=(detection("wick", "lower_rejection"),)),
        DirectionContext.BULLISH,
    )
    current = build_agent_confluence(
        inputs(detections=(detection("wick", "upper_rejection"),)),
        DirectionContext.BULLISH,
        previous=previous,
    )
    wick = next(v for v in current.votes if v.agent_name == "wick")

    assert wick.stance is DirectionContext.BEARISH
    assert wick.alignment == "opposed"
    assert wick.observation == "upper_rejection"
