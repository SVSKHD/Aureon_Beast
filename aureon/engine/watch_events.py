"""The things worth noticing before anything has happened (12, T-8).

Seventeen observations, all descriptive. Each one records that something was true at a candle
close — price came within reach of a level, the EMA pair narrowed, tick volume expanded — and
**none of them changes a setup's state**. They are recorded as ``SetupEvent``s of a ``WATCH_*``
type, whose ``from_state`` equals its ``to_state``, and the model refuses any other shape.

## Why they are events and not detections

A detection is an immutable claim about a candle, with a hashed id, an evaluation, and a place in
a population a review counts. These are none of that: "price is near the previous day's high" is
true on dozens of consecutive candles and is not a finding. Minting a detection for each would
double the detections collection with rows nothing measures, and every hit rate computed over that
collection would silently change meaning.

They are also not trades, not signals and not suggestions. Nothing reads them to decide anything;
they exist so that a setup's history says *why it looked interesting* before it looked like
anything, which is what a review needs to ask whether the early signs were worth watching.

## Distances are ATR multiples, and the vocabulary is honest

Every threshold is a multiple of ATR(14) from ``aureon/config/symbol_tuning.py``'s setup section,
versioned there. A distance in points is different money per instrument (decision 141); a distance
in ATR is the same market distance on gold and on silver, which is why no value here needs a
per-symbol number.

``tick_volume`` is what MT5 reports: the number of price CHANGES in a bar. It is not contracts
traded. This system cannot see which orders were resting, which side was the aggressor, or how
much size changed hands -- only that the price moved and how often. So the words for those things
are banned across the codebase and the docs, and a test enforces it.
``HIGH_TICK_VOLUME_SWEEP`` means "a sweep on a bar with many price changes" and nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass

from aureon.config.symbol_tuning import SetupTuning
from aureon.engine.levels import (
    LEVEL_ASIA_HIGH,
    LEVEL_ASIA_LOW,
    LEVEL_LONDON_HIGH,
    LEVEL_LONDON_LOW,
    LEVEL_PREVIOUS_DAY_HIGH,
    LEVEL_PREVIOUS_DAY_LOW,
    LEVEL_PREVIOUS_SESSION_HIGH,
    LEVEL_PREVIOUS_SESSION_LOW,
)
from aureon.models.enums import SetupEventType, SetupFamily

#: Levels that are a SESSION's extreme rather than a day's or a swing's. Named because
#: ``PROXIMITY_SESSION_EXTREME`` and ``PROXIMITY_PREV_DAY_EXTREME`` are different observations and
#: a reader grouping by event type must not find them mixed.
SESSION_LEVELS: frozenset[str] = frozenset(
    {
        LEVEL_ASIA_HIGH,
        LEVEL_ASIA_LOW,
        LEVEL_LONDON_HIGH,
        LEVEL_LONDON_LOW,
        LEVEL_PREVIOUS_SESSION_HIGH,
        LEVEL_PREVIOUS_SESSION_LOW,
    }
)

PREV_DAY_LEVELS: frozenset[str] = frozenset(
    {LEVEL_PREVIOUS_DAY_HIGH, LEVEL_PREVIOUS_DAY_LOW}
)

#: How many times a level must be touched within the window before the touches are worth noting
#: as a pattern rather than as individual approaches. Three, because two is a coincidence.
REPEATED_TEST_TOUCHES = 3

#: Tick volume this many times the recent median counts as expansion. A placeholder, and a
#: deliberately unfussy one: the median is over one session (96 M5 bars), so 1.5x is "clearly
#: busier than this morning" rather than a measured threshold.
VOLUME_EXPANSION_RATIO = 1.5

#: Every family, for an observation that is not about any particular structure.
ALL_FAMILIES: frozenset[SetupFamily] = frozenset(SetupFamily)


@dataclass(frozen=True)
class WatchObservation:
    """One thing noticed, and which setups it belongs on.

    ``families`` rather than "every open setup": an EMA-gap observation on a liquidity reversal is
    noise, and a history padded with irrelevant rows is a history nobody reads. ``anchor_price``
    narrows it further — a proximity observation belongs to the setup anchored at THAT level and
    not to one anchored twenty points away.
    """

    event_type: SetupEventType
    families: frozenset[SetupFamily]
    reason: str
    anchor_price: float | None = None
    detail: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.event_type.is_watch_event:
            # A lifecycle event here would change a setup's state through a path that was never
            # meant to, and the model's validator would reject it at the write -- later, and
            # further from the mistake.
            raise ValueError(
                f"{self.event_type.value} is a lifecycle event, not a watch observation"
            )


def observations(inputs: object, tuning: SetupTuning) -> list[WatchObservation]:
    """Everything worth noticing at this closed candle.

    Takes the setup engine's own ``SetupInputs`` -- duck-typed rather than imported, because
    ``aureon.services.setup_engine`` imports this module and a cycle would only appear on an import
    ordering nobody controls. The attributes used are exactly the ones that module documents.
    """
    found: list[WatchObservation] = []
    found.extend(_proximity(inputs, tuning))
    found.extend(_profile(inputs, tuning))
    found.extend(_momentum(inputs, tuning))
    found.extend(_volume(inputs, tuning))
    return found


# ── proximity ─────────────────────────────────────────────────────────────────


def _proximity(inputs, tuning: SetupTuning) -> list[WatchObservation]:
    """Price came within reach of something that matters.

    One observation per level, typed by what KIND of level it is. A single
    ``PROXIMITY_LEVEL`` event with the type in a detail field would have been shorter and would
    make "how often does a previous-day extreme matter" a question requiring a scan of details
    rather than a group-by.
    """
    found: list[WatchObservation] = []
    for level_type, level in getattr(inputs.levels, "levels", {}).items():
        if not inputs.near(level.price, atr_multiple=tuning.proximity_atr):
            continue
        if level_type in PREV_DAY_LEVELS:
            event = SetupEventType.PROXIMITY_PREV_DAY_EXTREME
        elif level_type in SESSION_LEVELS:
            event = SetupEventType.PROXIMITY_SESSION_EXTREME
        else:
            event = SetupEventType.PROXIMITY_LIQUIDITY_LEVEL
        found.append(
            WatchObservation(
                event,
                frozenset({SetupFamily.LIQUIDITY_REVERSAL, SetupFamily.BREAKOUT_ACCEPTANCE}),
                f"within {tuning.proximity_atr} ATR of {level_type}",
                anchor_price=level.price,
                detail={"level_type": level_type, "level": f"{level.price:.5f}"},
            )
        )
    return found


def _profile(inputs, tuning: SetupTuning) -> list[WatchObservation]:
    """Price came within reach of a volume-profile node, or crossed one.

    ``PROFILE_RECLAIM`` and ``PROFILE_REJECTION`` are about the value area as a whole: price was
    outside it and closed back in, or reached it and closed back out. Both read off
    ``price_vs_va``, which the profile already computes -- deriving it again here would be a second
    definition of "inside".
    """
    found: list[WatchObservation] = []
    nodes = (
        (SetupEventType.PROXIMITY_POC, inputs.poc_price, "POC"),
        (SetupEventType.PROXIMITY_VAH, inputs.value_area_high, "VAH"),
        (SetupEventType.PROXIMITY_VAL, inputs.value_area_low, "VAL"),
    )
    for event, price, label in nodes:
        if price is None or not inputs.near(price, atr_multiple=tuning.proximity_atr):
            continue
        found.append(
            WatchObservation(
                event,
                frozenset({SetupFamily.BREAKOUT_ACCEPTANCE, SetupFamily.LIQUIDITY_REVERSAL}),
                f"within {tuning.proximity_atr} ATR of the {label}",
                anchor_price=price,
                detail={"node": label, "price": f"{price:.5f}"},
            )
        )

    if inputs.price_vs_va == "inside" and inputs.value_area_high is not None:
        found.append(
            WatchObservation(
                SetupEventType.PROFILE_RECLAIM,
                frozenset({SetupFamily.BREAKOUT_ACCEPTANCE}),
                "closed back inside the value area",
            )
        )
    elif inputs.price_vs_va in {"above", "below"} and inputs.value_area_high is not None:
        found.append(
            WatchObservation(
                SetupEventType.PROFILE_REJECTION,
                frozenset({SetupFamily.BREAKOUT_ACCEPTANCE}),
                f"closed {inputs.price_vs_va} the value area",
                detail={"price_vs_va": str(inputs.price_vs_va)},
            )
        )
    return found


# ── momentum ──────────────────────────────────────────────────────────────────


def _momentum(inputs, tuning: SetupTuning) -> list[WatchObservation]:
    found: list[WatchObservation] = []
    fast, slow, atr = inputs.ema_fast, inputs.ema_slow, inputs.atr
    if fast is not None and slow is not None and atr:
        gap = abs(fast - slow)
        if gap <= tuning.ema_gap_atr * atr:
            found.append(
                WatchObservation(
                    SetupEventType.EMA_GAP_NARROWING,
                    frozenset({SetupFamily.MOMENTUM_TRANSITION, SetupFamily.TREND_PULLBACK}),
                    f"the EMA pair is within {tuning.ema_gap_atr} ATR",
                    detail={"gap": f"{gap:.5f}", "atr": f"{atr:.5f}"},
                )
            )

    previous_fast = inputs.previous_ema_fast
    previous_slow = inputs.previous_ema_slow
    if None not in (fast, slow, previous_fast, previous_slow):
        was = previous_fast - previous_slow
        now = fast - slow
        if (was < 0) != (now < 0):
            found.append(
                WatchObservation(
                    SetupEventType.EMA_FAST_SLOPE_CHANGE,
                    frozenset({SetupFamily.MOMENTUM_TRANSITION}),
                    "the fast EMA crossed to the other side of the slow one",
                )
            )

    rsi, previous_rsi = inputs.rsi, inputs.previous_rsi
    if rsi is not None and previous_rsi is not None:
        if (rsi - 50.0) * (previous_rsi - 50.0) < 0:
            found.append(
                WatchObservation(
                    SetupEventType.RSI_MOMENTUM_TURN,
                    frozenset({SetupFamily.MOMENTUM_TRANSITION, SetupFamily.TREND_PULLBACK}),
                    "RSI crossed 50",
                    detail={"rsi": f"{rsi:.2f}", "was": f"{previous_rsi:.2f}"},
                )
            )
    return found


# ── tick volume ───────────────────────────────────────────────────────────────


def _volume(inputs, tuning: SetupTuning) -> list[WatchObservation]:
    """Observations about the number of PRICE CHANGES in the bar.

    Not contracts traded, not aggression, not resting size. MT5's ``tick_volume`` is a count of
    quote updates, and every name here is chosen so that reading it back in a year does not
    suggest otherwise.
    """
    median = inputs.median_tick_volume
    if not median:
        return []
    ratio = inputs.candle.tick_volume / median
    if ratio < VOLUME_EXPANSION_RATIO:
        return []

    detail = {
        "tick_volume": str(inputs.candle.tick_volume),
        "median_tick_volume": f"{median:.1f}",
        "ratio": f"{ratio:.2f}",
    }
    found = [
        WatchObservation(
            SetupEventType.TICK_VOLUME_EXPANSION,
            ALL_FAMILIES,
            f"tick volume is {ratio:.1f}x the recent median",
            detail=detail,
        )
    ]

    near_level = next(
        (
            level.price
            for level in getattr(inputs.levels, "levels", {}).values()
            if inputs.near(level.price, atr_multiple=tuning.proximity_atr)
        ),
        None,
    )
    if near_level is not None:
        found.append(
            WatchObservation(
                SetupEventType.VOLUME_EXPANSION_AT_LEVEL,
                frozenset({SetupFamily.LIQUIDITY_REVERSAL, SetupFamily.BREAKOUT_ACCEPTANCE}),
                "tick volume expanded while price was at a level",
                anchor_price=near_level,
                detail=detail,
            )
        )
        swept = _swept(inputs, near_level, tuning)
        if swept is not None:
            found.append(swept)
    return found


def _swept(inputs, level: float, tuning: SetupTuning) -> WatchObservation | None:
    """A bar that reached through a level on expanding tick volume, and where it closed.

    Two event types, because the two are different observations: a SWEEP went through and stayed
    through at the close, a REJECTION went through and came back. Neither is a detection -- the
    liquidity agent decides what a sweep IS, with its own thresholds and its own version. This is
    "that happened on a busy bar", recorded beside it.
    """
    candle = inputs.candle
    through_high = candle.high > level and candle.low < level
    if not through_high:
        return None
    closed_beyond = candle.close > level
    if closed_beyond:
        return WatchObservation(
            SetupEventType.HIGH_TICK_VOLUME_SWEEP,
            frozenset({SetupFamily.LIQUIDITY_REVERSAL, SetupFamily.BREAKOUT_ACCEPTANCE}),
            "reached through the level on expanding tick volume and closed beyond it",
            anchor_price=level,
        )
    return WatchObservation(
        SetupEventType.HIGH_TICK_VOLUME_REJECTION,
        frozenset({SetupFamily.LIQUIDITY_REVERSAL}),
        "reached through the level on expanding tick volume and closed back inside",
        anchor_price=level,
    )


# ── the two that need memory ──────────────────────────────────────────────────


class LevelTouchCounter:
    """How many times each level has been reached today, for ``REPEATED_LEVEL_TEST``.

    Stateful, and therefore not in ``observations`` above: that function is pure and is tested as
    one. The counter is held by the setup engine, reset on the broker-day rollover for the reason
    everything else is -- a count spanning two days answers a question about neither.
    """

    def __init__(self, *, touches: int = REPEATED_TEST_TOUCHES) -> None:
        self.touches = touches
        self._counts: dict[str, int] = {}
        self._announced: set[str] = set()
        #: The last candle close this counter counted. Everything else in the setup path is
        #: idempotent through a deterministic document id, and this is the one piece of state
        #: that is not -- so it needs its own guard.
        #:
        #: The bug this fixes was found by the test that re-feeds a candle: a restart mid-session
        #: replays the last bar, the counter counted the same touch twice, and on the third
        #: replay it crossed the threshold and wrote a REPEATED_LEVEL_TEST event that the first
        #: pass had not written. So "re-processing a candle changes nothing" was false, in a way
        #: visible only as an extra row in one setup's history.
        self._counted_at = None

    def reset(self) -> None:
        self._counts.clear()
        self._announced.clear()
        self._counted_at = None

    def observe(self, inputs, tuning: SetupTuning) -> list[WatchObservation]:
        at = inputs.candle.close_time
        if self._counted_at is not None and at <= self._counted_at:
            return []
        self._counted_at = at
        found: list[WatchObservation] = []
        for level_type, level in getattr(inputs.levels, "levels", {}).items():
            if not inputs.near(level.price, atr_multiple=tuning.proximity_atr):
                continue
            self._counts[level_type] = self._counts.get(level_type, 0) + 1
            if (
                self._counts[level_type] >= self.touches
                and level_type not in self._announced
            ):
                # Once, not on every touch after the third. A repeated observation is the
                # failure the ops register exists to avoid, and a setup's history has the same
                # reader.
                self._announced.add(level_type)
                found.append(
                    WatchObservation(
                        SetupEventType.REPEATED_LEVEL_TEST,
                        frozenset(
                            {
                                SetupFamily.LIQUIDITY_REVERSAL,
                                SetupFamily.BREAKOUT_ACCEPTANCE,
                            }
                        ),
                        f"{level_type} has been reached {self._counts[level_type]} times today",
                        anchor_price=level.price,
                        detail={"touches": str(self._counts[level_type])},
                    )
                )
        return found


def breakout_pressure(inputs, tuning: SetupTuning) -> WatchObservation | None:
    """Price is pressing against a level without having broken it.

    The bar's extreme reached beyond the level and its close did not. Distinct from a rejection:
    there is no volume condition, so this is the quiet version -- price keeps touching and keeps
    failing to close through, which is the thing a human watching a chart would call pressure.
    """
    candle = inputs.candle
    for level_type, level in getattr(inputs.levels, "levels", {}).items():
        price = level.price
        # The close has to be on the same side as the OPEN, with the extreme on the other: that
        # is what "poked through and came back" means. A first version tested only the extreme
        # against the close, which called an ordinary upward break "pressure" -- the bar opened
        # below the level, closed above it, and satisfied the downward rule by accident.
        pressed_up = candle.open < price < candle.high and candle.close <= price
        pressed_down = candle.low < price < candle.open and candle.close >= price
        if not (pressed_up or pressed_down):
            continue
        if not inputs.near(level.price, atr_multiple=tuning.proximity_atr):
            continue
        return WatchObservation(
            SetupEventType.BREAKOUT_PRESSURE,
            frozenset({SetupFamily.BREAKOUT_ACCEPTANCE}),
            f"reached {level_type} and closed short of it",
            anchor_price=level.price,
            detail={"level_type": level_type},
        )
    return None
