"""Market state classification (§10).

Includes the Sunday-boundary case Phase 2 names explicitly: 21:30 UTC and 23:30 UTC
must not classify the same.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.models.enums import MarketState
from aureon.models.market import SymbolInfo
from aureon.services.market_state_service import (
    MarketStateService,
    WeeklySchedule,
    classify_market_state,
)
from tests.conftest import FakeLiveProvider

TRADEABLE = SymbolInfo(
    symbol="XAUUSD",
    point=0.01,
    digits=2,
    volume_min=0.01,
    volume_max=50.0,
    volume_step=0.01,
    trade_mode="full",
)


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def fresh(now: datetime) -> datetime:
    return now - timedelta(seconds=5)


# ── The case Phase 2 calls out ─────────────────────────────────────────────────


def test_sunday_2130_is_not_open_but_sunday_2330_is() -> None:
    """2026-09-20 is a Sunday; the weekly open is 22:00 UTC.

    The distinction matters because a broker may accept a connection and publish a
    quote before it accepts trades. Reporting OPEN at 21:30 would let a human confirm
    a trade that the broker then rejects.
    """
    early = utc(2026, 9, 20, 21, 30)
    late = utc(2026, 9, 20, 23, 30)

    early_state = classify_market_state(
        now=early, symbol_info=TRADEABLE, last_tick_at=fresh(early)
    )
    late_state = classify_market_state(now=late, symbol_info=TRADEABLE, last_tick_at=fresh(late))

    assert early_state.state is MarketState.PREOPEN
    assert late_state.state is MarketState.OPEN
    assert early_state.state is not late_state.state
    assert early_state.tradeable is False
    assert late_state.tradeable is True


@pytest.mark.parametrize(
    "moment,expected",
    [
        (utc(2026, 9, 19, 12, 0), MarketState.CLOSED),  # Saturday
        (utc(2026, 9, 20, 20, 30), MarketState.CLOSED),  # Sunday, before pre-open
        (utc(2026, 9, 20, 21, 30), MarketState.PREOPEN),  # Sunday, pre-open
        (utc(2026, 9, 20, 22, 0), MarketState.OPEN),  # Sunday, at the open
        (utc(2026, 9, 15, 12, 0), MarketState.OPEN),  # Tuesday
        (utc(2026, 9, 18, 20, 59), MarketState.OPEN),  # Friday, before close
        (utc(2026, 9, 18, 21, 1), MarketState.CLOSED),  # Friday, after close
    ],
)
def test_the_weekly_schedule(moment: datetime, expected: MarketState) -> None:
    result = classify_market_state(
        now=moment, symbol_info=TRADEABLE, last_tick_at=fresh(moment)
    )
    assert result.state is expected, result.reason


# ── Quiet because closed vs quiet because broken ──────────────────────────────


def test_a_dead_feed_inside_trading_hours_is_stale_not_closed() -> None:
    """The distinction this service exists for.

    Both look identical from a tick timestamp alone, and only one deserves an alarm.
    """
    tuesday = utc(2026, 9, 15, 12, 0)
    result = classify_market_state(
        now=tuesday, symbol_info=TRADEABLE, last_tick_at=tuesday - timedelta(minutes=10)
    )
    assert result.state is MarketState.STALE
    assert result.tick_age_seconds == pytest.approx(600, abs=1)


def test_a_quiet_weekend_is_closed_not_stale() -> None:
    saturday = utc(2026, 9, 19, 12, 0)
    result = classify_market_state(
        now=saturday, symbol_info=TRADEABLE, last_tick_at=saturday - timedelta(hours=15)
    )
    assert result.state is MarketState.CLOSED


def test_no_tick_yet_is_stale() -> None:
    tuesday = utc(2026, 9, 15, 12, 0)
    assert (
        classify_market_state(now=tuesday, symbol_info=TRADEABLE, last_tick_at=None).state
        is MarketState.STALE
    )


# ── The broker's own verdict wins ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "trade_mode,expected",
    [
        ("full", MarketState.OPEN),
        ("close_only", MarketState.CLOSED),
        ("disabled", MarketState.CLOSED),
        ("longonly", MarketState.CLOSED),
        ("unknown", MarketState.UNKNOWN),
    ],
)
def test_trade_mode_is_checked_before_anything_else(
    trade_mode: str, expected: MarketState
) -> None:
    tuesday = utc(2026, 9, 15, 12, 0)
    result = classify_market_state(
        now=tuesday,
        symbol_info=TRADEABLE.model_copy(update={"trade_mode": trade_mode}),
        last_tick_at=fresh(tuesday),
    )
    assert result.state is expected


def test_missing_symbol_info_is_unknown_not_closed() -> None:
    """"We could not tell" must be distinguishable from "it is shut"."""
    assert (
        classify_market_state(now=utc(2026, 9, 15, 12), symbol_info=None).state
        is MarketState.UNKNOWN
    )


def test_every_result_explains_itself() -> None:
    """The reason is rendered in /status, so it must never be empty."""
    for moment in (utc(2026, 9, 19, 12), utc(2026, 9, 15, 12), utc(2026, 9, 20, 21, 30)):
        result = classify_market_state(
            now=moment, symbol_info=TRADEABLE, last_tick_at=fresh(moment)
        )
        assert result.reason


# ── The service wrapper ───────────────────────────────────────────────────────


def test_the_service_reports_unknown_when_the_provider_fails(fake_live: FakeLiveProvider) -> None:
    """A provider error must not stop the observer writing state."""

    class Broken(FakeLiveProvider):
        def symbol_info(self, symbol: str) -> SymbolInfo:
            raise ConnectionError("terminal gone")

    broken = Broken(fake_live._all)
    result = MarketStateService(broken).state_for("XAUUSD", now=utc(2026, 9, 15, 12))
    assert result.state is MarketState.UNKNOWN
    assert "symbol_info failed" in result.reason


def test_the_schedule_is_configurable() -> None:
    """A broker on a different weekly schedule must be expressible."""
    from datetime import time

    schedule = WeeklySchedule(open_time=time(23, 0), preopen_minutes=30)
    sunday_2230 = utc(2026, 9, 20, 22, 30)
    result = classify_market_state(
        now=sunday_2230,
        symbol_info=TRADEABLE,
        last_tick_at=fresh(sunday_2230),
        schedule=schedule,
    )
    assert result.state is MarketState.PREOPEN
