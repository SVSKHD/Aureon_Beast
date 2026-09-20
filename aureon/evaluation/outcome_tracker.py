"""Tracks what happened after a detection (§22, §23).

Answers "did it work?" without ever letting the answer leak backwards into the
detection, and without letting "we do not know yet" masquerade as "no".

## What is measured, and from when

Nothing is measured on the detection's own candle. That candle is already over by the
time it is known to have closed, so a human acting on the detection transacts at the
**next open** at the earliest -- which is what ``EMA_OUTCOME_V1`` measures from. Every
excursion is then computed over candles strictly after the detection.

Excursions are signed relative to the detection's direction, so a BUY and a SELL are
directly comparable:

* **MFE** is the furthest price ran *in the detection's favour*, in points. It can be
  negative, which is informative rather than a bug: it means the best price ever
  offered was still worse than the reference.
* **MAE** is the furthest it ran *against*, and is normally negative.

## Timing is candle-resolution, deliberately

A candle's high and low happened *somewhere inside* it; candle data cannot say when.
So the time a threshold was reached is recorded as that candle's **close** -- the
first moment the crossing is actually known. Using the candle's open would claim
knowledge that did not exist yet, and would make every ``time_to`` optimistic.

The same limit produces a real ambiguity: when the favourable and adverse thresholds
are first crossed within one candle, their order is unknowable. §23 says MAE_FIRST
only when adverse came *before* favourable, so a same-candle crossing falls to
MFE_FIRST -- the optimistic side. Rather than hide that, such results are flagged
``path_ambiguous`` so a review can exclude them instead of trusting an order nobody
observed (decision 49).

## Gaps

A gap larger than two timeframes inside a horizon makes that horizon ``INVALID``, not
``COMPLETE`` (§22). Price moved while nothing was observed, so the excursion figures
would be a lower bound presented as a measurement. The fixture's weekend gap
exercises this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aureon.config.sessions import session_for
from aureon.models.base import to_utc, utc_now
from aureon.models.detection import Detection
from aureon.models.enums import (
    Direction,
    HorizonKind,
    HorizonStatus,
    PathClassification,
    ReferencePrice,
    Timeframe,
    assert_horizon_transition,
)
from aureon.models.evaluation import (
    DetectionEvaluation,
    EvaluationRule,
    Horizon,
    HorizonResult,
)
from aureon.models.market import Candle

# A gap wider than this many timeframes invalidates any horizon spanning it (§22).
DEFAULT_GAP_TOLERANCE_TIMEFRAMES = 2


@dataclass
class _HorizonState:
    """Mutable accumulator for one horizon of one detection."""

    horizon: Horizon
    status: HorizonStatus = HorizonStatus.PENDING
    future_high: float | None = None
    future_low: float | None = None
    mfe: float | None = None
    mfe_at: datetime | None = None
    mfe_price: float | None = None
    mae: float | None = None
    mae_at: datetime | None = None
    mae_price: float | None = None
    reached: dict[str, bool] = field(default_factory=dict)
    time_to: dict[str, float | None] = field(default_factory=dict)
    first_favourable_at: datetime | None = None
    first_adverse_at: datetime | None = None
    candles_seen: int = 0
    completed_at: datetime | None = None
    invalid_reason: str | None = None

    @property
    def open(self) -> bool:
        return self.status is HorizonStatus.PENDING

    def to_result(self) -> HorizonResult:
        path, ambiguous = self._path()
        return HorizonResult(
            horizon_id=self.horizon.id,
            status=self.status,
            future_high=self.future_high,
            future_low=self.future_low,
            mfe=self.mfe,
            mfe_at=self.mfe_at,
            mfe_price=self.mfe_price,
            mae=self.mae,
            mae_at=self.mae_at,
            mae_price=self.mae_price,
            reached=dict(self.reached),
            time_to=dict(self.time_to),
            path=path,
            path_ambiguous=ambiguous,
            candles_seen=self.candles_seen,
            completed_at=self.completed_at,
            invalid_reason=self.invalid_reason,
        )

    def _path(self) -> tuple[PathClassification, bool]:
        favourable, adverse = self.first_favourable_at, self.first_adverse_at
        if favourable is None and adverse is None:
            return PathClassification.NONE, False
        if adverse is not None and (favourable is None or adverse < favourable):
            return PathClassification.MAE_FIRST, False
        # Same candle: the real order is unobservable. §23's "otherwise" puts it in
        # MFE_FIRST; the flag records that it was not actually seen.
        ambiguous = favourable is not None and adverse is not None and adverse == favourable
        return PathClassification.MFE_FIRST, ambiguous


@dataclass
class _Tracked:
    """Everything being measured for one detection."""

    detection: Detection
    reference_value: float | None = None
    horizons: dict[str, _HorizonState] = field(default_factory=dict)
    last_candle_close: datetime | None = None

    @property
    def open(self) -> bool:
        return any(state.open for state in self.horizons.values())


class OutcomeTracker:
    """Evaluates detections against a frozen rule as candles arrive (§22).

    Stateful by necessity -- a horizon spans many candles -- but deterministic: the
    same detections and candles in the same order always produce the same evaluations,
    which is what lets the backfill reproduce live results exactly.
    """

    def __init__(
        self,
        rule: EvaluationRule,
        *,
        market_tz: str,
        point: float = 0.01,
        gap_tolerance_timeframes: int = DEFAULT_GAP_TOLERANCE_TIMEFRAMES,
    ) -> None:
        if point <= 0:
            raise ValueError("point must be positive")
        self.rule = rule
        self.market_tz = market_tz
        self.point = point
        # A PRICE rule's thresholds are converted to points ONCE, here, using the
        # symbol's own point from symbol_info -- never a hard-coded multiplier. The
        # excursion maths below stays in points, so nothing downstream has to know
        # which unit the rule was written in (§21).
        self._thresholds_points = self.rule.thresholds_in_points(point)
        #: threshold KEY -> the same threshold in points. The key is derived from the
        #: rule's own value (3 stays "3" whether it means $3 or 3 points), so stored
        #: results stay readable in the unit the rule was written in.
        self._threshold_points_by_key = dict(
            zip(self.rule.threshold_keys, self._thresholds_points, strict=True)
        )
        self.gap_tolerance_timeframes = gap_tolerance_timeframes
        self._tracked: dict[str, _Tracked] = {}

    # ── Intake ────────────────────────────────────────────────────────────────

    def track(self, detection: Detection) -> DetectionEvaluation | None:
        """Begin evaluating a detection. Returns ``None`` if it is not evaluable.

        Context-only detections (``direction is None``) are skipped: without a
        direction there is no favourable side, so "did it work?" has no meaning
        (decision 48). They are still stored as detections and still available to the
        reviews as context.
        """
        if detection.direction is None:
            return None
        if detection.detection_id in self._tracked:
            return self._evaluation(self._tracked[detection.detection_id])

        tracked = _Tracked(
            detection=detection,
            horizons={h.id: _HorizonState(horizon=h) for h in self.rule.horizons},
        )
        for state in tracked.horizons.values():
            for key in self.rule.threshold_keys:
                state.reached[key] = False
                state.time_to[key] = None
        self._tracked[detection.detection_id] = tracked
        return self._evaluation(tracked)

    def on_detection(self, detection: Detection) -> list[DetectionEvaluation]:
        """Feed a new detection, completing any ``opposite_cross`` horizons it ends.

        An opposite-direction detection from the same agent on the same stream is what
        the ``opposite_cross`` horizon waits for (§21).
        """
        updated: list[DetectionEvaluation] = []
        for tracked in self._tracked.values():
            if not tracked.open or tracked.reference_value is None:
                continue
            origin = tracked.detection
            if (
                detection.agent_name != origin.agent_name
                or detection.symbol != origin.symbol
                or detection.timeframe != origin.timeframe
                or detection.direction is None
                or detection.direction is origin.direction
                or detection.detected_at.utc <= origin.detected_at.utc
            ):
                continue
            changed = False
            for state in tracked.horizons.values():
                if state.open and state.horizon.kind is HorizonKind.OPPOSITE_CROSS:
                    self._complete(state, at=detection.detected_at.utc)
                    changed = True
            if changed:
                updated.append(self._evaluation(tracked))
        return updated

    # ── The candle stream ─────────────────────────────────────────────────────

    def on_closed_candle(self, candle: Candle) -> list[DetectionEvaluation]:
        """Advance every open evaluation for this stream. Returns those that changed."""
        updated: list[DetectionEvaluation] = []
        for tracked in list(self._tracked.values()):
            origin = tracked.detection
            if candle.symbol != origin.symbol or candle.timeframe != origin.timeframe:
                continue
            # Strictly after the detection's own candle: that bar is already history.
            if candle.open_time.utc <= origin.candle_open_time.utc:
                continue
            if not tracked.open:
                continue
            if self._apply(tracked, candle):
                updated.append(self._evaluation(tracked))
        return updated

    def _apply(self, tracked: _Tracked, candle: Candle) -> bool:
        origin = tracked.detection

        if tracked.reference_value is None:
            # The first candle after the detection fixes the reference price.
            tracked.reference_value = (
                candle.open
                if self.rule.reference_price is ReferencePrice.NEXT_OPEN
                else origin.price
            )

        gapped = self._gap_before(tracked, candle, origin.timeframe)
        tracked.last_candle_close = candle.close_time

        reference = tracked.reference_value
        favourable, adverse = self._excursions(origin.direction, reference, candle)
        seen_at = candle.close_time  # candle resolution; see the module docstring

        for state in tracked.horizons.values():
            if not state.open:
                continue
            if gapped:
                self._invalidate(
                    state,
                    f"candle gap larger than {self.gap_tolerance_timeframes} timeframes "
                    f"before {candle.open_time.utc.isoformat()}",
                )
                continue

            state.candles_seen += 1
            self._update_excursions(
                state,
                candle=candle,
                direction=origin.direction,
                favourable=favourable,
                adverse=adverse,
                seen_at=seen_at,
            )
            self._update_thresholds(
                state, tracked, favourable=favourable, adverse=adverse, seen_at=seen_at
            )
            if self._terminates(state, tracked, candle):
                self._complete(state, at=seen_at)
        return True

    def _gap_before(self, tracked: _Tracked, candle: Candle, timeframe: Timeframe) -> bool:
        previous_close = tracked.last_candle_close
        if previous_close is None:
            return False
        gap = (candle.open_time.utc - previous_close).total_seconds()
        return gap > timeframe.seconds * self.gap_tolerance_timeframes

    def _excursions(
        self, direction: Direction, reference: float, candle: Candle
    ) -> tuple[float, float]:
        """Favourable and adverse excursion of one candle, in points.

        Signed by direction so BUY and SELL results are directly comparable.
        """
        if direction is Direction.BUY:
            return (candle.high - reference) / self.point, (candle.low - reference) / self.point
        return (reference - candle.low) / self.point, (reference - candle.high) / self.point

    def _update_excursions(
        self,
        state: _HorizonState,
        *,
        candle: Candle,
        direction: Direction,
        favourable: float,
        adverse: float,
        seen_at: datetime,
    ) -> None:
        # future_high/low are the raw extremes and are direction-independent.
        state.future_high = (
            candle.high if state.future_high is None else max(state.future_high, candle.high)
        )
        state.future_low = (
            candle.low if state.future_low is None else min(state.future_low, candle.low)
        )
        # The PRICES behind the excursions are direction-dependent: for a BUY the
        # favourable extreme is the high and the adverse one the low; for a SELL they
        # are the other way round. Recording the high either way would misreport every
        # SELL evaluation's excursion prices.
        favourable_price = candle.high if direction is Direction.BUY else candle.low
        adverse_price = candle.low if direction is Direction.BUY else candle.high

        if state.mfe is None or favourable > state.mfe:
            state.mfe = favourable
            state.mfe_at = seen_at
            state.mfe_price = favourable_price
        if state.mae is None or adverse < state.mae:
            state.mae = adverse
            state.mae_at = seen_at
            state.mae_price = adverse_price

    def _update_thresholds(
        self,
        state: _HorizonState,
        tracked: _Tracked,
        *,
        favourable: float,
        adverse: float,
        seen_at: datetime,
    ) -> None:
        detected_at = tracked.detection.detected_at.utc
        # `favourable` and `adverse` are in POINTS, so the comparison uses the rule's
        # thresholds converted to points -- identical for a POINTS rule, scaled by the
        # symbol's tick for a PRICE one.
        for key, threshold in self._threshold_points_by_key.items():
            if state.reached.get(key):
                continue
            if favourable >= threshold:
                state.reached[key] = True
                state.time_to[key] = (seen_at - detected_at).total_seconds()

        first = self._thresholds_points[0]
        if state.first_favourable_at is None and favourable >= first:
            state.first_favourable_at = seen_at
        if state.first_adverse_at is None and adverse <= -first:
            state.first_adverse_at = seen_at

    # ── Termination ───────────────────────────────────────────────────────────

    def _terminates(self, state: _HorizonState, tracked: _Tracked, candle: Candle) -> bool:
        horizon = state.horizon
        origin = tracked.detection

        if horizon.kind is HorizonKind.CANDLES:
            return state.candles_seen >= (horizon.value or 0)
        if horizon.kind is HorizonKind.MINUTES:
            elapsed = (candle.close_time - origin.detected_at.utc).total_seconds()
            return elapsed >= (horizon.value or 0) * 60
        if horizon.kind is HorizonKind.SESSION_CLOSE:
            return session_for(candle.open_time.market) is not origin.session.session
        if horizon.kind is HorizonKind.DAY_CLOSE:
            return candle.open_time.market_date != origin.detected_at.market_date
        # OPPOSITE_CROSS is event-driven and ends via on_detection().
        return False

    def _complete(self, state: _HorizonState, *, at: datetime) -> None:
        assert_horizon_transition(state.status, HorizonStatus.COMPLETE)
        state.status = HorizonStatus.COMPLETE
        state.completed_at = to_utc(at)

    def _invalidate(self, state: _HorizonState, reason: str) -> None:
        assert_horizon_transition(state.status, HorizonStatus.INVALID)
        state.status = HorizonStatus.INVALID
        state.invalid_reason = reason

    # ── Output ────────────────────────────────────────────────────────────────

    def _evaluation(self, tracked: _Tracked) -> DetectionEvaluation:
        return DetectionEvaluation(
            detection_id=tracked.detection.detection_id,
            rule_id=self.rule.rule_id,
            evaluation_rule_id=self.rule.rule_id,
            reference_price=self.rule.reference_price,
            reference_value=tracked.reference_value,
            horizons=tuple(
                tracked.horizons[h.id].to_result() for h in self.rule.horizons
            ),
            updated_at=utc_now(),
        )

    def evaluation_for(self, detection_id: str) -> DetectionEvaluation | None:
        tracked = self._tracked.get(detection_id)
        return self._evaluation(tracked) if tracked else None

    def all_evaluations(self) -> list[DetectionEvaluation]:
        return [self._evaluation(t) for t in self._tracked.values()]

    @property
    def open_count(self) -> int:
        return sum(1 for t in self._tracked.values() if t.open)

    @property
    def tracked_count(self) -> int:
        return len(self._tracked)

    def release_closed(self) -> int:
        """Drop fully-resolved evaluations from memory.

        The live observer runs indefinitely, so finished evaluations must not
        accumulate. A released detection is never re-tracked, because
        ``on_closed_candle`` only advances what is still open.
        """
        done = [k for k, v in self._tracked.items() if not v.open]
        for key in done:
            del self._tracked[key]
        return len(done)
