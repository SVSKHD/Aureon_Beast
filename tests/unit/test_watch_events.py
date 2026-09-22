"""The seventeen descriptive observations, and the vocabulary they may not use (12, T-8).

Two kinds of test here, and the second is the more important one.

The first kind checks that each observation fires when it should and not otherwise. Ordinary work.

The second checks that the whole codebase and every document avoid four words: **delta**,
**absorption**, **footprint** and **order-flow**. Those name things this system cannot see. MT5's
``tick_volume`` is a count of price CHANGES in a bar — not contracts, not aggression, not resting
size — and a field or a card that used one of those words would be making a claim no data here
supports. A reader would believe it, because the rest of the system is careful.

That is why it is a test and not a convention: the words are seductive, they are what every
trading forum uses, and nothing about adding one would fail.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureon.config.symbol_tuning import setup_tuning
from aureon.engine.levels import (
    LEVEL_ASIA_HIGH,
    LEVEL_PREVIOUS_DAY_HIGH,
    LEVEL_SWING_LOW,
    Level,
    Levels,
)
from aureon.engine.watch_events import (
    REPEATED_TEST_TOUCHES,
    VOLUME_EXPANSION_RATIO,
    LevelTouchCounter,
    WatchObservation,
    breakout_pressure,
    observations,
)
from aureon.models.base import MarketTime
from aureon.models.enums import (
    WATCH_EVENT_TYPES,
    SessionName,
    SetupEventType,
    SetupFamily,
    Timeframe,
)
from aureon.models.market import Candle
from aureon.services.setup_engine import SetupInputs

REPO_ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
MARKET_TZ = "Europe/Athens"
SYMBOL = "XAUUSD"
ATR = 2.0
LEVEL = 2412.5
TUNING = setup_tuning(SYMBOL, SetupFamily.LIQUIDITY_REVERSAL.value)


def bar(
    index: int = 0,
    *,
    open_: float = LEVEL,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    tick_volume: int = 100,
) -> Candle:
    at = START + timedelta(minutes=5 * index)
    return Candle(
        symbol=SYMBOL,
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(at, MARKET_TZ),
        open=open_,
        high=high if high is not None else open_ + 0.2,
        low=low if low is not None else open_ - 0.2,
        close=close if close is not None else open_,
        tick_volume=tick_volume,
    )


def inputs(candle: Candle | None = None, *, levels: dict[str, float] | None = None, **kwargs):
    base = {
        "candle": candle if candle is not None else bar(),
        "market_date": "2026-09-16",
        "session": SessionName.LONDON,
        "levels": Levels(
            {
                name: Level(level_type=name, price=price)
                for name, price in (levels or {}).items()
            }
        ),
        "atr": ATR,
        "median_tick_volume": 100.0,
    }
    base.update(kwargs)
    return SetupInputs(**base)


def types_of(found) -> set[SetupEventType]:
    return {observation.event_type for observation in found}


# ── nothing here may change a state ───────────────────────────────────────────


def test_every_observation_is_a_descriptive_event() -> None:
    """The one property the whole module rests on.

    A lifecycle event smuggled in here would change a setup's state through a path that was never
    meant to, and every reader filtering on ``is_watch_event`` would be filtering out a transition.
    """
    found = observations(
        inputs(
            bar(high=LEVEL + 1.0, low=LEVEL - 1.0, tick_volume=300),
            levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL},
            rsi=45.0,
            previous_rsi=55.0,
            ema_fast=2412.4,
            ema_slow=2412.6,
            previous_ema_fast=2412.8,
            previous_ema_slow=2412.6,
            poc_price=LEVEL,
            value_area_high=LEVEL + 0.1,
            value_area_low=LEVEL - 1.0,
            price_vs_va="inside",
        ),
        TUNING,
    )
    assert found, "the fixture produced no observations; the assertion below is vacuous"
    for observation in found:
        assert observation.event_type.is_watch_event, observation.event_type


def test_a_lifecycle_event_cannot_be_wrapped_as_an_observation() -> None:
    """Refused at construction rather than at the write, so the error names the mistake."""
    with pytest.raises(ValueError, match="lifecycle event"):
        WatchObservation(
            SetupEventType.CONFIRMED, frozenset({SetupFamily.TREND_PULLBACK}), "no"
        )


def test_every_named_watch_event_is_reachable_from_this_module() -> None:
    """T-8 names seventeen. A value in the enum that nothing emits is a name in the runbook and
    in no history, which is exactly the drift the ops register's closed set exists to prevent."""
    import inspect

    from aureon.engine import watch_events

    source = inspect.getsource(watch_events)
    missing = sorted(
        event.name for event in WATCH_EVENT_TYPES if event.name not in source
    )
    assert not missing, f"no code path emits these: {missing}"
    assert len(WATCH_EVENT_TYPES) == 17


# ── proximity ─────────────────────────────────────────────────────────────────


def test_a_previous_day_extreme_and_a_session_extreme_are_different_observations() -> None:
    """A single PROXIMITY_LEVEL with the kind in a detail field would make "how often does a
    previous-day extreme matter" a question needing a scan rather than a group-by."""
    found = observations(
        inputs(levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL, LEVEL_ASIA_HIGH: LEVEL + 0.1}), TUNING
    )
    assert SetupEventType.PROXIMITY_PREV_DAY_EXTREME in types_of(found)
    assert SetupEventType.PROXIMITY_SESSION_EXTREME in types_of(found)


def test_a_swing_level_is_a_plain_liquidity_proximity() -> None:
    found = observations(inputs(levels={LEVEL_SWING_LOW: LEVEL}), TUNING)
    assert types_of(found) == {SetupEventType.PROXIMITY_LIQUIDITY_LEVEL}


def test_a_distant_level_is_not_noticed() -> None:
    found = observations(inputs(levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL + 50.0}), TUNING)
    assert found == []


def test_nothing_is_noticed_without_an_atr() -> None:
    """A proximity with no sense of scale fires constantly on one instrument and never on the
    other, which is worse than not firing at all."""
    found = observations(
        inputs(levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL}, atr=None), TUNING
    )
    assert SetupEventType.PROXIMITY_PREV_DAY_EXTREME not in types_of(found)


def test_the_observation_carries_the_level_it_is_about() -> None:
    """So the engine can attach it to the setup anchored THERE and not to one twenty points
    away."""
    found = observations(inputs(levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL}), TUNING)
    assert found[0].anchor_price == LEVEL
    assert found[0].detail["level_type"] == LEVEL_PREVIOUS_DAY_HIGH


# ── the profile ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "event"),
    [
        ("poc_price", SetupEventType.PROXIMITY_POC),
        ("value_area_high", SetupEventType.PROXIMITY_VAH),
        ("value_area_low", SetupEventType.PROXIMITY_VAL),
    ],
)
def test_each_profile_node_has_its_own_proximity(field: str, event) -> None:
    found = observations(inputs(**{field: LEVEL}), TUNING)
    assert event in types_of(found)


def test_a_close_back_inside_the_value_area_is_a_reclaim() -> None:
    found = observations(
        inputs(value_area_high=LEVEL + 5.0, value_area_low=LEVEL - 5.0, price_vs_va="inside"),
        TUNING,
    )
    assert SetupEventType.PROFILE_RECLAIM in types_of(found)
    assert SetupEventType.PROFILE_REJECTION not in types_of(found)


@pytest.mark.parametrize("where", ["above", "below"])
def test_a_close_outside_the_value_area_is_a_rejection(where: str) -> None:
    found = observations(
        inputs(value_area_high=LEVEL + 5.0, value_area_low=LEVEL - 5.0, price_vs_va=where),
        TUNING,
    )
    assert SetupEventType.PROFILE_REJECTION in types_of(found)
    assert found[-1].detail["price_vs_va"] == where


# ── momentum ──────────────────────────────────────────────────────────────────


def test_a_narrow_ema_gap_is_noticed() -> None:
    found = observations(inputs(ema_fast=LEVEL + 0.1, ema_slow=LEVEL), TUNING)
    assert SetupEventType.EMA_GAP_NARROWING in types_of(found)


def test_a_wide_ema_gap_is_not() -> None:
    found = observations(inputs(ema_fast=LEVEL + 20.0, ema_slow=LEVEL), TUNING)
    assert SetupEventType.EMA_GAP_NARROWING not in types_of(found)


def test_the_fast_ema_crossing_the_slow_one_is_noticed() -> None:
    found = observations(
        inputs(
            ema_fast=LEVEL - 0.1,
            ema_slow=LEVEL,
            previous_ema_fast=LEVEL + 0.1,
            previous_ema_slow=LEVEL,
        ),
        TUNING,
    )
    assert SetupEventType.EMA_FAST_SLOPE_CHANGE in types_of(found)


def test_an_ema_pair_that_stays_on_one_side_is_not_a_slope_change() -> None:
    found = observations(
        inputs(
            ema_fast=LEVEL + 0.2,
            ema_slow=LEVEL,
            previous_ema_fast=LEVEL + 0.1,
            previous_ema_slow=LEVEL,
        ),
        TUNING,
    )
    assert SetupEventType.EMA_FAST_SLOPE_CHANGE not in types_of(found)


@pytest.mark.parametrize(("was", "now"), [(55.0, 45.0), (45.0, 55.0)])
def test_rsi_crossing_fifty_is_noticed(was: float, now: float) -> None:
    found = observations(inputs(rsi=now, previous_rsi=was), TUNING)
    assert SetupEventType.RSI_MOMENTUM_TURN in types_of(found)
    assert found[-1].detail["rsi"] == f"{now:.2f}"


def test_rsi_moving_without_crossing_fifty_is_not() -> None:
    found = observations(inputs(rsi=65.0, previous_rsi=55.0), TUNING)
    assert SetupEventType.RSI_MOMENTUM_TURN not in types_of(found)


# ── tick volume ───────────────────────────────────────────────────────────────


def test_expanding_tick_volume_is_noticed_and_named_honestly() -> None:
    found = observations(
        inputs(bar(tick_volume=int(100 * VOLUME_EXPANSION_RATIO) + 10)), TUNING
    )
    assert SetupEventType.TICK_VOLUME_EXPANSION in types_of(found)
    detail = found[0].detail
    assert "tick_volume" in detail and "median_tick_volume" in detail
    assert "volume" in found[0].reason and "tick" in found[0].reason


def test_ordinary_tick_volume_is_not_noticed() -> None:
    found = observations(inputs(bar(tick_volume=100)), TUNING)
    assert SetupEventType.TICK_VOLUME_EXPANSION not in types_of(found)


def test_nothing_is_noticed_about_volume_without_a_median() -> None:
    """A ratio against nothing is not a ratio."""
    found = observations(inputs(bar(tick_volume=5000), median_tick_volume=None), TUNING)
    assert SetupEventType.TICK_VOLUME_EXPANSION not in types_of(found)


def test_expansion_at_a_level_is_its_own_observation() -> None:
    found = observations(
        inputs(bar(tick_volume=400), levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL}), TUNING
    )
    assert SetupEventType.VOLUME_EXPANSION_AT_LEVEL in types_of(found)


def test_a_busy_bar_through_a_level_that_closes_beyond_it_is_a_sweep() -> None:
    found = observations(
        inputs(
            bar(open_=LEVEL - 0.5, high=LEVEL + 1.0, low=LEVEL - 0.6, close=LEVEL + 0.5,
                tick_volume=400),
            levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL},
        ),
        TUNING,
    )
    assert SetupEventType.HIGH_TICK_VOLUME_SWEEP in types_of(found)
    assert SetupEventType.HIGH_TICK_VOLUME_REJECTION not in types_of(found)


def test_a_busy_bar_through_a_level_that_closes_back_inside_is_a_rejection() -> None:
    """The two are different observations, and the difference is where it CLOSED."""
    found = observations(
        inputs(
            bar(open_=LEVEL - 0.5, high=LEVEL + 1.0, low=LEVEL - 0.6, close=LEVEL - 0.4,
                tick_volume=400),
            levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL},
        ),
        TUNING,
    )
    assert SetupEventType.HIGH_TICK_VOLUME_REJECTION in types_of(found)
    assert SetupEventType.HIGH_TICK_VOLUME_SWEEP not in types_of(found)


# ── the two with memory ───────────────────────────────────────────────────────


def test_a_level_reached_three_times_is_noticed_once() -> None:
    """Once, not on every touch after the third. A repeated message is the failure the ops
    register exists to avoid, and a setup's history has the same reader."""
    counter = LevelTouchCounter()
    fired = []
    for index in range(REPEATED_TEST_TOUCHES + 3):
        fired.extend(
            counter.observe(
                inputs(bar(index), levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL}), TUNING
            )
        )
    assert [o.event_type for o in fired] == [SetupEventType.REPEATED_LEVEL_TEST]
    assert fired[0].detail["touches"] == str(REPEATED_TEST_TOUCHES)


def test_two_touches_are_a_coincidence() -> None:
    counter = LevelTouchCounter()
    fired = []
    for index in range(REPEATED_TEST_TOUCHES - 1):
        fired.extend(
            counter.observe(
                inputs(bar(index), levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL}), TUNING
            )
        )
    assert fired == []


def test_re_feeding_a_candle_does_not_count_the_touch_twice() -> None:
    """The bug this found, kept as a test.

    Everything else in the setup path is idempotent through a deterministic document id; this
    counter is the one piece of state that is not. A restart mid-session replays the last bar, the
    counter counted the same touch twice, and on the third replay it crossed the threshold and
    wrote a REPEATED_LEVEL_TEST event the first pass had not written -- so "re-processing a candle
    changes nothing" was false, visible only as one extra row in one setup's history.
    """
    counter = LevelTouchCounter()
    same = inputs(bar(0), levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL})
    fired = []
    for _ in range(REPEATED_TEST_TOUCHES + 2):
        fired.extend(counter.observe(same, TUNING))
    assert fired == [], "a replayed candle counted its touch again"


def test_the_counter_resets_on_a_new_day() -> None:
    """A count spanning two days answers a question about neither."""
    counter = LevelTouchCounter()
    for index in range(REPEATED_TEST_TOUCHES):
        counter.observe(inputs(bar(index), levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL}), TUNING)
    counter.reset()
    fired = []
    for index in range(REPEATED_TEST_TOUCHES - 1):
        fired.extend(
            counter.observe(
                inputs(bar(index + 100), levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL}), TUNING
            )
        )
    assert fired == [], "the counter kept yesterday's touches"


def test_pressing_against_a_level_without_closing_through_it_is_noticed() -> None:
    found = breakout_pressure(
        inputs(
            bar(open_=LEVEL - 1.0, high=LEVEL + 0.3, low=LEVEL - 1.2, close=LEVEL - 0.4),
            levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL},
        ),
        TUNING,
    )
    assert found is not None
    assert found.event_type is SetupEventType.BREAKOUT_PRESSURE


def test_closing_through_a_level_is_not_pressure() -> None:
    found = breakout_pressure(
        inputs(
            bar(open_=LEVEL - 1.0, high=LEVEL + 0.8, low=LEVEL - 1.2, close=LEVEL + 0.6),
            levels={LEVEL_PREVIOUS_DAY_HIGH: LEVEL},
        ),
        TUNING,
    )
    assert found is None


# ── the vocabulary ────────────────────────────────────────────────────────────

#: Words for things this system cannot see. MT5's ``tick_volume`` counts price CHANGES; nothing
#: here observes contracts, aggression, resting size or which side initiated.
#:
#: Three of the four are banned as whole words, because they have no innocent use in this
#: codebase. ``delta`` is different and the first version of this list got it wrong: banning the
#: bare word fired on ``delta = values.diff()`` inside the RSI -- which is a first difference, and
#: exactly what the word means in mathematics -- and on a seconds-until-open ``delta`` in the
#: market-state service. Both are correct code, and a rule that calls correct code a violation is
#: a rule somebody deletes.
#:
#: So ``delta`` is banned in its MARKET sense only: the phrases that would claim this system can
#: see which side traded. The cost of the narrower rule is that a new phrasing could slip through;
#: the cost of the blunt one was three false positives on day one.
FORBIDDEN = ("absorption", "footprint", "order flow", "order-flow", "orderflow")

#: The market sense of "delta", as phrases. Matched case-insensitively with flexible separators so
#: "volume delta", "volume-delta" and "Volume Delta" are all caught.
FORBIDDEN_PHRASES = (
    r"volume[\s_-]*delta",
    r"delta[\s_-]*volume",
    r"cumulative[\s_-]*delta",
    r"bid[\s_-]*ask[\s_-]*delta",
    r"delta[\s_-]*divergence",
)

#: Files that are ALLOWED to contain the words, because their subject is the ban itself.
EXEMPT_FILES = {
    "tests/unit/test_watch_events.py",
    "docs/DOCS_CHECK.md",
    "docs/PHASE1_DECISIONS.md",
}


def scanned_files() -> list[Path]:
    found = [
        *(REPO_ROOT / "aureon").rglob("*.py"),
        *(REPO_ROOT / "docs").rglob("*.md"),
        *(REPO_ROOT / "scripts").rglob("*.py"),
        REPO_ROOT / "CLAUDE.md",
    ]
    return sorted(
        path
        for path in found
        if str(path.relative_to(REPO_ROOT)) not in EXEMPT_FILES and path.is_file()
    )


def test_there_are_files_to_scan() -> None:
    """A glob that found nothing would make the check below pass on an empty set."""
    assert len(scanned_files()) > 50


@pytest.mark.parametrize("word", FORBIDDEN)
def test_no_module_or_document_uses_a_word_for_something_we_cannot_see(word: str) -> None:
    """One test per word, so a failure names the word rather than the whole list.

    A field or a card calling ``tick_volume`` any of these would be making a claim no data in this
    system supports -- and a reader would believe it, because the rest of the system is careful.
    """
    pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
    hits: list[str] = []
    for path in scanned_files():
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:90]}")
    assert not hits, (
        f"{word!r} names something this system cannot observe. MT5's tick_volume counts price "
        "CHANGES, not contracts or aggression:\n  " + "\n  ".join(hits)
    )


@pytest.mark.parametrize("pattern", FORBIDDEN_PHRASES)
def test_no_module_or_document_claims_to_see_which_side_traded(pattern: str) -> None:
    """The market sense of "delta", which this system has no data for.

    Separate from the whole-word list above because the bare word is legitimate arithmetic -- see
    the comment on ``FORBIDDEN``.
    """
    compiled = re.compile(pattern, re.IGNORECASE)
    hits: list[str] = []
    for path in scanned_files():
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if compiled.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:90]}")
    assert not hits, (
        f"{pattern!r} claims this system can see which side traded. It sees tick_volume, a count "
        "of price changes:\n  " + "\n  ".join(hits)
    )


def test_the_forbidden_lists_are_not_empty_and_the_matchers_work() -> None:
    """Guards the guard: a regex that matched nothing would make every test above vacuous."""
    assert FORBIDDEN and FORBIDDEN_PHRASES
    assert re.search(rf"\b{re.escape('absorption')}\b", "the absorption there", re.IGNORECASE)
    assert re.search(FORBIDDEN_PHRASES[0], "cumulative Volume-Delta", re.IGNORECASE)
    # And the arithmetic sense must NOT match, or the narrowing achieved nothing.
    for pattern in FORBIDDEN_PHRASES:
        assert not re.search(pattern, "delta = values.diff()", re.IGNORECASE), pattern
