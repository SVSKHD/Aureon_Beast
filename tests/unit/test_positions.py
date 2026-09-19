"""The pure Phase 5 pieces: deal reconciliation and excursion tracking (§36, §44, §45).

No emulator needed -- both are functions over broker data.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aureon.models.base import MarketTime, utc_now
from aureon.models.broker import BrokerDeal
from aureon.models.enums import (
    DealEntry,
    Direction,
    ExcursionSource,
    Timeframe,
)
from aureon.models.market import Candle, QuoteSnapshot
from aureon.models.trade import CLOSE_REASON_CONVENTION, Trade
from aureon.positions.deal_reconciler import (
    CLOSE_REASON_BY_BROKER_REASON,
    close_reason_for,
    summarise_all,
    summarise_position,
)
from aureon.positions.excursion_tracker import ExcursionTracker, reconstruct_from_candles

TZ = "Europe/Athens"
T0 = utc_now()
POSITION = 555


def deal(
    deal_id: int,
    entry: DealEntry,
    volume: float,
    price: float,
    *,
    profit: float = 0.0,
    reason: str | None = None,
    position_id: int = POSITION,
    seconds: int = 0,
    commission: float = 0.0,
    swap: float = 0.0,
) -> BrokerDeal:
    return BrokerDeal(
        deal_id=deal_id,
        position_id=position_id,
        symbol="XAUUSD",
        direction=Direction.BUY,
        entry=entry,
        volume=volume,
        price=price,
        executed_at=T0 + timedelta(seconds=seconds),
        profit=profit,
        commission=commission,
        swap=swap,
        reason=reason,
    )


def trade(*, direction: Direction = Direction.BUY, open_price: float = 2400.0, **kwargs) -> Trade:
    base = dict(
        trade_id="t1",
        mt5_position_id=POSITION,
        symbol="XAUUSD",
        direction=direction,
        volume=0.10,
        open_price=open_price,
        open_time=MarketTime.from_utc(T0, TZ),
    )
    return Trade(**(base | kwargs))


# ── Close reasons (§44, decision 15) ──────────────────────────────────────────


@pytest.mark.parametrize(
    "broker_reason,expected",
    [
        ("sl", "sl"),
        ("tp", "tp"),
        ("mobile", "mobile"),
        ("client", "manual"),
        ("web", "manual"),
        ("expert", "discord"),
        ("so", "broker"),
        ("rollover", "broker"),
    ],
)
def test_broker_reasons_map_to_the_convention(broker_reason: str, expected: str) -> None:
    mapped, raw = close_reason_for(deal(1, DealEntry.OUT, 0.1, 2400.0, reason=broker_reason))
    assert mapped == expected
    assert raw == broker_reason


def test_an_unmapped_reason_keeps_its_raw_value() -> None:
    """Discarding it would destroy the evidence for widening the map (decision 15)."""
    mapped, raw = close_reason_for(deal(1, DealEntry.OUT, 0.1, 2400.0, reason="brand_new"))
    assert mapped == "unknown"
    assert raw == "brand_new"


def test_a_missing_reason_is_unknown() -> None:
    mapped, raw = close_reason_for(deal(1, DealEntry.OUT, 0.1, 2400.0, reason=None))
    assert mapped == "unknown"
    assert raw is None


def test_every_mapped_reason_is_in_the_convention() -> None:
    """A map entry outside the convention would store a value nothing else understands."""
    assert set(CLOSE_REASON_BY_BROKER_REASON.values()) <= CLOSE_REASON_CONVENTION


# ── Position outcomes (§36) ───────────────────────────────────────────────────


def test_a_full_close_is_summarised() -> None:
    outcome = summarise_position(
        [
            deal(1, DealEntry.IN, 0.25, 2400.0),
            deal(2, DealEntry.OUT, 0.25, 2395.0, profit=-125.0, reason="sl", seconds=60),
        ],
        POSITION,
    )
    assert outcome.is_fully_closed is True
    assert outcome.is_partially_closed is False
    assert outcome.open_price == pytest.approx(2400.0)
    assert outcome.close_price == pytest.approx(2395.0)
    assert outcome.close_reason == "sl"
    assert outcome.realized_pnl == pytest.approx(-125.0)


def test_a_partial_close_is_summarised() -> None:
    outcome = summarise_position(
        [
            deal(1, DealEntry.IN, 0.25, 2400.0),
            deal(2, DealEntry.OUT, 0.10, 2403.0, profit=30.0, reason="client", seconds=60),
        ],
        POSITION,
    )
    assert outcome.is_partially_closed is True
    assert outcome.is_fully_closed is False
    assert outcome.remaining_volume == pytest.approx(0.15)


def test_realized_pnl_sums_every_deals_profit() -> None:
    """The broker's arithmetic, not a recomputation from prices."""
    outcome = summarise_position(
        [
            deal(1, DealEntry.IN, 0.25, 2400.0, commission=-2.0),
            deal(2, DealEntry.OUT, 0.10, 2403.0, profit=30.0, seconds=10),
            deal(3, DealEntry.OUT, 0.15, 2405.0, profit=75.0, seconds=20, swap=-1.5),
        ],
        POSITION,
    )
    assert outcome.realized_pnl == pytest.approx(105.0)
    assert outcome.commission == pytest.approx(-2.0)
    assert outcome.swap == pytest.approx(-1.5)


def test_the_last_exit_supplies_the_close_price_and_reason() -> None:
    """That is the price the position finally left the market at."""
    outcome = summarise_position(
        [
            deal(1, DealEntry.IN, 0.20, 2400.0),
            deal(2, DealEntry.OUT, 0.10, 2403.0, reason="client", seconds=10),
            deal(3, DealEntry.OUT, 0.10, 2408.0, reason="tp", seconds=20),
        ],
        POSITION,
    )
    assert outcome.close_price == pytest.approx(2408.0)
    assert outcome.close_reason == "tp"


def test_deals_are_ordered_before_folding() -> None:
    """Broker history can arrive out of order; the 'last' exit must be by time."""
    outcome = summarise_position(
        [
            deal(3, DealEntry.OUT, 0.10, 2408.0, reason="tp", seconds=20),
            deal(1, DealEntry.IN, 0.20, 2400.0),
            deal(2, DealEntry.OUT, 0.10, 2403.0, reason="client", seconds=10),
        ],
        POSITION,
    )
    assert outcome.close_reason == "tp"


def test_an_inout_deal_counts_on_both_sides() -> None:
    """A reversal closes one direction and opens the other in a single deal.

    Treating it as a pure exit would leave the newly opened side invisible.
    """
    outcome = summarise_position(
        [deal(1, DealEntry.IN, 0.10, 2400.0), deal(2, DealEntry.INOUT, 0.10, 2405.0, seconds=10)],
        POSITION,
    )
    assert outcome.opened_volume == pytest.approx(0.20)
    assert outcome.closed_volume == pytest.approx(0.10)


def test_other_positions_deals_are_ignored() -> None:
    """Matching is by position id (§36), so a neighbour's deal must not leak in."""
    outcome = summarise_position(
        [
            deal(1, DealEntry.IN, 0.10, 2400.0),
            deal(2, DealEntry.OUT, 0.10, 2405.0, profit=50.0, position_id=999, seconds=10),
        ],
        POSITION,
    )
    assert outcome.exit_deals == []
    assert outcome.realized_pnl == pytest.approx(0.0)


def test_no_deals_yields_an_empty_outcome() -> None:
    outcome = summarise_position([], POSITION)
    assert outcome.is_fully_closed is False
    assert outcome.close_price is None


def test_summarise_all_splits_by_position() -> None:
    outcomes = summarise_all(
        [
            deal(1, DealEntry.IN, 0.10, 2400.0, position_id=1),
            deal(2, DealEntry.IN, 0.20, 2410.0, position_id=2),
            deal(3, DealEntry.OUT, 0.10, 2405.0, position_id=1, seconds=10),
        ]
    )
    assert set(outcomes) == {1, 2}
    assert outcomes[1].is_fully_closed is True
    assert outcomes[2].is_fully_closed is False


# ── Excursions (§45) ──────────────────────────────────────────────────────────


def quote(bid: float, *, seconds: int = 0) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol="XAUUSD",
        bid=bid,
        ask=bid + 0.30,
        point=0.01,
        captured_at=T0 + timedelta(seconds=seconds),
    )


def test_a_long_position_is_measured_at_the_bid() -> None:
    """The exit side of the book: using the mid would flatter every excursion."""
    tracker = ExcursionTracker(point=0.01)
    tracker.track(trade())
    tracker.on_quote("t1", quote(2401.00))
    tracker.on_quote("t1", quote(2398.00, seconds=1))

    current = tracker.current("t1")
    assert current.mfe == pytest.approx(100.0)  # 2401.00 - 2400.00
    assert current.mae == pytest.approx(-200.0)
    assert current.mfe_price == pytest.approx(2401.00)


def test_a_short_position_is_measured_at_the_ask_and_favours_down() -> None:
    tracker = ExcursionTracker(point=0.01)
    tracker.track(trade(direction=Direction.SELL))
    tracker.on_quote("t1", quote(2398.00))  # ask 2398.30
    current = tracker.current("t1")
    assert current.mfe == pytest.approx(170.0)  # 2400.00 - 2398.30


def test_only_a_new_extreme_triggers_a_write() -> None:
    """Otherwise every quote would write to Firestore.

    Note the first quote always writes: with no prior observation it sets BOTH extremes
    to the same value. Only once a range exists can a quote fall inside it.
    """
    tracker = ExcursionTracker(point=0.01)
    tracker.track(trade())
    assert tracker.on_quote("t1", quote(2401.00)) is not None, "first quote sets both"
    assert tracker.on_quote("t1", quote(2398.00, seconds=1)) is not None, "new low"
    # Strictly inside [2398.00, 2401.00]: neither extreme moves.
    assert tracker.on_quote("t1", quote(2400.00, seconds=2)) is None
    assert tracker.on_quote("t1", quote(2399.00, seconds=3)) is None


def test_the_first_observation_sets_both_extremes_to_the_same_value() -> None:
    """A single observation bounds the position's excursion from both sides.

    So ``mae`` can legitimately be positive early on -- "the worst it got was still
    +100 in our favour" -- for the same reason ``mfe`` can be negative.
    """
    tracker = ExcursionTracker(point=0.01)
    tracker.track(trade())
    tracker.on_quote("t1", quote(2401.00))
    current = tracker.current("t1")
    assert current.mfe == pytest.approx(100.0)
    assert current.mae == pytest.approx(100.0)


def test_mfe_may_be_negative() -> None:
    """The best price ever offered was still worse than entry -- informative, not a bug."""
    tracker = ExcursionTracker(point=0.01)
    tracker.track(trade())
    tracker.on_quote("t1", quote(2398.00))
    assert tracker.current("t1").mfe < 0


def test_an_untracked_trade_is_ignored() -> None:
    assert ExcursionTracker().on_quote("nope", quote(2400.0)) is None


def test_tracking_resumes_from_stored_extremes() -> None:
    """A monitor restart must not discard hours of accumulated extremes."""
    from aureon.models.trade import Excursion

    stored = Excursion(mfe=500.0, mae=-300.0, source=ExcursionSource.LIVE_TICKS)
    tracker = ExcursionTracker(point=0.01)
    tracker.track(trade(excursion=stored))
    # A modest new quote must not displace the bigger stored extremes.
    tracker.on_quote("t1", quote(2400.50))
    current = tracker.current("t1")
    assert current.mfe == pytest.approx(500.0)
    assert current.mae == pytest.approx(-300.0)


def test_reconstruction_is_always_labelled_reconstructed() -> None:
    """§45. A candle cannot say whether the position was still open at its high."""
    candles = [
        Candle(
            symbol="XAUUSD",
            timeframe=Timeframe.M1,
            open_time=MarketTime.from_utc(T0 + timedelta(minutes=i), TZ),
            open=2400.0,
            high=2400.0 + i,
            low=2400.0 - i,
            close=2400.0,
        )
        for i in range(1, 4)
    ]
    rebuilt = reconstruct_from_candles(trade(), candles, point=0.01)
    assert rebuilt.source is ExcursionSource.RECONSTRUCTED
    assert rebuilt.mfe == pytest.approx(300.0)
    assert rebuilt.mae == pytest.approx(-300.0)


def test_candles_outside_the_positions_life_are_ignored() -> None:
    """Otherwise someone else's price action is attributed to this trade."""

    def candle(minutes: int, high: float, low: float) -> Candle:
        return Candle(
            symbol="XAUUSD",
            timeframe=Timeframe.M1,
            open_time=MarketTime.from_utc(T0 + timedelta(minutes=minutes), TZ),
            open=2400.0,
            high=high,
            low=low,
            close=2400.0,
        )

    before = candle(-10, 2500.0, 2300.0)
    during = candle(1, 2401.0, 2399.0)
    after = candle(120, 2600.0, 2200.0)
    rebuilt = reconstruct_from_candles(
        trade(close_time=MarketTime.from_utc(T0 + timedelta(minutes=10), TZ)),
        [before, during, after],
        point=0.01,
    )
    assert rebuilt.mfe == pytest.approx(100.0)
    assert rebuilt.mae == pytest.approx(-100.0)


def test_a_weaker_label_survives_a_live_resume() -> None:
    """The record must describe the weakest measurement it contains (§45)."""
    from aureon.models.trade import Excursion

    reconstructed = Excursion(mfe=100.0, mae=-50.0, source=ExcursionSource.RECONSTRUCTED)
    tracker = ExcursionTracker(point=0.01)
    tracker.track(trade(excursion=reconstructed))
    tracker.on_quote("t1", quote(2405.00))
    assert tracker.current("t1").source is ExcursionSource.RECONSTRUCTED


def test_release_returns_the_final_excursion_and_stops_tracking() -> None:
    tracker = ExcursionTracker(point=0.01)
    tracker.track(trade())
    tracker.on_quote("t1", quote(2401.00))
    final = tracker.release("t1")
    assert final is not None and final.mfe == pytest.approx(100.0)
    assert tracker.tracked == 0
    assert tracker.current("t1") is None
