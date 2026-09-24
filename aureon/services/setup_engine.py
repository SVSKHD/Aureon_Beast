"""Tracking four structures across candles, and never trading one (12, T-7).

The agents answer "what happened at this candle". This answers "what is being built", which is a
different question and needs a different kind of object: one that remembers, changes state, and
can be wrong about a sequence rather than about a bar.

## What it is allowed to touch

It runs inside the observer, after the agents and the context, on each closed candle. It reads
detections, levels, indicators and the profile; it writes setups and setup events through the
repository. It imports nothing from ``aureon.execution`` and has no broker handle, and the
boundary suite enforces both -- ``services`` is on the observation side. It never mutates a
detection: a setup REFERENCES the detection that advanced it, one way, because detections are
immutable (CLAUDE.md).

**It emits no orders and no recommendations.** A setup reaching CONFIRMED posts a card in Discord
and nothing else happens. The thresholds below decide what is worth *watching*, never what is
worth doing.

## The four families, and why exactly four

Each is a shape with a beginning, a middle and a way of being wrong -- which is what makes it
measurable. They are closed for the same reason the ops register's names are: an open vocabulary
grows near-duplicates that no review can group by.

* ``LIQUIDITY_REVERSAL`` -- price sweeps a level and comes back. The failure is a close beyond
  the sweep extreme.
* ``BREAKOUT_ACCEPTANCE`` -- price breaks a level and stays. The failure is a close back inside,
  which is FAKEOUT_RISK first and INVALIDATED only if it holds.
* ``TREND_PULLBACK`` -- an established trend pulls back into its EMA zone and resumes. The
  failure is a close through the slow EMA against the trend.
* ``MOMENTUM_TRANSITION`` -- the EMA pair narrows, the fast slope turns, the cross lands. The
  failure is the gap widening again without one.

## One open setup per (symbol, family, direction, anchor)

That tuple is the id (T-6), so this is not a rule the engine enforces separately -- it is what
the id means. A new anchor opens a new setup; the same anchor re-derives the same id and finds
the setup already there. The consequence worth stating: a level tested twice in one day is ONE
setup with two histories of events, not two setups, and the ``market_date`` component is what
keeps it from being one setup across a week.

## Everything here is a placeholder

No real session has produced a setup. The distances are in ATR multiples rather than points --
the one deliberate choice -- because a distance in points is different money per instrument
(decision 141) and a distance in ATR is the same market distance on both. Read
``aureon/config/symbol_tuning.py``'s setup section before believing any number.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aureon.config.symbol_tuning import SetupTuning, setup_tuning
from aureon.engine.levels import HIGH_LEVELS, Levels
from aureon.engine.watch_events import LevelTouchCounter
from aureon.models.base import MarketTime, to_utc, utc_now
from aureon.models.detection import Detection
from aureon.models.enums import (
    DirectionContext,
    MtfAlignment,
    SessionName,
    SetupAnchorKind,
    SetupEventType,
    SetupFamily,
    SetupState,
    Timeframe,
    TrendBias,
)
from aureon.models.identity import price_bin, setup_event_id, setup_id
from aureon.models.market import Candle
from aureon.models.setup import Setup, SetupAnchor, SetupContextSummary, SetupEvent
from aureon.services.agent_confluence import build_agent_confluence

log = logging.getLogger(__name__)

#: How many bins a price is divided into for the id, expressed in ATR rather than in points --
#: the same reasoning as every other distance here. A tenth of an ATR is fine enough that two
#: genuinely different levels never collide and coarse enough that one level tested twice does
#: not become two setups.
ANCHOR_BIN_ATR = 0.10

#: The fallback bin when ATR is unknown (too little history). A fixed multiple of the symbol's
#: own tick, so it is still instrument-relative; setups opened in this window are rare, because
#: nothing else works without ATR either.
ANCHOR_BIN_POINTS = 50.0


@dataclass(frozen=True)
class SetupInputs:
    """Everything one closed candle offers the engine.

    Assembled by the caller rather than fetched here, which is what makes every lifecycle in this
    module testable from a scripted list of candles with no Firestore, no MT5 and no engine. The
    observer builds it from what it already computed for the detections.
    """

    candle: Candle
    market_date: str
    session: SessionName
    detections: tuple[Detection, ...] = ()
    levels: Levels = field(default_factory=Levels)
    atr: float | None = None
    ema_fast: float | None = None
    ema_slow: float | None = None
    previous_ema_fast: float | None = None
    previous_ema_slow: float | None = None
    rsi: float | None = None
    previous_rsi: float | None = None
    trend: TrendBias = TrendBias.SIDEWAYS
    mtf_alignment: MtfAlignment = MtfAlignment.MIXED
    volatility_regime: str | None = None
    price_vs_va: str | None = None
    value_area_high: float | None = None
    value_area_low: float | None = None
    poc_price: float | None = None
    #: Median tick volume over the recent window, for "expansion" to mean something relative.
    median_tick_volume: float | None = None

    @property
    def price(self) -> float:
        return self.candle.close

    @property
    def context(self) -> SetupContextSummary:
        return SetupContextSummary(
            mtf_alignment=self.mtf_alignment,
            volatility_regime=self.volatility_regime,
            price_vs_va=self.price_vs_va,
            session=self.session,
        )

    def near(self, level: float, *, atr_multiple: float) -> bool:
        """Whether this candle came within ``atr_multiple`` ATRs of a price.

        ``False`` when ATR is unknown rather than falling back to a points distance: a proximity
        test with no sense of scale is a test that fires constantly on one instrument and never
        on the other, which is worse than not firing at all.
        """
        if self.atr is None or self.atr <= 0:
            return False
        distance = min(
            abs(self.candle.high - level),
            abs(self.candle.low - level),
            abs(self.candle.close - level),
        )
        return distance <= atr_multiple * self.atr

    def volume_expanded(self) -> bool:
        """Tick volume above the recent median. NOT contracts traded -- MT5 reports the number
        of price CHANGES in the bar, and calling it anything else would be a claim this system
        cannot support."""
        if not self.median_tick_volume:
            return False
        return self.candle.tick_volume > self.median_tick_volume


@dataclass(frozen=True)
class Advance:
    """One state change a family proposes, before the repository has accepted it."""

    to_state: SetupState
    event_type: SetupEventType
    reason: str | None = None
    linked_detection_id: str | None = None
    invalidation_price: float | None = None


@dataclass(frozen=True)
class Opening:
    """A setup a family proposes to open."""

    family: SetupFamily
    direction: DirectionContext
    anchor: SetupAnchor
    invalidation_price: float | None = None


# ── small shared predicates ───────────────────────────────────────────────────


def _bullish(direction: DirectionContext) -> bool:
    return direction is DirectionContext.BULLISH


def _beyond(price: float, level: float, *, above: bool) -> bool:
    return price > level if above else price < level


def _detection_matching(
    inputs: SetupInputs, *, agent: str, contains: str | None = None
) -> Detection | None:
    """The first detection from ``agent`` at this candle, optionally matching its event key.

    Matched on the agent NAME and the event key rather than on a detection type, because the
    event key is the stored contract (the liquidity and breakout agents emit
    ``{direction}|{level_type}``) and a parallel taxonomy here would drift from it.
    """
    for detection in inputs.detections:
        if detection.agent_name != agent:
            continue
        if contains is not None and contains not in detection.event_key:
            continue
        return detection
    return None


def _anchor_bin(price: float, inputs: SetupInputs, *, point: float) -> str:
    size = (
        ANCHOR_BIN_ATR * inputs.atr
        if inputs.atr and inputs.atr > 0
        else ANCHOR_BIN_POINTS * point
    )
    return price_bin(price, bin_size=size)


# ── the families ──────────────────────────────────────────────────────────────


class Family:
    """One structure's rules. Subclasses answer two questions and nothing else."""

    family: SetupFamily

    #: True when the family's anchor MOVES, so only one setup per direction may be open at a
    #: time. The two EMA-anchored families set it, and the reason is a bug this flag exists
    #: because of: ``openings`` runs on every candle with a live EMA read, so a drifting EMA
    #: crosses an anchor bin boundary and opens a SECOND setup -- then a third, for as long as
    #: the trend lasts. The first version of this module did exactly that, and the test walking
    #: a pullback to confirmation found two setups where it expected one.
    #:
    #: Fixing it by freezing the anchor at open time would not work: ``openings`` has no memory,
    #: and the id is derived from what it returns. Fixing it by binning more coarsely only moves
    #: the boundary. The honest answer is that "the trend" is not a price, so its identity is the
    #: DIRECTION and the day -- one per direction, which is what this flag says.
    one_per_direction: bool = False

    def openings(self, inputs: SetupInputs, tuning: SetupTuning) -> list[Opening]:
        raise NotImplementedError

    def advance(
        self, setup: Setup, inputs: SetupInputs, tuning: SetupTuning
    ) -> Advance | None:
        raise NotImplementedError

    # Shared by three of the four: after CONFIRMED, a pullback and a continuation look the same
    # whatever built the structure. Factored here rather than copied, because the one place they
    # differ (TREND_PULLBACK, whose pullback IS its middle) is easier to see as an override.
    def _after_confirmation(
        self, setup: Setup, inputs: SetupInputs, tuning: SetupTuning
    ) -> Advance | None:
        above = _bullish(setup.direction_context)
        anchor = setup.anchor.price

        if setup.state is SetupState.CONFIRMED:
            if inputs.near(anchor, atr_multiple=tuning.proximity_atr):
                return Advance(
                    SetupState.PULLBACK,
                    SetupEventType.PULLBACK_STARTED,
                    reason="price returned to the anchor after confirmation",
                )
            if _beyond(inputs.price, anchor, above=above) and inputs.volume_expanded():
                return Advance(
                    SetupState.CONTINUATION,
                    SetupEventType.CONTINUATION,
                    reason="extended beyond the anchor on expanding tick volume",
                )
            return None

        if setup.state is SetupState.PULLBACK:
            if inputs.ema_fast is not None and inputs.near(
                inputs.ema_fast, atr_multiple=tuning.proximity_atr
            ):
                return Advance(
                    SetupState.PULLBACK,
                    SetupEventType.PULLBACK_TO_EMA20,
                    reason="the pullback reached the fast EMA",
                )
            if _beyond(inputs.price, anchor, above=above):
                return Advance(
                    SetupState.CONTINUATION,
                    SetupEventType.CONTINUATION,
                    reason="resumed beyond the anchor",
                )
            return None

        if setup.state is SetupState.CONTINUATION and inputs.near(
            anchor, atr_multiple=tuning.proximity_atr
        ):
            return Advance(
                SetupState.PULLBACK,
                SetupEventType.PULLBACK_STARTED,
                reason="returned to the anchor again",
            )
        return None


class LiquidityReversal(Family):
    """A level is swept and price comes back through it.

    The sequence: near the level → a sweep detection at it → a reclaim (a close back on the
    original side, or a rejection wick) → confirmation (an EMA cross, or a break of the reaction
    swing) → pullback or continuation.

    Invalidation is a **close** beyond the sweep extreme, not a touch of it. A wick past the
    extreme is what a sweep is; requiring a close is what distinguishes the structure failing
    from the structure happening.
    """

    family = SetupFamily.LIQUIDITY_REVERSAL

    def openings(self, inputs: SetupInputs, tuning: SetupTuning) -> list[Opening]:
        found: list[Opening] = []
        for level_type, level in inputs.levels.levels.items():
            if not inputs.near(level.price, atr_multiple=tuning.proximity_atr):
                continue
            # A HIGH swept from below reverses DOWNWARDS, and vice versa. The direction is the
            # reversal's, not the approach's -- naming it after the approach is the mistake that
            # makes every one of these setups point the wrong way.
            above = level_type in HIGH_LEVELS
            found.append(
                Opening(
                    self.family,
                    DirectionContext.BEARISH if above else DirectionContext.BULLISH,
                    SetupAnchor(
                        kind=SetupAnchorKind.LIQUIDITY_LEVEL,
                        price=level.price,
                        level_type=level_type,
                    ),
                )
            )
        return found

    def advance(
        self, setup: Setup, inputs: SetupInputs, tuning: SetupTuning
    ) -> Advance | None:
        anchor = setup.anchor.price
        # The sweep goes AGAINST the setup's direction: a bearish reversal sweeps upward.
        swept_above = not _bullish(setup.direction_context)

        if setup.state is SetupState.OBSERVING:
            sweep = _detection_matching(
                inputs, agent="liquidity", contains=setup.anchor.level_type or ""
            )
            if sweep is not None:
                extreme = inputs.candle.high if swept_above else inputs.candle.low
                return Advance(
                    SetupState.WATCH,
                    SetupEventType.WATCH_STARTED,
                    reason="the level was swept",
                    linked_detection_id=sweep.detection_id,
                    invalidation_price=extreme,
                )
            return None

        if setup.state is SetupState.WATCH:
            reclaimed = not _beyond(inputs.price, anchor, above=swept_above)
            wick = _detection_matching(inputs, agent="wick")
            if reclaimed or wick is not None:
                return Advance(
                    SetupState.DEVELOPING,
                    SetupEventType.DEVELOPING,
                    reason="reclaimed the level" if reclaimed else "rejection wick",
                    linked_detection_id=wick.detection_id if wick else None,
                )
            return None

        if setup.state is SetupState.DEVELOPING:
            cross = _detection_matching(inputs, agent="ema_cross")
            if cross is not None and _agrees(cross, setup.direction_context):
                return Advance(
                    SetupState.CONFIRMED,
                    SetupEventType.CONFIRMED,
                    reason="an EMA cross in the reversal's direction",
                    linked_detection_id=cross.detection_id,
                )
            return None

        return self._after_confirmation(setup, inputs, tuning)


class BreakoutAcceptance(Family):
    """A level is broken and price stays beyond it.

    Acceptance needs ``acceptance_closes`` closes beyond, not one: one close beyond a level is
    the definition of the break itself, so accepting on it would make acceptance and breakout the
    same event and the family would have no middle.

    A close back inside is FAKEOUT_RISK, not INVALIDATED. The distinction is the whole reason
    that state exists -- a setup that wobbles and recovers is a different measurement from one
    that failed, and collapsing them throws away the difference a review wants.
    """

    family = SetupFamily.BREAKOUT_ACCEPTANCE

    def openings(self, inputs: SetupInputs, tuning: SetupTuning) -> list[Opening]:
        found: list[Opening] = []
        candidates: list[tuple[SetupAnchorKind, str | None, float]] = []
        for level_type, level in inputs.levels.levels.items():
            candidates.append((SetupAnchorKind.SESSION_EXTREME, level_type, level.price))
        if inputs.value_area_high is not None:
            candidates.append((SetupAnchorKind.VALUE_AREA_EDGE, "vah", inputs.value_area_high))
        if inputs.value_area_low is not None:
            candidates.append((SetupAnchorKind.VALUE_AREA_EDGE, "val", inputs.value_area_low))

        for kind, level_type, price in candidates:
            if not inputs.near(price, atr_multiple=tuning.proximity_atr):
                continue
            above = (level_type in HIGH_LEVELS) or level_type == "vah"
            found.append(
                Opening(
                    self.family,
                    # A break of a high continues UPWARDS: the direction is the break's.
                    DirectionContext.BULLISH if above else DirectionContext.BEARISH,
                    SetupAnchor(kind=kind, price=price, level_type=level_type),
                )
            )
        return found

    def advance(
        self, setup: Setup, inputs: SetupInputs, tuning: SetupTuning
    ) -> Advance | None:
        anchor = setup.anchor.price
        above = _bullish(setup.direction_context)
        beyond = _beyond(inputs.price, anchor, above=above)

        if setup.state is SetupState.OBSERVING:
            breakout = _detection_matching(inputs, agent="breakout")
            if breakout is not None or (
                beyond
                and inputs.atr
                and abs(inputs.price - anchor) >= tuning.break_atr * inputs.atr
            ):
                return Advance(
                    SetupState.WATCH,
                    SetupEventType.WATCH_STARTED,
                    reason="closed beyond the level",
                    linked_detection_id=(
                        breakout.detection_id if breakout is not None else None
                    ),
                    invalidation_price=anchor,
                )
            return None

        if setup.state is SetupState.WATCH:
            if not beyond:
                return Advance(
                    SetupState.INVALIDATED,
                    SetupEventType.INVALIDATED,
                    reason="closed back inside before any acceptance",
                )
            return Advance(
                SetupState.DEVELOPING,
                SetupEventType.DEVELOPING,
                reason="a second close beyond the level",
            )

        if setup.state is SetupState.DEVELOPING:
            if not beyond:
                return Advance(
                    SetupState.INVALIDATED,
                    SetupEventType.INVALIDATED,
                    reason="closed back inside before acceptance",
                )
            if inputs.volume_expanded():
                return Advance(
                    SetupState.CONFIRMED,
                    SetupEventType.CONFIRMED,
                    reason="accepted beyond the level on expanding tick volume",
                )
            return None

        if setup.state in (SetupState.CONFIRMED, SetupState.PULLBACK, SetupState.CONTINUATION):
            if not beyond:
                return Advance(
                    SetupState.FAKEOUT_RISK,
                    SetupEventType.FAKEOUT_RISK,
                    reason="closed back inside the level after acceptance",
                )
            return self._after_confirmation(setup, inputs, tuning)

        if setup.state is SetupState.FAKEOUT_RISK:
            if beyond:
                return Advance(
                    SetupState.CONFIRMED,
                    SetupEventType.RECLAIMED,
                    reason="reclaimed the level; the fakeout did not hold",
                )
            return Advance(
                SetupState.INVALIDATED,
                SetupEventType.INVALIDATED,
                reason="a second close back inside; the break failed",
            )
        return None


class TrendPullback(Family):
    """An established trend pulls back into its EMA zone and resumes.

    The only family whose anchor is not a price level: it is the EMA zone, which moves. That is
    why the anchor price is the fast EMA at the moment the setup opened and not a live read --
    an anchor that moved would change the setup's id every candle, and the id is what makes it
    the same setup tomorrow.
    """

    family = SetupFamily.TREND_PULLBACK
    one_per_direction = True

    def openings(self, inputs: SetupInputs, tuning: SetupTuning) -> list[Opening]:
        if inputs.ema_fast is None or inputs.trend is TrendBias.SIDEWAYS:
            return []
        # NOT gated on ``inputs.mtf_alignment``, although an earlier version of this family was
        # (12, T-12). Multi-timeframe alignment is RECORDED, never acted on: whether it predicts
        # anything is a question for the evaluation rules and nobody has answered it, and a
        # filter built on the assumption that it does would be a threshold nobody researched --
        # invisible, too, because the setups it suppressed would never exist to be counted.
        #
        # The alignment rides on ``context_summary`` and is a cohort dimension in the reference
        # block, so "did aligned trend pullbacks do better?" is answerable from the record. That
        # is the question the rule preserves; the gate would have destroyed it.
        direction = (
            DirectionContext.BULLISH
            if inputs.trend is TrendBias.BULLISH
            else DirectionContext.BEARISH
        )
        return [
            Opening(
                self.family,
                direction,
                SetupAnchor(kind=SetupAnchorKind.EMA_ZONE, price=inputs.ema_fast),
                invalidation_price=inputs.ema_slow,
            )
        ]

    def advance(
        self, setup: Setup, inputs: SetupInputs, tuning: SetupTuning
    ) -> Advance | None:
        above = _bullish(setup.direction_context)

        if inputs.ema_slow is not None and inputs.atr:
            through = (
                inputs.price < inputs.ema_slow - tuning.ema_break_atr * inputs.atr
                if above
                else inputs.price > inputs.ema_slow + tuning.ema_break_atr * inputs.atr
            )
            if through and setup.state not in (SetupState.OBSERVING,):
                return Advance(
                    SetupState.INVALIDATED,
                    SetupEventType.INVALIDATED,
                    reason="closed through the slow EMA against the trend",
                )

        if setup.state is SetupState.OBSERVING:
            if inputs.ema_fast is not None and inputs.near(
                inputs.ema_fast, atr_multiple=tuning.proximity_atr
            ):
                return Advance(
                    SetupState.WATCH,
                    SetupEventType.PULLBACK_TO_EMA20,
                    reason="pulled back to the fast EMA",
                    invalidation_price=inputs.ema_slow,
                )
            return None

        if setup.state is SetupState.WATCH:
            wick = _detection_matching(inputs, agent="wick")
            touched_slow = inputs.ema_slow is not None and inputs.near(
                inputs.ema_slow, atr_multiple=tuning.proximity_atr
            )
            if wick is not None:
                return Advance(
                    SetupState.DEVELOPING,
                    SetupEventType.EMA20_REJECTION,
                    reason="rejected from the fast EMA",
                    linked_detection_id=wick.detection_id,
                )
            if touched_slow:
                return Advance(
                    SetupState.DEVELOPING,
                    SetupEventType.EMA50_TEST,
                    reason="tested the slow EMA",
                )
            return None

        if setup.state is SetupState.DEVELOPING:
            extended = (
                inputs.candle.high > setup.anchor.price
                if above
                else inputs.candle.low < setup.anchor.price
            )
            if extended:
                return Advance(
                    SetupState.CONFIRMED,
                    SetupEventType.CONFIRMED,
                    reason="a new extreme in the trend's direction",
                )
            return None

        return self._after_confirmation(setup, inputs, tuning)


class MomentumTransition(Family):
    """The EMA pair narrows, the fast slope turns, and a cross lands.

    The slowest of the four to set up, which is why its expiry is the longest. Its failure is
    quiet: the gap widens again with no cross, and the setup expires rather than breaking.
    """

    family = SetupFamily.MOMENTUM_TRANSITION
    one_per_direction = True

    def openings(self, inputs: SetupInputs, tuning: SetupTuning) -> list[Opening]:
        if inputs.ema_fast is None or inputs.ema_slow is None or not inputs.atr:
            return []
        gap = abs(inputs.ema_fast - inputs.ema_slow)
        if gap > tuning.ema_gap_atr * inputs.atr:
            return []
        # NEUTRAL, deliberately. A narrowing gap says a transition may be coming and says
        # nothing about which way; assigning a direction here would be inventing one, and the
        # cross is what resolves it.
        return [
            Opening(
                self.family,
                DirectionContext.NEUTRAL,
                SetupAnchor(kind=SetupAnchorKind.EMA_ZONE, price=inputs.ema_slow),
            )
        ]

    def advance(
        self, setup: Setup, inputs: SetupInputs, tuning: SetupTuning
    ) -> Advance | None:
        if setup.state is SetupState.OBSERVING:
            if _slope_changed(inputs):
                # WATCH_STARTED, not EMA_FAST_SLOPE_CHANGE. The slope change is what was SEEN and
                # is a descriptive event (T-8); the state change is a lifecycle event, and the
                # model refuses to let a descriptive one carry a transition -- which is how this
                # was caught rather than shipped. The observation still gets recorded in its own
                # right by ``watch_events``; the reason line carries it here.
                return Advance(
                    SetupState.WATCH,
                    SetupEventType.WATCH_STARTED,
                    reason="the fast EMA's slope turned",
                )
            return None

        if setup.state is SetupState.WATCH:
            if _rsi_turned(inputs):
                return Advance(
                    SetupState.DEVELOPING,
                    SetupEventType.DEVELOPING,
                    reason="RSI turned with the slope",
                )
            return None

        if setup.state is SetupState.DEVELOPING:
            cross = _detection_matching(inputs, agent="ema_cross")
            if cross is not None:
                return Advance(
                    SetupState.CONFIRMED,
                    SetupEventType.CONFIRMED,
                    reason="the EMA pair crossed",
                    linked_detection_id=cross.detection_id,
                )
            return None

        return self._after_confirmation(setup, inputs, tuning)


def _agrees(detection: Detection, direction: DirectionContext) -> bool:
    """Whether a detection's event key points the same way as a setup.

    Read off the event key, which is the stored contract, rather than off a direction field that
    not every agent has.
    """
    key = detection.event_key.lower()
    if _bullish(direction):
        return "bull" in key or "up" in key
    return "bear" in key or "down" in key


def _slope_changed(inputs: SetupInputs) -> bool:
    if inputs.ema_fast is None or inputs.previous_ema_fast is None:
        return False
    if inputs.previous_ema_slow is None or inputs.ema_slow is None:
        return False
    was = inputs.previous_ema_fast - inputs.previous_ema_slow
    now = inputs.ema_fast - inputs.ema_slow
    return (was < 0) != (now < 0) or abs(now) < abs(was) * 0.5


def _rsi_turned(inputs: SetupInputs) -> bool:
    if inputs.rsi is None or inputs.previous_rsi is None:
        return False
    return (inputs.rsi - 50.0) * (inputs.previous_rsi - 50.0) < 0


FAMILIES: tuple[Family, ...] = (
    LiquidityReversal(),
    BreakoutAcceptance(),
    TrendPullback(),
    MomentumTransition(),
)


# ── the engine ────────────────────────────────────────────────────────────────


class SetupEngine:
    """Runs the families over closed candles and records what changes.

    Holds the open setups in memory between candles, loaded once per broker day. A read per
    candle would be a Firestore round trip inside the observer's poll loop for data that only
    this process writes -- and the cache is safe precisely because of that: one writer.
    """

    def __init__(
        self,
        *,
        account_scope: str,
        symbol: str,
        timeframe: Timeframe,
        repository: Any,
        point: float,
        market_tz: str,
        families: Sequence[Family] = FAMILIES,
        reference: Callable[[Setup], Any] | None = None,
        now: Any = utc_now,
    ) -> None:
        self.account_scope = account_scope
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.repository = repository
        self.point = point
        self.market_tz = market_tz
        self.families = tuple(families)
        #: T-9. Given a setup about to be created, returns the measured historical block to
        #: attach. Optional and never consulted for anything else: the engine's decisions do not
        #: read it, and an engine constructed without one tracks exactly the same setups.
        self.reference = reference
        self._now = now
        self._tracked: dict[str, Setup] = {}
        self._loaded_date: str | None = None
        #: Candles since each setup last advanced, for expiry. In memory rather than on the
        #: document: it is derivable from ``updated_at`` and the timeframe, and a stored counter
        #: would be a second answer to the same question.
        self._idle: dict[str, int] = {}
        #: T-8's one stateful observation: how many times each level has been reached today.
        #: Reset on the broker-day rollover, for the reason everything else is -- a count
        #: spanning two days answers a question about neither.
        self._touches = LevelTouchCounter()

    # ── the loop ──────────────────────────────────────────────────────────────

    def on_closed_candle(self, inputs: SetupInputs) -> list[SetupEvent]:
        """Advance what is open, expire what has gone quiet, then open what is new.

        That order matters. Advancing first means a candle that both completes one setup and
        opens another records the completion against the setup that existed; opening first would
        let a new setup consume the candle that finished its predecessor.
        """
        self._load(inputs.market_date)
        written: list[SetupEvent] = []

        for setup in list(self._tracked.values()):
            if setup.market_date != inputs.market_date:
                continue
            written.extend(self._advance_one(setup, inputs))

        written.extend(self._open_new(inputs))
        written.extend(self._record_observations(inputs))
        return written

    # ── descriptive observations (T-8) ────────────────────────────────────────

    def _record_observations(self, inputs: SetupInputs) -> list[SetupEvent]:
        """Record what was NOTICED, on the setups it is relevant to.

        Last, after the state changes, so an observation about this candle lands beside the
        transition it accompanied rather than before it. The event ids are deterministic over
        (setup, close, type), so an observation that is true on twenty consecutive candles writes
        twenty rows -- one per candle -- and re-processing any of them writes none.

        Relevance is by FAMILY and, where the observation names a level, by the setup's anchor. An
        EMA-gap note on a liquidity reversal is noise, and a history padded with irrelevant rows is
        a history nobody reads.
        """
        from aureon.engine import watch_events

        if not self._tracked:
            return []
        tuning = setup_tuning(self.symbol, SetupFamily.LIQUIDITY_REVERSAL.value)
        try:
            seen = list(watch_events.observations(inputs, tuning))
            seen.extend(self._touches.observe(inputs, tuning))
            pressure = watch_events.breakout_pressure(inputs, tuning)
            if pressure is not None:
                seen.append(pressure)
        except Exception:  # noqa: BLE001 - an observation must not stop the loop
            log.exception("watch_events raised on %s", inputs.candle.open_time.utc)
            return []

        written: list[SetupEvent] = []
        for observation in seen:
            for setup in list(self._tracked.values()):
                if setup.family not in observation.families:
                    continue
                if observation.anchor_price is not None and not self._same_anchor(
                    setup, observation.anchor_price, inputs
                ):
                    continue
                event = self._write_observation(setup, inputs, observation)
                if event is not None:
                    written.append(event)
        return written

    def _same_anchor(
        self, setup: Setup, price: float, inputs: SetupInputs
    ) -> bool:
        """Whether an observation's level is the setup's own anchor.

        Compared through the SAME binning the id uses, so "the level this setup is about" means
        exactly what it means in the id. A raw equality check would fail on a level that had been
        recomputed to a neighbouring tick, and a generous tolerance would attach a note about one
        level to a setup about another.
        """
        return _anchor_bin(price, inputs, point=self.point) == _anchor_bin(
            setup.anchor.price, inputs, point=self.point
        )

    def _write_observation(
        self, setup: Setup, inputs: SetupInputs, observation: object
    ) -> SetupEvent | None:
        snapshot = dict(_snapshot(inputs))
        for key, value in (getattr(observation, "detail", None) or {}).items():
            snapshot.setdefault(key, value)
        return self._write(
            setup,
            inputs,
            Advance(
                # The SAME state: a descriptive event never moves a setup, and the model refuses
                # any other shape.
                setup.state,
                observation.event_type,
                reason=observation.reason,
            ),
            snapshot=snapshot,
        )

    def _advance_one(self, setup: Setup, inputs: SetupInputs) -> list[SetupEvent]:
        family = self._family(setup.family)
        tuning = setup_tuning(self.symbol, setup.family.value)
        if family is None:
            return []

        advance: Advance | None = None
        try:
            advance = family.advance(setup, inputs, tuning)
        except Exception:  # noqa: BLE001 - a family bug must not stop the observer
            log.exception("setup family %s raised on %s", setup.family.value, setup.setup_id)

        if advance is None:
            written = self._maybe_expire(setup, inputs, tuning)
            if not written and setup.setup_id in self._tracked:
                self._refresh_live_context(setup, inputs)
            return written

        self._idle[setup.setup_id] = 0
        event = self._write(setup, inputs, advance)
        return [event] if event is not None else []

    def _refresh_live_context(self, setup: Setup, inputs: SetupInputs) -> None:
        """Keep an open setup's six-agent meter current even when no event fires.

        Confidence is a live read of observer-owned facts, not a lifecycle transition. Creating a
        fake event just to refresh the card would corrupt event_count and the setup history, so
        the repository has a summary-only refresh path for this purpose.
        """
        confluence = build_agent_confluence(
            inputs,
            setup.direction_context,
            previous=setup.agent_confluence,
        )
        try:
            refreshed = self.repository.refresh_live_context(
                setup.setup_id,
                context_summary=inputs.context,
                agent_confluence=confluence,
                now=to_utc(self._now()),
            )
        except Exception:  # noqa: BLE001 - a card refresh must not stop observation
            log.exception("could not refresh live context for setup %s", setup.setup_id)
            return
        self._tracked[refreshed.setup_id] = refreshed

    def _maybe_expire(
        self, setup: Setup, inputs: SetupInputs, tuning: SetupTuning
    ) -> list[SetupEvent]:
        idle = self._idle.get(setup.setup_id, 0) + 1
        self._idle[setup.setup_id] = idle
        if idle < tuning.expiry_candles:
            return []
        event = self._write(
            setup,
            inputs,
            Advance(
                SetupState.INVALIDATED,
                SetupEventType.EXPIRED,
                reason="expired",
            ),
        )
        return [event] if event is not None else []

    def _open_new(self, inputs: SetupInputs) -> list[SetupEvent]:
        written: list[SetupEvent] = []
        for family in self.families:
            tuning = setup_tuning(self.symbol, family.family.value)
            try:
                openings = family.openings(inputs, tuning)
            except Exception:  # noqa: BLE001
                log.exception("setup family %s raised while opening", family.family.value)
                continue
            for opening in openings:
                if family.one_per_direction and self._already_open(
                    opening.family, opening.direction
                ):
                    continue
                event = self._open(opening, inputs, tuning)
                if event is not None:
                    written.append(event)
        return written

    def _already_open(self, family: SetupFamily, direction: DirectionContext) -> bool:
        return any(
            setup.family is family and setup.direction_context is direction
            for setup in self._tracked.values()
        )

    # ── writing ───────────────────────────────────────────────────────────────

    def _open(
        self, opening: Opening, inputs: SetupInputs, tuning: SetupTuning
    ) -> SetupEvent | None:
        identifier = setup_id(
            account_scope=self.account_scope,
            symbol=self.symbol,
            timeframe=self.timeframe.value,
            family=opening.family.value,
            direction_context=opening.direction.value,
            market_date=inputs.market_date,
            anchor_kind=opening.anchor.kind.value,
            anchor_price_bin=_anchor_bin(opening.anchor.price, inputs, point=self.point),
            setup_version=tuning.version,
        )
        if identifier in self._tracked:
            # The same anchor on a later candle. Not a new setup -- that is what the id means.
            return None

        moment = to_utc(self._now())
        setup = Setup(
            setup_id=identifier,
            account_scope=self.account_scope,
            symbol=self.symbol,
            timeframe=self.timeframe,
            family=opening.family,
            direction_context=opening.direction,
            market_date=inputs.market_date,
            anchor=opening.anchor,
            invalidation_price=opening.invalidation_price,
            opened_at=moment,
            context_summary=inputs.context,
            agent_confluence=build_agent_confluence(inputs, opening.direction),
            setup_version=tuning.version,
            params_snapshot=tuning.snapshot(),
        )
        if self.reference is not None:
            # Measured once, at open, and never rewritten -- see ``setup_reference``. A failure
            # here costs the block, not the setup: an unmeasured reference says "nothing measured
            # yet", which is the truth, and observation continues either way.
            try:
                setup = setup.model_copy(update={"reference": self.reference(setup)})
            except Exception:  # noqa: BLE001
                log.exception("could not attach a reference to setup %s", identifier)
        try:
            stored = self.repository.open(setup, now=moment)
        except Exception:  # noqa: BLE001 - a storage failure must not stop observation
            log.exception("could not open setup %s", identifier)
            return None
        self._tracked[stored.setup_id] = stored
        self._idle[stored.setup_id] = 0
        return None

    def _write(
        self,
        setup: Setup,
        inputs: SetupInputs,
        advance: Advance,
        *,
        snapshot: dict[str, str] | None = None,
    ) -> SetupEvent | None:
        moment = to_utc(self._now())
        event = SetupEvent(
            event_id=setup_event_id(
                setup_id=setup.setup_id,
                candle_close=inputs.candle.close_time,
                event_type=advance.event_type.value,
            ),
            setup_id=setup.setup_id,
            event_type=advance.event_type,
            from_state=setup.state,
            to_state=advance.to_state,
            linked_detection_id=advance.linked_detection_id,
            market_time=MarketTime.from_utc(inputs.candle.close_time, self.market_tz),
            context_snapshot=snapshot if snapshot is not None else _snapshot(inputs),
            reason=advance.reason,
        )
        confluence = build_agent_confluence(
            inputs,
            setup.direction_context,
            previous=setup.agent_confluence,
        )
        try:
            moved, stored, applied = self.repository.record(
                event,
                linked_detection_id=advance.linked_detection_id,
                invalidation_price=advance.invalidation_price,
                context_summary=inputs.context,
                agent_confluence=confluence,
                now=moment,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "could not record %s on %s", advance.event_type.value, setup.setup_id
            )
            return None

        if moved.is_terminal:
            self._tracked.pop(moved.setup_id, None)
            self._idle.pop(moved.setup_id, None)
        else:
            self._tracked[moved.setup_id] = moved
        return stored if applied else None

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _family(self, family: SetupFamily) -> Family | None:
        return next((f for f in self.families if f.family is family), None)

    def _load(self, market_date: str) -> None:
        """Load the day's open setups once.

        A new broker day CLEARS the cache rather than merging: a setup's id carries its date, so
        yesterday's are a different population and holding them would let a level from Monday
        advance on Tuesday's candles.
        """
        if self._loaded_date == market_date:
            return
        self._tracked.clear()
        self._idle.clear()
        self._touches.reset()
        self._loaded_date = market_date
        try:
            for setup in self.repository.open_setups(
                symbol=self.symbol, market_date=market_date
            ):
                self._tracked[setup.setup_id] = setup
                self._idle[setup.setup_id] = 0
        except Exception:  # noqa: BLE001
            log.exception("could not load open setups for %s %s", self.symbol, market_date)

    @property
    def tracked(self) -> dict[str, Setup]:
        """The setups this engine is following, for tests and for ``/setups``."""
        return dict(self._tracked)


def _snapshot(inputs: SetupInputs) -> dict[str, str]:
    """A few numbers that were true at this close, flattened and bounded.

    Only values that are not recoverable from the candle itself: an event that stored the OHLC
    again would double the sub-collection for nothing, since the candle is in the archive and in
    ``market_day_frames``.
    """
    values: dict[str, float | str | None] = {
        "close": inputs.candle.close,
        "atr": inputs.atr,
        "ema_fast": inputs.ema_fast,
        "ema_slow": inputs.ema_slow,
        "rsi": inputs.rsi,
        "tick_volume": inputs.candle.tick_volume,
        "trend": inputs.trend.value,
        "mtf_alignment": inputs.mtf_alignment.value,
    }
    return {
        key: (f"{value:.5f}" if isinstance(value, float) else str(value))
        for key, value in values.items()
        if value is not None
    }


def now_for(candle: Candle) -> datetime:
    """The candle's close, for an engine that wants its clock from the data.

    Used by the replay path so that a replayed session stamps the same ``opened_at`` a live one
    did. A wall clock there would make every replayed setup differ from its original in a field
    nobody meant to compare, and the live-vs-replay check would report a difference that is only
    the clock.
    """
    return to_utc(candle.close_time)
