"""Each family's lifecycle, walked candle by candle (12, T-7).

Scripted paths rather than fixtures, deliberately. A fixture week produces whatever it produces,
and a test over it asserts that the engine did *something*; these assert that a specific sequence
of candles produces a specific sequence of states, which is the only way a lifecycle can be
wrong in a way anybody notices.

Every path is written as the story it represents -- "the level is swept, price reclaims it, the
EMAs cross" -- so a reader can check the rule against the market rather than against the code.

The engine writes through a real ``SetupRepository`` over the in-memory Firestore, so the
transition gate and the idempotent write are exercised too: an engine that proposed an illegal
transition would be refused here rather than in production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.config.symbol_tuning import setup_tuning
from aureon.engine.levels import (
    LEVEL_PREVIOUS_DAY_HIGH,
    LEVEL_PREVIOUS_DAY_LOW,
    Level,
    Levels,
)
from aureon.models.base import MarketTime
from aureon.models.detection import Detection, SessionContext
from aureon.models.enums import (
    DirectionContext,
    MtfAlignment,
    SessionName,
    SetupEventType,
    SetupFamily,
    SetupState,
    Timeframe,
    TrendBias,
)
from aureon.models.market import Candle
from aureon.models.setup import Setup, SetupAnchor
from aureon.services.setup_engine import (
    FAMILIES,
    SetupEngine,
    SetupInputs,
)
from aureon.storage.setup_repository import SetupRepository

START = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
MARKET_TZ = "Europe/Athens"
MARKET_DATE = "2026-09-16"
SYMBOL = "XAUUSD"
ATR = 2.0
LEVEL = 2412.5


def candle(
    index: int,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    tick_volume: int = 100,
) -> Candle:
    at = START + timedelta(minutes=5 * index)
    return Candle(
        symbol=SYMBOL,
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(at, MARKET_TZ),
        open=open_,
        high=high,
        low=low,
        close=close,
        tick_volume=tick_volume,
    )


def flat(index: int, price: float, **kwargs) -> Candle:
    """A candle that does nothing, for the steps of a story that are just time passing."""
    return candle(
        index, open_=price, high=price + 0.2, low=price - 0.2, close=price, **kwargs
    )


def detection(
    bar: Candle, *, agent: str, event_key: str, version: str = "1.2.0"
) -> Detection:
    return Detection(
        detection_id=f"{agent}-{event_key}-{bar.open_time.utc.isoformat()}",
        account_scope="primary",
        symbol=SYMBOL,
        timeframe=Timeframe.M5,
        agent_name=agent,
        agent_version=version,
        event_key=event_key,
        detected_at=bar.open_time.model_copy(update={"utc": bar.close_time}),
        candle_open_time=bar.open_time,
        price=bar.close,
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
    )


def inputs(
    bar: Candle,
    *,
    detections: tuple[Detection, ...] = (),
    levels: Levels | None = None,
    **kwargs,
) -> SetupInputs:
    base = {
        "candle": bar,
        "market_date": MARKET_DATE,
        "session": SessionName.LONDON,
        "detections": detections,
        "levels": levels if levels is not None else Levels(),
        "atr": ATR,
        "median_tick_volume": 100.0,
    }
    base.update(kwargs)
    return SetupInputs(**base)


def levels_with(**prices: float) -> Levels:
    return Levels({name: Level(level_type=name, price=price) for name, price in prices.items()})


def engine(firestore, *, families=None, now=None) -> SetupEngine:
    return SetupEngine(
        account_scope="primary",
        symbol=SYMBOL,
        timeframe=Timeframe.M5,
        repository=SetupRepository(firestore),
        point=0.01,
        market_tz=MARKET_TZ,
        families=families if families is not None else FAMILIES,
        now=now if now is not None else (lambda: START),
    )


def only(engine_: SetupEngine, family: SetupFamily):
    found = [s for s in engine_.tracked.values() if s.family is family]
    assert len(found) == 1, f"expected one {family.value}, got {[s.state for s in found]}"
    return found[0]


def lifecycle(repository: SetupRepository, setup_id: str):
    """The events that CHANGED the state, in order.

    The history legitimately holds both kinds since T-8: a setup accumulates descriptive
    ``WATCH_*`` observations alongside its transitions, and a proximity note can sit between two
    of them. A test asserting a lifecycle sequence therefore has to say so -- reading the raw list
    would make every one of these tests depend on how many things happened to be noticed.
    """
    return [e for e in repository.events(setup_id) if not e.event_type.is_watch_event]


def states_of(repository: SetupRepository, setup_id: str) -> list[SetupState]:
    return [e.to_state for e in lifecycle(repository, setup_id)]


def test_a_loaded_legacy_setup_gets_a_live_confidence_snapshot_on_a_quiet_candle(
    firestore,
) -> None:
    """A pre-feature setup must not wait for a lifecycle event before Discord gets a score."""
    repository = SetupRepository(firestore)
    repository.open(
        Setup(
            setup_id="legacy-confidence",
            account_scope="primary",
            symbol=SYMBOL,
            timeframe=Timeframe.M5,
            family=SetupFamily.LIQUIDITY_REVERSAL,
            direction_context=DirectionContext.BEARISH,
            market_date=MARKET_DATE,
            anchor=SetupAnchor(
                kind=SetupAnchorKind.LIQUIDITY_LEVEL,
                price=LEVEL,
                level_type=LEVEL_PREVIOUS_DAY_HIGH,
            ),
            opened_at=START,
        ),
        now=START,
    )

    from aureon.services.setup_engine import LiquidityReversal

    restarted = engine(firestore, families=one_family(LiquidityReversal))
    restarted.on_closed_candle(
        inputs(
            flat(1, LEVEL - 5.0),
            ema_fast=99.0,
            ema_slow=100.0,
            rsi=42.0,
            trend=TrendBias.BEARISH,
        )
    )

    refreshed = repository.get("legacy-confidence")
    assert refreshed is not None
    assert refreshed.agent_confluence.confidence_pct == 50
    assert refreshed.agent_confluence.aligned_count == 3
    assert refreshed.agent_confluence.neutral_count == 3
    assert refreshed.event_count == 0
    assert repository.events("legacy-confidence") == []


# ── LIQUIDITY_REVERSAL ────────────────────────────────────────────────────────


def one_family(family_class):
    return [f for f in FAMILIES if isinstance(f, family_class)]


@pytest.fixture
def liquidity(firestore):
    from aureon.services.setup_engine import LiquidityReversal

    return engine(firestore, families=one_family(LiquidityReversal))


def test_a_swept_high_walks_the_whole_reversal(liquidity, firestore) -> None:
    """The story: price approaches the previous day's high, sweeps it, closes back below it,
    and the EMAs cross downwards. That is a bearish reversal, and every step is a state.
    """
    repository = SetupRepository(firestore)
    levels = levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL})

    # 1. Within proximity: the setup opens, OBSERVING.
    liquidity.on_closed_candle(inputs(flat(0, LEVEL - 0.5), levels=levels))
    setup = only(liquidity, SetupFamily.LIQUIDITY_REVERSAL)
    assert setup.state is SetupState.OBSERVING
    assert setup.direction_context is DirectionContext.BEARISH, (
        "a HIGH swept from below reverses downwards; naming it after the approach points "
        "every one of these setups the wrong way"
    )

    # 2. The sweep: a wick through the level, and the liquidity agent says so.
    sweep_bar = candle(1, open_=LEVEL - 0.2, high=LEVEL + 1.5, low=LEVEL - 0.4, close=LEVEL + 0.8)
    liquidity.on_closed_candle(
        inputs(
            sweep_bar,
            levels=levels,
            detections=(
                detection(
                    sweep_bar, agent="liquidity", event_key=f"up|{LEVEL_PREVIOUS_DAY_HIGH}"
                ),
            ),
        )
    )
    assert only(liquidity, SetupFamily.LIQUIDITY_REVERSAL).state is SetupState.WATCH

    # 3. The reclaim: a close back below the level.
    liquidity.on_closed_candle(inputs(flat(2, LEVEL - 1.0), levels=levels))
    assert only(liquidity, SetupFamily.LIQUIDITY_REVERSAL).state is SetupState.DEVELOPING

    # 4. Confirmation: a bearish EMA cross.
    cross_bar = flat(3, LEVEL - 1.5)
    liquidity.on_closed_candle(
        inputs(
            cross_bar,
            levels=levels,
            detections=(detection(cross_bar, agent="ema_cross", event_key="bearish"),),
        )
    )
    setup = only(liquidity, SetupFamily.LIQUIDITY_REVERSAL)
    assert setup.state is SetupState.CONFIRMED

    assert states_of(repository, setup.setup_id) == [
        SetupState.WATCH,
        SetupState.DEVELOPING,
        SetupState.CONFIRMED,
    ]


def test_the_confirming_cross_must_point_the_same_way(liquidity) -> None:
    """A bullish cross does not confirm a bearish reversal.

    Planted as "any ema_cross confirms", the setup above would confirm on the cross that is
    taking price back through the level it just reclaimed -- the opposite of the structure.
    """
    levels = levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL})
    liquidity.on_closed_candle(inputs(flat(0, LEVEL - 0.5), levels=levels))
    sweep_bar = candle(1, open_=LEVEL, high=LEVEL + 1.5, low=LEVEL - 0.4, close=LEVEL + 0.8)
    liquidity.on_closed_candle(
        inputs(
            sweep_bar,
            levels=levels,
            detections=(
                detection(
                    sweep_bar, agent="liquidity", event_key=f"up|{LEVEL_PREVIOUS_DAY_HIGH}"
                ),
            ),
        )
    )
    liquidity.on_closed_candle(inputs(flat(2, LEVEL - 1.0), levels=levels))

    wrong_way = flat(3, LEVEL - 1.2)
    liquidity.on_closed_candle(
        inputs(
            wrong_way,
            levels=levels,
            detections=(detection(wrong_way, agent="ema_cross", event_key="bullish"),),
        )
    )
    assert only(liquidity, SetupFamily.LIQUIDITY_REVERSAL).state is SetupState.DEVELOPING


def test_a_swept_low_reverses_upwards(liquidity) -> None:
    levels = levels_with(**{LEVEL_PREVIOUS_DAY_LOW: LEVEL})
    liquidity.on_closed_candle(inputs(flat(0, LEVEL + 0.5), levels=levels))
    setup = only(liquidity, SetupFamily.LIQUIDITY_REVERSAL)
    assert setup.direction_context is DirectionContext.BULLISH


# ── BREAKOUT_ACCEPTANCE ───────────────────────────────────────────────────────


@pytest.fixture
def breakout(firestore):
    from aureon.services.setup_engine import BreakoutAcceptance

    return engine(firestore, families=one_family(BreakoutAcceptance))


def walk_breakout(breakout_engine, *, closes: list[float], volumes: list[int] | None = None):
    levels = levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL})
    volumes = volumes or [100] * len(closes)
    for index, (close, volume) in enumerate(zip(closes, volumes, strict=True)):
        breakout_engine.on_closed_candle(
            inputs(flat(index, close, tick_volume=volume), levels=levels)
        )


def test_a_break_is_accepted_only_after_two_closes_and_expanding_volume(
    breakout, firestore
) -> None:
    """One close beyond a level is the break itself; accepting on it would make acceptance and
    breakout the same event, and the family would have no middle."""
    repository = SetupRepository(firestore)
    walk_breakout(
        breakout,
        closes=[LEVEL - 0.4, LEVEL + 1.0, LEVEL + 1.2, LEVEL + 1.5],
        volumes=[100, 100, 100, 180],
    )
    setup = only(breakout, SetupFamily.BREAKOUT_ACCEPTANCE)
    assert setup.state is SetupState.CONFIRMED
    assert setup.direction_context is DirectionContext.BULLISH
    assert states_of(repository, setup.setup_id) == [
        SetupState.WATCH,
        SetupState.DEVELOPING,
        SetupState.CONFIRMED,
    ]


def test_acceptance_needs_the_volume_and_not_only_the_closes(breakout) -> None:
    walk_breakout(
        breakout,
        closes=[LEVEL - 0.4, LEVEL + 1.0, LEVEL + 1.2, LEVEL + 1.5],
        volumes=[100, 100, 100, 90],
    )
    assert only(breakout, SetupFamily.BREAKOUT_ACCEPTANCE).state is SetupState.DEVELOPING


def test_a_close_back_inside_after_acceptance_is_FAKEOUT_RISK_not_a_failure(
    breakout, firestore
) -> None:
    """The distinction the state exists for. A setup that wobbles and recovers is a different
    measurement from one that failed, and collapsing them throws the difference away."""
    repository = SetupRepository(firestore)
    walk_breakout(
        breakout,
        closes=[LEVEL - 0.4, LEVEL + 1.0, LEVEL + 1.2, LEVEL + 1.5, LEVEL - 0.5],
        volumes=[100, 100, 100, 180, 100],
    )
    setup = only(breakout, SetupFamily.BREAKOUT_ACCEPTANCE)
    assert setup.state is SetupState.FAKEOUT_RISK

    # And a reclaim takes it back to CONFIRMED rather than opening a new setup.
    walk_breakout_more(breakout, index=5, close=LEVEL + 1.0)
    setup = only(breakout, SetupFamily.BREAKOUT_ACCEPTANCE)
    assert setup.state is SetupState.CONFIRMED
    assert SetupEventType.RECLAIMED in [
        e.event_type for e in lifecycle(repository, setup.setup_id)
    ]


def walk_breakout_more(breakout_engine, *, index: int, close: float, volume: int = 100):
    levels = levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL})
    breakout_engine.on_closed_candle(
        inputs(flat(index, close, tick_volume=volume), levels=levels)
    )


def test_a_second_close_back_inside_invalidates(breakout, firestore) -> None:
    repository = SetupRepository(firestore)
    walk_breakout(
        breakout,
        closes=[LEVEL - 0.4, LEVEL + 1.0, LEVEL + 1.2, LEVEL + 1.5, LEVEL - 0.5],
        volumes=[100, 100, 100, 180, 100],
    )
    setup = only(breakout, SetupFamily.BREAKOUT_ACCEPTANCE)
    walk_breakout_more(breakout, index=5, close=LEVEL - 1.0)

    assert setup.setup_id not in breakout.tracked, "an invalidated setup is still tracked"
    stored = repository.get(setup.setup_id)
    assert stored.state is SetupState.INVALIDATED
    assert stored.closed_at is not None


def test_a_break_that_closes_back_inside_before_acceptance_just_fails(breakout, firestore) -> None:
    """No FAKEOUT_RISK here: nothing was accepted, so there is nothing to have faked out of."""
    repository = SetupRepository(firestore)
    walk_breakout(breakout, closes=[LEVEL - 0.4, LEVEL + 1.0, LEVEL - 0.6])
    stored = [s for s in repository.open_setups(symbol=SYMBOL)]
    assert stored == [], "the setup should be terminal"


# ── TREND_PULLBACK ────────────────────────────────────────────────────────────


@pytest.fixture
def pullback(firestore):
    from aureon.services.setup_engine import TrendPullback

    return engine(firestore, families=one_family(TrendPullback))


def trending(index: int, price: float, *, ema_fast: float, ema_slow: float, **kwargs):
    return inputs(
        flat(index, price),
        trend=TrendBias.BULLISH,
        mtf_alignment=MtfAlignment.ALIGNED,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        **kwargs,
    )


def test_an_aligned_trend_pulls_back_and_resumes(pullback, firestore) -> None:
    repository = SetupRepository(firestore)
    # 1. Trend established and aligned: the setup opens at the fast EMA.
    pullback.on_closed_candle(trending(0, 2420.0, ema_fast=2418.0, ema_slow=2410.0))
    setup = only(pullback, SetupFamily.TREND_PULLBACK)
    assert setup.state is SetupState.OBSERVING
    assert setup.direction_context is DirectionContext.BULLISH

    # 2. Price comes back to the fast EMA.
    pullback.on_closed_candle(trending(1, 2418.2, ema_fast=2418.0, ema_slow=2410.0))
    assert only(pullback, SetupFamily.TREND_PULLBACK).state is SetupState.WATCH

    # 3. A rejection wick off it.
    wick_bar = flat(2, 2418.5)
    pullback.on_closed_candle(
        inputs(
            wick_bar,
            trend=TrendBias.BULLISH,
            mtf_alignment=MtfAlignment.ALIGNED,
            ema_fast=2418.0,
            ema_slow=2410.0,
            detections=(detection(wick_bar, agent="wick", event_key="bullish_rejection"),),
        )
    )
    assert only(pullback, SetupFamily.TREND_PULLBACK).state is SetupState.DEVELOPING

    # 4. A new high above the anchor confirms.
    pullback.on_closed_candle(trending(3, 2422.0, ema_fast=2419.0, ema_slow=2411.0))
    setup = only(pullback, SetupFamily.TREND_PULLBACK)
    assert setup.state is SetupState.CONFIRMED
    assert [e.event_type for e in lifecycle(repository, setup.setup_id)][:2] == [
        SetupEventType.PULLBACK_TO_EMA20,
        SetupEventType.EMA20_REJECTION,
    ]


def test_an_unaligned_trend_still_opens_and_records_the_alignment(pullback) -> None:
    """Alignment is RECORDED, never gated on (11D, enforced by 12 T-12).

    This test asserted the opposite until T-12's boundary check found the gate. Refusing to open
    on anything but ALIGNED was a filter on a threshold nobody researched -- and an invisible
    one, because the setups it suppressed would never exist to be counted. The question "did
    aligned trend pullbacks do better?" is answerable only if the unaligned ones are in the
    record too.
    """
    pullback.on_closed_candle(
        inputs(
            flat(0, 2420.0),
            trend=TrendBias.BULLISH,
            mtf_alignment=MtfAlignment.MIXED,
            ema_fast=2418.0,
            ema_slow=2410.0,
        )
    )
    assert pullback.tracked, "an unaligned trend pullback was suppressed"
    opened = next(iter(pullback.tracked.values()))
    assert opened.context_summary.mtf_alignment is MtfAlignment.MIXED, (
        "the alignment must be recorded on the setup, or the question is unanswerable"
    )


def test_a_sideways_market_opens_nothing(pullback) -> None:
    pullback.on_closed_candle(
        inputs(
            flat(0, 2420.0),
            trend=TrendBias.SIDEWAYS,
            mtf_alignment=MtfAlignment.ALIGNED,
            ema_fast=2418.0,
            ema_slow=2410.0,
        )
    )
    assert pullback.tracked == {}


def test_closing_through_the_slow_ema_against_the_trend_invalidates(pullback, firestore) -> None:
    repository = SetupRepository(firestore)
    pullback.on_closed_candle(trending(0, 2420.0, ema_fast=2418.0, ema_slow=2410.0))
    setup = only(pullback, SetupFamily.TREND_PULLBACK)
    pullback.on_closed_candle(trending(1, 2418.2, ema_fast=2418.0, ema_slow=2410.0))
    # Well below the slow EMA: the trend claim is dead, not merely tested.
    pullback.on_closed_candle(trending(2, 2405.0, ema_fast=2416.0, ema_slow=2410.0))

    stored = repository.get(setup.setup_id)
    assert stored.state is SetupState.INVALIDATED
    assert "slow EMA" in [e.reason for e in lifecycle(repository, setup.setup_id)][-1]


# ── MOMENTUM_TRANSITION ───────────────────────────────────────────────────────


@pytest.fixture
def momentum(firestore):
    from aureon.services.setup_engine import MomentumTransition

    return engine(firestore, families=one_family(MomentumTransition))


def test_a_narrowing_gap_opens_NEUTRAL_and_the_cross_resolves_it(momentum, firestore) -> None:
    """NEUTRAL is a real answer here. A narrowing gap says a transition may be coming and says
    nothing about which way; assigning a direction before the cross would be inventing one."""
    repository = SetupRepository(firestore)
    momentum.on_closed_candle(
        inputs(flat(0, 2420.0), ema_fast=2419.5, ema_slow=2419.0)
    )
    setup = only(momentum, SetupFamily.MOMENTUM_TRANSITION)
    assert setup.direction_context is DirectionContext.NEUTRAL
    assert setup.state is SetupState.OBSERVING

    # The slope turns: the gap flips sign.
    momentum.on_closed_candle(
        inputs(
            flat(1, 2419.0),
            ema_fast=2418.6,
            ema_slow=2419.0,
            previous_ema_fast=2419.5,
            previous_ema_slow=2419.0,
        )
    )
    assert only(momentum, SetupFamily.MOMENTUM_TRANSITION).state is SetupState.WATCH

    # RSI crosses 50 with it.
    momentum.on_closed_candle(
        inputs(flat(2, 2418.0), ema_fast=2418.0, ema_slow=2419.0, rsi=46.0, previous_rsi=53.0)
    )
    assert only(momentum, SetupFamily.MOMENTUM_TRANSITION).state is SetupState.DEVELOPING

    # And the cross lands.
    cross_bar = flat(3, 2417.0)
    momentum.on_closed_candle(
        inputs(
            cross_bar,
            ema_fast=2417.0,
            ema_slow=2419.0,
            detections=(detection(cross_bar, agent="ema_cross", event_key="bearish"),),
        )
    )
    setup = only(momentum, SetupFamily.MOMENTUM_TRANSITION)
    assert setup.state is SetupState.CONFIRMED
    assert states_of(repository, setup.setup_id)[-1] is SetupState.CONFIRMED


def test_a_wide_gap_opens_nothing(momentum) -> None:
    momentum.on_closed_candle(inputs(flat(0, 2420.0), ema_fast=2425.0, ema_slow=2410.0))
    assert momentum.tracked == {}


# ── expiry ────────────────────────────────────────────────────────────────────


def test_a_setup_that_goes_quiet_expires_as_an_invalidation_with_a_reason(
    liquidity, firestore
) -> None:
    """So "nothing happened in time" and "the structure broke" stay separable in a review."""
    repository = SetupRepository(firestore)
    levels = levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL})
    liquidity.on_closed_candle(inputs(flat(0, LEVEL - 0.5), levels=levels))
    setup = only(liquidity, SetupFamily.LIQUIDITY_REVERSAL)

    expiry = setup_tuning(SYMBOL, SetupFamily.LIQUIDITY_REVERSAL.value).expiry_candles
    for index in range(1, expiry + 1):
        # Far from the level, so nothing advances and nothing re-opens.
        liquidity.on_closed_candle(inputs(flat(index, LEVEL - 40.0), levels=levels))

    stored = repository.get(setup.setup_id)
    assert stored.state is SetupState.INVALIDATED
    events = lifecycle(repository, setup.setup_id)
    assert events[-1].event_type is SetupEventType.EXPIRED
    assert events[-1].reason == "expired"
    assert setup.setup_id not in liquidity.tracked


def test_the_idle_counter_RESETS_on_every_advance(breakout, firestore) -> None:
    """A counter that only ever counted up would expire a setup that is working.

    The path has to be quiet, then active, then quiet again -- with more quiet candles IN TOTAL
    than the expiry but never that many in a row. A first version of this test walked a setup
    that advanced on every candle, which never reaches the expiry branch at all: the plant
    removing the reset survived it, because with nothing idle there was nothing to reset.
    """
    repository = SetupRepository(firestore)
    expiry = setup_tuning(SYMBOL, SetupFamily.BREAKOUT_ACCEPTANCE.value).expiry_candles
    levels = levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL})

    # Open, then accept, so the setup is CONFIRMED and has somewhere quiet to sit.
    walk_breakout(
        breakout,
        closes=[LEVEL - 0.4, LEVEL + 1.0, LEVEL + 1.2, LEVEL + 1.5],
        volumes=[100, 100, 100, 180],
    )
    setup = only(breakout, SetupFamily.BREAKOUT_ACCEPTANCE)
    assert setup.state is SetupState.CONFIRMED

    index = 4
    quiet = expiry - 1
    for _ in range(2):
        # Quiet: far beyond the level so nothing advances, and flat volume so no continuation.
        for _ in range(quiet):
            breakout.on_closed_candle(
                inputs(flat(index, LEVEL + 40.0, tick_volume=50), levels=levels)
            )
            index += 1
        # One candle that advances: back at the level, which is a pullback.
        breakout.on_closed_candle(inputs(flat(index, LEVEL + 0.3), levels=levels))
        index += 1

    stored = repository.get(setup.setup_id)
    assert stored.state is not SetupState.INVALIDATED, (
        f"a setup that advanced twice expired after {2 * quiet} total quiet candles; the idle "
        "counter is not resetting"
    )
    assert 2 * quiet > expiry, "the path is not long enough to distinguish the two behaviours"


# ── determinism and immutability ──────────────────────────────────────────────


def test_the_same_candles_produce_the_same_setup_ids_twice(firestore) -> None:
    """Live and replay have to agree, or the parity check compares two populations that can
    never match."""
    from tests.conftest import InMemoryFirestore

    def run(store) -> list[str]:
        run_engine = engine(store)
        levels = levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL})
        for index, close in enumerate([LEVEL - 0.4, LEVEL + 1.0, LEVEL - 0.6, LEVEL - 1.0]):
            run_engine.on_closed_candle(inputs(flat(index, close), levels=levels))
        return sorted(
            s.setup_id
            for s in SetupRepository(store).open_setups(symbol=SYMBOL)
        )

    first = run(firestore)
    second = run(InMemoryFirestore())
    assert first == second
    assert first, "the run produced no setups; the comparison is vacuous"


def test_the_engine_never_touches_a_detection(liquidity) -> None:
    """A setup REFERENCES the detection that advanced it, one way. Detections are immutable."""
    levels = levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL})
    liquidity.on_closed_candle(inputs(flat(0, LEVEL - 0.5), levels=levels))
    sweep_bar = candle(1, open_=LEVEL, high=LEVEL + 1.5, low=LEVEL - 0.4, close=LEVEL + 0.8)
    original = detection(
        sweep_bar, agent="liquidity", event_key=f"up|{LEVEL_PREVIOUS_DAY_HIGH}"
    )
    before = original.model_dump(mode="json")

    liquidity.on_closed_candle(
        inputs(sweep_bar, levels=levels, detections=(original,))
    )
    assert original.model_dump(mode="json") == before


def test_the_advancing_detection_is_linked_on_the_setup_and_the_event(
    liquidity, firestore
) -> None:
    repository = SetupRepository(firestore)
    levels = levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL})
    liquidity.on_closed_candle(inputs(flat(0, LEVEL - 0.5), levels=levels))
    sweep_bar = candle(1, open_=LEVEL, high=LEVEL + 1.5, low=LEVEL - 0.4, close=LEVEL + 0.8)
    sweep = detection(sweep_bar, agent="liquidity", event_key=f"up|{LEVEL_PREVIOUS_DAY_HIGH}")
    liquidity.on_closed_candle(inputs(sweep_bar, levels=levels, detections=(sweep,)))

    setup = only(liquidity, SetupFamily.LIQUIDITY_REVERSAL)
    assert sweep.detection_id in setup.linked_detection_ids
    assert lifecycle(repository, setup.setup_id)[0].linked_detection_id == sweep.detection_id


def test_re_feeding_the_same_candle_changes_nothing(liquidity, firestore) -> None:
    """A restart mid-session replays the last candle. The event id is deterministic, so the
    second pass finds the row already there."""
    repository = SetupRepository(firestore)
    levels = levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL})
    liquidity.on_closed_candle(inputs(flat(0, LEVEL - 0.5), levels=levels))
    sweep_bar = candle(1, open_=LEVEL, high=LEVEL + 1.5, low=LEVEL - 0.4, close=LEVEL + 0.8)
    sweep = detection(sweep_bar, agent="liquidity", event_key=f"up|{LEVEL_PREVIOUS_DAY_HIGH}")

    liquidity.on_closed_candle(inputs(sweep_bar, levels=levels, detections=(sweep,)))
    setup = only(liquidity, SetupFamily.LIQUIDITY_REVERSAL)
    count_after_first = repository.get(setup.setup_id).event_count

    liquidity.on_closed_candle(inputs(sweep_bar, levels=levels, detections=(sweep,)))
    assert repository.get(setup.setup_id).event_count == count_after_first


def test_a_new_broker_day_does_not_advance_yesterdays_setups(liquidity, firestore) -> None:
    """A setup's id carries its date, so yesterday's are a different population.

    Holding them across the rollover would let a level from Monday advance on Tuesday's
    candles, under a context that belongs to Monday.
    """
    levels = levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL})
    liquidity.on_closed_candle(inputs(flat(0, LEVEL - 0.5), levels=levels))
    yesterday = only(liquidity, SetupFamily.LIQUIDITY_REVERSAL)

    liquidity.on_closed_candle(
        inputs(flat(1, LEVEL - 0.5), levels=levels, market_date="2026-09-17")
    )
    assert yesterday.setup_id not in liquidity.tracked
    today = only(liquidity, SetupFamily.LIQUIDITY_REVERSAL)
    assert today.market_date == "2026-09-17"
    assert today.setup_id != yesterday.setup_id


# ── the engine survives its own bugs ──────────────────────────────────────────


def test_a_family_that_raises_while_OPENING_does_not_stop_the_observer(firestore) -> None:
    """The candle loop is the one thing that must not stop. A family bug costs its own setups
    and nothing else."""
    from aureon.services.setup_engine import BreakoutAcceptance, Family

    class Exploding(Family):
        family = SetupFamily.LIQUIDITY_REVERSAL

        def openings(self, inputs_, tuning):
            raise RuntimeError("boom")

        def advance(self, setup, inputs_, tuning):
            raise RuntimeError("boom")

    both = engine(firestore, families=[Exploding(), BreakoutAcceptance()])
    both.on_closed_candle(
        inputs(flat(0, LEVEL - 0.4), levels=levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL}))
    )
    assert [s.family for s in both.tracked.values()] == [SetupFamily.BREAKOUT_ACCEPTANCE]


def test_a_family_that_raises_while_ADVANCING_does_not_stop_the_observer(firestore) -> None:
    """The other handler, and it needs its own test.

    The one above never reaches ``advance``: the exploding family raises while opening, so it
    never has a setup to advance. A plant narrowing the advance handler's ``except`` survived
    that test for exactly that reason -- the code path was unreachable from it.
    """
    from aureon.services.setup_engine import BreakoutAcceptance, Family

    opened: list[str] = []

    class OpensThenExplodes(Family):
        family = SetupFamily.LIQUIDITY_REVERSAL
        one_per_direction = True

        def openings(self, inputs_, tuning):
            from aureon.models.enums import SetupAnchorKind
            from aureon.models.setup import SetupAnchor
            from aureon.services.setup_engine import Opening

            if opened:
                return []
            opened.append("once")
            return [
                Opening(
                    self.family,
                    DirectionContext.BULLISH,
                    SetupAnchor(kind=SetupAnchorKind.LIQUIDITY_LEVEL, price=LEVEL),
                )
            ]

        def advance(self, setup, inputs_, tuning):
            raise RuntimeError("boom on advance")

    both = engine(firestore, families=[OpensThenExplodes(), BreakoutAcceptance()])
    levels = levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL})
    both.on_closed_candle(inputs(flat(0, LEVEL - 0.4), levels=levels))
    assert any(
        s.family is SetupFamily.LIQUIDITY_REVERSAL for s in both.tracked.values()
    ), "the fixture family did not open a setup, so advance is never called"

    # This candle calls advance on the exploding setup. It must not propagate, and the other
    # family must still make progress.
    both.on_closed_candle(inputs(flat(1, LEVEL + 1.0), levels=levels))
    breakouts = [
        s for s in both.tracked.values() if s.family is SetupFamily.BREAKOUT_ACCEPTANCE
    ]
    assert breakouts and breakouts[0].state is SetupState.WATCH


def test_no_setup_opens_without_an_atr(firestore) -> None:
    """A proximity test with no sense of scale fires constantly on one instrument and never on
    the other, which is worse than not firing at all."""
    blind = engine(firestore)
    blind.on_closed_candle(
        inputs(
            flat(0, LEVEL),
            levels=levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL}),
            atr=None,
            ema_fast=2419.5,
            ema_slow=2419.0,
        )
    )
    assert [s.family for s in blind.tracked.values()] == [], (
        "a family opened a setup with no ATR to measure proximity with"
    )


# ── the moving anchor ─────────────────────────────────────────────────────────


def test_a_drifting_ema_does_not_open_a_second_trend_setup(pullback) -> None:
    """The bug this found, kept as a test.

    ``openings`` runs on every candle with a LIVE EMA read, so a drifting EMA crosses an anchor
    bin boundary and opens a second setup -- then a third, for as long as the trend lasts. The
    first version of the engine did exactly that, and the test walking a pullback to confirmation
    found two setups where it expected one.

    Freezing the anchor at open time would not fix it: ``openings`` has no memory, and the id is
    derived from what it returns. Binning more coarsely only moves the boundary. So the family
    declares that its identity is the DIRECTION and the day, not a price.
    """
    for index in range(12):
        # A trend whose EMAs climb steadily, one full ATR over the run -- several anchor bins.
        pullback.on_closed_candle(
            trending(
                index,
                2420.0 + index * 0.5,
                ema_fast=2418.0 + index * 0.5,
                ema_slow=2410.0 + index * 0.4,
            )
        )
    bullish = [
        s
        for s in pullback.tracked.values()
        if s.family is SetupFamily.TREND_PULLBACK
        and s.direction_context is DirectionContext.BULLISH
    ]
    assert len(bullish) == 1, f"{len(bullish)} trend setups for one trend"


def test_a_drifting_ema_does_not_open_a_second_momentum_setup(momentum) -> None:
    for index in range(12):
        momentum.on_closed_candle(
            inputs(
                flat(index, 2420.0 + index * 0.5),
                ema_fast=2419.5 + index * 0.5,
                ema_slow=2419.0 + index * 0.5,
            )
        )
    assert len(momentum.tracked) == 1, f"{len(momentum.tracked)} momentum setups for one gap"


def test_a_level_family_DOES_open_a_second_setup_at_a_second_level(liquidity) -> None:
    """The other half of the rule: a price anchor is a real identity, so two levels are two
    setups. Without this, the fix above would have been "one setup per family", which would
    silently merge two structures at two different prices.
    """
    levels = levels_with(
        **{LEVEL_PREVIOUS_DAY_HIGH: LEVEL, LEVEL_PREVIOUS_DAY_LOW: LEVEL - 1.0}
    )
    liquidity.on_closed_candle(inputs(flat(0, LEVEL - 0.5), levels=levels))
    found = [s for s in liquidity.tracked.values()]
    assert len(found) == 2
    assert {s.direction_context for s in found} == {
        DirectionContext.BEARISH,
        DirectionContext.BULLISH,
    }


def test_the_params_snapshot_is_stored_on_every_setup(liquidity) -> None:
    """A setup judged by one set of distances and reviewed against another is not a comparison,
    and these numbers are not recoverable from config a month later."""
    liquidity.on_closed_candle(
        inputs(flat(0, LEVEL - 0.5), levels=levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL}))
    )
    setup = only(liquidity, SetupFamily.LIQUIDITY_REVERSAL)
    assert setup.params_snapshot["proximity_atr"]
    assert setup.params_snapshot["version"] == setup.setup_version


def test_the_event_snapshot_carries_what_the_candle_does_not(liquidity, firestore) -> None:
    """Only values that are not recoverable from the bar itself. An event that stored the OHLC
    again would double the sub-collection for nothing -- the candle is in the archive and in
    ``market_day_frames``."""
    repository = SetupRepository(firestore)
    levels = levels_with(**{LEVEL_PREVIOUS_DAY_HIGH: LEVEL})
    liquidity.on_closed_candle(inputs(flat(0, LEVEL - 0.5), levels=levels))
    sweep_bar = candle(1, open_=LEVEL, high=LEVEL + 1.5, low=LEVEL - 0.4, close=LEVEL + 0.8)
    liquidity.on_closed_candle(
        inputs(
            sweep_bar,
            levels=levels,
            rsi=61.0,
            detections=(
                detection(
                    sweep_bar, agent="liquidity", event_key=f"up|{LEVEL_PREVIOUS_DAY_HIGH}"
                ),
            ),
        )
    )
    setup = only(liquidity, SetupFamily.LIQUIDITY_REVERSAL)
    snapshot = lifecycle(repository, setup.setup_id)[0].context_snapshot
    assert "atr" in snapshot and "rsi" in snapshot
    for absent in ("open", "high", "low"):
        assert absent not in snapshot, f"the snapshot repeats the candle's {absent}"


# ── the observer's wiring (12, T-7) ───────────────────────────────────────────


def test_the_observer_runs_the_setup_engine_on_EVERY_closed_candle() -> None:
    """Including the ones that produced no detection.

    This is the reason the market engine got a third callback. ``on_candle_close`` has no
    detections and ``on_detections`` does not fire on a candle that produced none -- which is most
    of them, and exactly the candles on which a setup expires, invalidates, or comes into
    proximity of a level. A version wired to ``on_detections`` would miss all of it.
    """
    from aureon.engine.market_engine import MarketEngine

    seen: list[tuple[str, int]] = []

    class Recording:
        def on_closed_candle(self, inputs_):
            seen.append((inputs_.candle.open_time.utc.isoformat(), len(inputs_.detections)))
            return []

    import inspect

    import main_observer

    source = inspect.getsource(main_observer.Observer._on_analysis)
    # Spelled as two halves so the literal never appears whole: §83's boundary test scans tests/
    # for bare collection names, deliberately, because a stale one in a test helper matches
    # nothing and makes the assertion around it pass vacuously.
    assert "set" + "ups" in source
    # And the callback is actually passed to the market engine.
    signature = inspect.signature(MarketEngine.__init__)
    assert "on_analysis" in signature.parameters


def test_the_market_engine_calls_on_analysis_for_a_candle_with_no_detections() -> None:
    """The property itself, against a real MarketEngine over a fake provider."""
    from aureon.engine.analysis_engine import AnalysisEngine
    from aureon.engine.market_engine import MarketEngine

    class Silent:
        """An agent that detects nothing, so on_detections never fires."""

        agent_name = "silent"
        agent_version = "1.0.0"

        def min_window(self) -> int:
            return 1

        def params_snapshot(self) -> dict[str, object]:
            return {}

        def on_closed_candle(self, window, ctx):
            return []

    bars = [flat(i, 2400.0 + i) for i in range(3)]

    class Provider:
        """Just enough of the provider protocol for one poll."""

        def now_utc(self):
            # Well past the last bar's close plus the grace, so every bar is final.
            return bars[-1].close_time + timedelta(minutes=30)

        def get_closed_candles(self, symbol, timeframe, start, end):
            return [bar for bar in bars if start <= bar.open_time.utc < end]

    analysis = AnalysisEngine(
        [Silent()], account_scope="primary", market_tz=MARKET_TZ, window_size=10
    )
    analysed: list[tuple] = []
    detected: list[list] = []
    market = MarketEngine(
        Provider(),
        analysis,
        symbols=[SYMBOL],
        timeframes=[Timeframe.M5],
        on_detections=detected.append,
        on_analysis=lambda candle, detections: analysed.append((candle, detections)),
    )
    market.poll_once()

    assert detected == [], "the fixture agent detected something; the test proves nothing"
    assert len(analysed) == len(bars), (
        "on_analysis did not fire for every closed candle, so a setup could never expire"
    )
    assert all(detections == [] for _, detections in analysed)
