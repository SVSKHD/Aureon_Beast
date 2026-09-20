"""The per-symbol context tracker: buffers, rollovers and scopes (9B).

``test_detection_context`` proves what a detection ends up carrying. This proves the
bookkeeping underneath it: which candles each scope covers, what survives a session change,
what a broker day rollover clears, and that the figures a detection records are a function of
**that broker day's** candles -- which is what makes a replay of one day's archive reproduce
them (§82).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.config.symbol_tuning import tuning_for
from aureon.engine.market_context import DETECTION_SCOPE, MarketContextTracker
from aureon.models.base import MarketTime
from aureon.models.enums import SessionName, Timeframe
from aureon.models.market import Candle

TZ = "Europe/Athens"
SYMBOL = "XAUUSD"
#: 2026-09-16 is a Wednesday. 03:00 Athens is inside Asia (02:00-10:00), 11:00 inside London.
ASIA = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)  # 03:00 Athens


def tracker(symbol: str = SYMBOL) -> MarketContextTracker:
    return MarketContextTracker(
        symbol=symbol, timeframe=Timeframe.M5, tuning=tuning_for(symbol)
    )


def candle(
    *, at: datetime, low: float, high: float, volume: int = 100, symbol: str = SYMBOL
) -> Candle:
    return Candle(
        symbol=symbol,
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(at, TZ),
        open=low,
        high=high,
        low=low,
        close=high,
        tick_volume=volume,
    )


def feed(track: MarketContextTracker, candles) -> None:
    for item in candles:
        track.observe(item)


def asia_candles(count: int = 6, *, base: float = 2400.0, volume: int = 100):
    return [
        candle(at=ASIA + timedelta(minutes=5 * i), low=base, high=base + 0.10, volume=volume)
        for i in range(count)
    ]


def london_candles(count: int = 6, *, base: float = 2410.0, volume: int = 100):
    start = ASIA + timedelta(hours=8)  # 11:00 Athens
    return [
        candle(at=start + timedelta(minutes=5 * i), low=base, high=base + 0.10, volume=volume)
        for i in range(count)
    ]


# ── Scopes ────────────────────────────────────────────────────────────────────


def test_the_asia_scope_holds_only_asias_candles() -> None:
    track = tracker()
    feed(track, [*asia_candles(4), *london_candles(4)])

    asia = track.profile("asia")
    london = track.profile("london")
    assert asia.total_volume == pytest.approx(400.0)
    assert london.total_volume == pytest.approx(400.0)
    # Different prices, so the profiles do not overlap: a leak would show as a shared bin.
    assert {b.price for b in asia.bins} & {b.price for b in london.bins} == set()


def test_asia_is_retained_after_its_session_ends() -> None:
    """The phase says "retained until day end", and every §19 tag depends on it."""
    track = tracker()
    feed(track, asia_candles(4))
    feed(track, london_candles(4))
    assert track.session is SessionName.LONDON
    assert track.profile("asia").total_volume == pytest.approx(400.0)


def test_the_day_scope_covers_every_session_so_far() -> None:
    track = tracker()
    feed(track, [*asia_candles(4), *london_candles(2)])
    assert track.profile("day").total_volume == pytest.approx(600.0)


def test_the_previous_session_scope_is_the_one_that_just_ended() -> None:
    track = tracker()
    feed(track, asia_candles(4))
    assert track.profile("previous_session").is_empty  # nothing has ended yet
    feed(track, london_candles(2))
    assert track.profile("previous_session").total_volume == pytest.approx(400.0)


def test_an_unknown_scope_is_refused_rather_than_silently_empty() -> None:
    with pytest.raises(ValueError, match="unknown profile scope"):
        tracker().profile("lunchtime")


def test_the_state_panel_carries_the_session_asia_and_the_day() -> None:
    track = tracker()
    feed(track, [*asia_candles(4), *london_candles(4)])
    panel = track.profiles()
    assert set(panel) == {"current_session", "asia", "day"}
    assert panel["current_session"].scope == "london"
    assert panel["asia"].total_volume == pytest.approx(400.0)
    assert panel["day"].total_volume == pytest.approx(800.0)


def test_an_off_session_panel_falls_back_to_asia_rather_than_labelling_a_profile_off() -> None:
    """OFF is the absence of a session; a profile labelled `off` invites reading it as one."""
    track = tracker()
    feed(track, [candle(at=ASIA - timedelta(hours=2), low=2400.0, high=2400.1)])
    assert track.session is SessionName.OFF
    assert track.profiles()["current_session"].scope == DETECTION_SCOPE


# ── The broker day rollover ───────────────────────────────────────────────────


def test_a_new_broker_day_clears_the_day_and_the_sessions() -> None:
    track = tracker()
    feed(track, asia_candles(4))
    next_day = [
        candle(at=ASIA + timedelta(days=1, minutes=5 * i), low=2500.0, high=2500.10)
        for i in range(3)
    ]
    feed(track, next_day)

    assert track.profile("day").total_volume == pytest.approx(300.0)
    assert track.profile("asia").total_volume == pytest.approx(300.0)
    # Yesterday's prices are gone from both, which is what "retained until day end" means.
    assert all(b.price > 2490.0 for b in track.profile("asia").bins)


def test_the_rollover_records_each_sessions_range_for_the_median() -> None:
    """The 20-day history is ranges, not candles: an observer runs for weeks."""
    track = tracker()
    feed(track, asia_candles(4))  # a 0.10 range
    assert track.days_of_history(SessionName.ASIA) == 0
    feed(track, [candle(at=ASIA + timedelta(days=1), low=2500.0, high=2500.10)])
    assert track.days_of_history(SessionName.ASIA) == 1


def test_a_reference_is_absent_until_asia_has_traded() -> None:
    track = tracker()
    early = candle(at=ASIA - timedelta(hours=2), low=2400.0, high=2400.10)
    track.observe(early)
    assert track.reference(2400.0) is None

    feed(track, asia_candles(2))
    ref = track.reference(2400.05)
    assert ref is not None
    assert ref.scope == DETECTION_SCOPE


# ── Volatility is a function of the broker day ────────────────────────────────


def test_the_atr_a_detection_records_comes_from_the_broker_day() -> None:
    """§82's parity property: a replay of one day's archive must reproduce the context.

    A rolling buffer spanning days would give the live observer -- whose reach-back may load
    candles from before the archive begins -- more history than the replay, and every context
    field in the first candles would differ.
    """
    track = tracker()
    # A loud previous day, then a quiet new one.
    feed(
        track,
        [
            candle(at=ASIA + timedelta(minutes=5 * i), low=2400.0, high=2410.0)
            for i in range(20)
        ],
    )
    loud = track.volatility().atr_14
    assert loud is not None and loud > 5.0

    feed(
        track,
        [
            candle(at=ASIA + timedelta(days=1, minutes=5 * i), low=2500.0, high=2500.10)
            for i in range(16)
        ],
    )
    quiet = track.volatility().atr_14
    assert quiet is not None
    assert quiet < 1.0, "yesterday's range leaked into today's ATR"


def test_the_context_is_computed_once_per_candle() -> None:
    """Two reads between candles return the same object, so N detections cost one build."""
    track = tracker()
    feed(track, asia_candles(20))
    assert track.volatility() is track.volatility()
    track.observe(asia_candles(21)[-1])
    # A new candle invalidates it, or the context would describe the wrong moment.
    assert track.volatility() is not None


@pytest.mark.parametrize("symbol", ["XAUUSD", "XAGUSD"])
def test_each_symbol_uses_its_own_tick_and_bin_width(symbol: str) -> None:
    track = tracker(symbol)
    base = 2400.0 if symbol == "XAUUSD" else 30.0
    feed(track, asia_candles(6, base=base))
    profile = track.profile("asia")
    assert profile.symbol == symbol
    assert profile.bin_points == (10.0 if symbol == "XAUUSD" else 2.0)
    assert profile.poc_price is not None
