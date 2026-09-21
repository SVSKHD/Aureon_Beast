"""The volume profile (9B, §19).

A profile is unfalsifiable unless its inputs are pinned, so these tests do the arithmetic by
hand. Five candles with known volumes and known ranges have exactly one right POC, one right
value area, and a known set of nodes -- and every one of those answers depends on the bin
width, which is why it is part of the stored document.

The estimate under test is the assumption the module names: a candle's tick volume spread
across its high-low **in proportion to each bin's overlap** with that range. These tests pin
the proportion, because an equal-share-per-bin version would move the POC for a reason that
is purely arithmetic and would look just as plausible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.config.symbol_tuning import VALUE_AREA_FRACTION, tuning_for
from aureon.engine.volume_profile import (
    HVN_FRACTION_OF_POC,
    bin_index,
    build_profile,
    reference_for,
)
from aureon.models.base import MarketTime
from aureon.models.enums import Timeframe
from aureon.models.market import Candle
from aureon.models.profile import MAX_PROFILE_BINS

TZ = "Europe/Athens"
T0 = datetime(2026, 9, 16, 7, 0, tzinfo=UTC)
SYMBOL = "XAUUSD"
POINT = 0.01
#: 10 points at gold's tick: $0.10 bins, so 2400.00-2400.10 is one bin.
BIN_POINTS = 10.0
WIDTH = BIN_POINTS * POINT


def candle(
    *,
    minutes: int,
    low: float,
    high: float,
    volume: int,
    symbol: str = SYMBOL,
) -> Candle:
    moment = T0 + timedelta(minutes=minutes)
    return Candle(
        symbol=symbol,
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(moment, TZ),
        open=low,
        high=high,
        low=low,
        close=high,
        tick_volume=volume,
    )


def profile_of(candles, **kwargs):
    return build_profile(
        candles,
        bin_points=kwargs.pop("bin_points", BIN_POINTS),
        point=kwargs.pop("point", POINT),
        scope=kwargs.pop("scope", "asia"),
        **kwargs,
    )


# ── The hand-built profile ────────────────────────────────────────────────────


def five_candles() -> list[Candle]:
    """Five candles whose arithmetic is checkable by eye.

    Four of them sit entirely inside one $0.10 bin each; the third is a doji at 2400.05,
    whose whole volume lands in the 2400.00 bin. So the 2400.00 bin holds 100 + 300 = 400
    and is the POC, and nothing else is close.
    """
    return [
        candle(minutes=0, low=2400.00, high=2400.09, volume=100),   # bin 2400.00
        candle(minutes=5, low=2400.10, high=2400.19, volume=50),    # bin 2400.10
        candle(minutes=10, low=2400.05, high=2400.05, volume=300),  # doji, bin 2400.00
        candle(minutes=15, low=2400.20, high=2400.29, volume=30),   # bin 2400.20
        candle(minutes=20, low=2400.30, high=2400.39, volume=20),   # bin 2400.30
    ]


def test_the_poc_is_the_busiest_bin() -> None:
    profile = profile_of(five_candles())
    assert profile.poc_price == pytest.approx(2400.00)
    assert profile.total_tick_volume == pytest.approx(500.0)
    # Four bins touched, in price order, with the volumes the arithmetic gives.
    assert [b.price for b in profile.bins] == pytest.approx(
        [2400.00, 2400.10, 2400.20, 2400.30]
    )
    assert [b.tick_volume for b in profile.bins] == pytest.approx([400.0, 50.0, 30.0, 20.0])


def test_a_doji_puts_its_whole_volume_in_one_bin() -> None:
    """The one case where the even-spreading assumption is exactly right."""
    profile = profile_of([candle(minutes=0, low=2400.05, high=2400.05, volume=77)])
    assert [b.tick_volume for b in profile.bins] == pytest.approx([77.0])
    assert profile.poc_price == pytest.approx(2400.00)


def test_a_candle_spanning_bins_is_split_by_overlap_not_equally() -> None:
    """A bar covering 1.5 bins gives two thirds of its volume to the bin it fills.

    An equal-share-per-bin implementation would give 50/50 here and would look just as
    plausible -- and would move the POC whenever a wide bar straddled a boundary.
    """
    profile = profile_of([candle(minutes=0, low=2400.00, high=2400.15, volume=90)])
    volumes = {b.price: b.tick_volume for b in profile.bins}
    assert volumes[2400.00] == pytest.approx(60.0)  # 0.10 of the 0.15 range
    assert volumes[2400.10] == pytest.approx(30.0)  # 0.05 of it


def test_the_value_area_holds_at_least_seventy_percent(
) -> None:
    """§19's definition, asserted as the property rather than as an expected band."""
    profile = profile_of(five_candles())
    assert profile.value_area_low is not None
    inside = sum(
        b.tick_volume
        for b in profile.bins
        if profile.value_area_low <= b.price < profile.value_area_high
    )
    assert inside >= profile.total_tick_volume * VALUE_AREA_FRACTION
    # And it contains the POC, which a quantile of the price distribution need not.
    assert profile.value_area_low <= profile.poc_price < profile.value_area_high


def test_the_value_area_grows_outward_from_the_poc() -> None:
    """Two heavy bins either side of a light one: the band must be contiguous.

    A hole in a value area is the failure mode that makes one useless, because "70% of
    volume traded between these two prices" stops being true of the range it names.
    """
    candles = [
        candle(minutes=0, low=2400.00, high=2400.09, volume=200),
        candle(minutes=5, low=2400.10, high=2400.19, volume=10),
        candle(minutes=10, low=2400.20, high=2400.29, volume=190),
    ]
    profile = profile_of(candles)
    assert profile.value_area_low == pytest.approx(2400.00)
    assert profile.value_area_high == pytest.approx(2400.30)
    inside = sum(
        b.tick_volume
        for b in profile.bins
        if profile.value_area_low <= b.price < profile.value_area_high
    )
    assert inside == pytest.approx(profile.total_tick_volume)


def test_a_high_volume_node_is_a_local_peak_not_every_busy_bin() -> None:
    profile = profile_of(five_candles())
    assert profile.hvn == pytest.approx((2400.00,))
    # The threshold is a fraction of the POC, so a bin at half the POC is not a node.
    assert 50.0 < 400.0 * HVN_FRACTION_OF_POC


def test_an_untouched_price_inside_the_range_is_a_low_volume_node() -> None:
    """A price the market skipped is the strongest possible "little traded here"."""
    candles = [
        candle(minutes=0, low=2400.00, high=2400.09, volume=100),
        candle(minutes=5, low=2400.30, high=2400.39, volume=100),
    ]
    profile = profile_of(candles)
    skipped = [2400.10, 2400.20]
    assert any(
        any(abs(node - price) < 1e-9 for node in profile.lvn) for price in skipped
    ), f"a skipped price must be a low-volume node; got {profile.lvn}"


# ── Inputs are part of the answer ─────────────────────────────────────────────


def test_the_bin_width_changes_the_poc_which_is_why_it_is_stored() -> None:
    """The same candles, two widths, two different POCs -- both correct."""
    candles = [
        candle(minutes=0, low=2400.00, high=2400.04, volume=100),
        candle(minutes=5, low=2400.06, high=2400.09, volume=150),
    ]
    fine = profile_of(candles, bin_points=5.0)
    coarse = profile_of(candles, bin_points=10.0)
    assert fine.poc_price == pytest.approx(2400.05)
    assert coarse.poc_price == pytest.approx(2400.00)
    assert fine.bin_points == 5.0 and coarse.bin_points == 10.0


def test_each_symbols_bin_width_comes_from_its_tuning() -> None:
    """10 points is $0.10 of gold and $0.01 of silver (9B)."""
    assert tuning_for("XAUUSD").volume_bin_points == 10.0
    assert tuning_for("XAGUSD").volume_bin_points == 2.0
    # Which is what makes the two instruments' profiles comparable in shape at all:
    # gold's bin is 4.2e-5 of price, silver's 6.7e-5. Same order, not the same number.
    gold = tuning_for("XAUUSD")
    silver = tuning_for("XAGUSD")
    gold_share = gold.volume_bin_points * gold.point / 2400.0
    silver_share = silver.volume_bin_points * silver.point / 30.0
    assert 0.3 < gold_share / silver_share < 3.0


def test_bins_are_keyed_from_an_absolute_origin_so_scopes_are_comparable() -> None:
    """Two overlapping windows must share bin edges, or their POCs are different facts.

    The second assertion is the one that found a real bug: computed as ``price / width``,
    ``2400.10 / 0.10`` is 24000.999999999996 in binary, so every exact bin edge landed one
    bin low and the whole profile's shape drifted by a bin. The index is computed in ticks
    now, and this is what would catch the regression.
    """
    early = profile_of(five_candles()[:3], scope="asia")
    whole = profile_of(five_candles(), scope="day")
    edges_early = {b.price for b in early.bins}
    assert edges_early <= {b.price for b in whole.bins}
    assert (
        bin_index(2400.00, point=POINT, bin_points=BIN_POINTS) + 1
        == bin_index(2400.10, point=POINT, bin_points=BIN_POINTS)
    ), "an exact bin edge must land in the NEXT bin, not the previous one"


def test_a_negative_price_still_bins_monotonically() -> None:
    """No instrument trades below zero; a function that is wrong there cannot be checked."""
    assert bin_index(-0.05, point=POINT, bin_points=BIN_POINTS) < bin_index(
        0.05, point=POINT, bin_points=BIN_POINTS
    )


# ── Empty and degenerate windows ──────────────────────────────────────────────


def test_an_empty_window_is_an_empty_profile_not_an_error() -> None:
    """The Asia profile before Asia opens. Raising would make a caller invent a value."""
    profile = build_profile(
        [],
        bin_points=BIN_POINTS,
        point=POINT,
        scope="asia",
        symbol=SYMBOL,
        timeframe=Timeframe.M5,
    )
    assert profile.is_empty
    assert profile.poc_price is None
    assert profile.value_area_low is None
    assert profile.bins == ()


def test_candles_with_no_volume_contribute_nothing() -> None:
    profile = profile_of([candle(minutes=0, low=2400.0, high=2400.09, volume=0)])
    assert profile.is_empty


def test_a_nonsense_bin_width_is_refused_rather_than_dividing_by_zero() -> None:
    with pytest.raises(ValueError, match="positive"):
        profile_of(five_candles(), bin_points=0.0)
    with pytest.raises(ValueError, match="positive"):
        profile_of(five_candles(), point=0.0)


def test_the_bin_list_is_capped_and_says_so_in_the_model() -> None:
    """A profile is a shape; an unbounded map is how a document grows unnoticed."""
    candles = [
        candle(minutes=i * 5, low=2400.0 + i * 0.10, high=2400.09 + i * 0.10, volume=10 + i)
        for i in range(MAX_PROFILE_BINS + 40)
    ]
    profile = profile_of(candles)
    assert len(profile.bins) == MAX_PROFILE_BINS
    # Still price-ordered, and the total still describes the WHOLE scope.
    prices = [b.price for b in profile.bins]
    assert prices == sorted(prices)
    assert profile.total_tick_volume == pytest.approx(sum(c.tick_volume for c in candles))


# ── What a detection records ──────────────────────────────────────────────────


def test_a_reference_says_where_price_stood_relative_to_the_value_area() -> None:
    profile = profile_of(five_candles())
    assert reference_for(profile, 2400.05).price_vs_va == "inside"
    assert reference_for(profile, 2400.95).price_vs_va == "above"
    assert reference_for(profile, 2399.00).price_vs_va == "below"


def test_a_reference_carries_the_nearest_nodes_and_the_scope() -> None:
    profile = profile_of(five_candles(), scope="london")
    ref = reference_for(profile, 2400.12)
    assert ref.scope == "london"
    assert ref.poc_price == pytest.approx(2400.00)
    assert ref.nearest_hvn == pytest.approx(2400.00)


def test_a_reference_to_an_empty_profile_says_nothing_rather_than_zero() -> None:
    """Absent context is a gap; a zero would be a price."""
    empty = build_profile(
        [], bin_points=BIN_POINTS, point=POINT, scope="asia",
        symbol=SYMBOL, timeframe=Timeframe.M5,
    )
    ref = reference_for(empty, 2400.0)
    assert ref.poc_price is None
    assert ref.price_vs_va is None
    assert ref.nearest_hvn is None and ref.nearest_lvn is None


# ── Determinism and both symbols ──────────────────────────────────────────────


def test_the_same_candles_produce_the_same_profile() -> None:
    first = profile_of(five_candles())
    second = profile_of(five_candles())
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_a_profile_over_the_fixture_week_is_built_for_either_symbol(
    candles, silver_candles
) -> None:
    """Parity of shape, not of numbers (9B).

    Each symbol uses its own tick and its own bin width, so the two profiles are different
    facts -- what this asserts is that both are computable and neither collapses to one bin,
    which is what a shared bin width would do to one of them.
    """
    for symbol, week in (("XAUUSD", candles), ("XAGUSD", silver_candles)):
        tuning = tuning_for(symbol)
        profile = build_profile(
            week[:288],
            bin_points=tuning.volume_bin_points,
            point=tuning.point,
            scope="day",
        )
        assert profile.symbol == symbol
        assert profile.total_tick_volume > 0
        assert profile.poc_price is not None
        assert 3 <= len(profile.bins) <= MAX_PROFILE_BINS
        assert profile.value_area_low < profile.poc_price + tuning.volume_bin_points * tuning.point
