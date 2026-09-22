"""Detection outcomes: rules, horizons and results (§21-§23).

The whole point of this module is to make *"we do not know yet"* a first-class
answer. A horizon is ``PENDING`` until its termination fires, and a PENDING
horizon is excluded from every statistic rather than counted as a miss. Treating
unknown as failure is the single easiest way to make a strategy look worse than it
is -- and treating it as success is the easiest way to make it look better -- so
the type system keeps them apart and Phase 7 may only read
``complete_horizons``.

Rules are frozen once shipped (§21). A threshold change is a NEW ``rule_id``,
never an edit, because editing one would silently redefine what every already-
stored result meant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ConfigDict, Field, model_validator

from aureon.models.base import AureonDocument, AureonModel, UtcDatetime
from aureon.models.enums import (
    DirectionContext,
    HorizonKind,
    HorizonStatus,
    PathClassification,
    ReferencePrice,
    SetupFamily,
    ThresholdUnit,
    Timeframe,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aureon.models.setup import SetupContextSummary


def threshold_key(value: float) -> str:
    """Canonical map key for a threshold (decision 14).

    Firestore map keys must be strings, and ``3`` and ``3.0`` must not become two
    different buckets. Integral values render without a decimal point so the
    common case reads as ``"3"`` rather than ``"3.0"``.
    """
    if value == int(value):
        return str(int(value))
    return repr(float(value))


class Horizon(AureonModel):
    """One measurement window on a detection (§21).

    ``value`` is interpreted by ``kind``: a candle count, a minute count, or
    ignored entirely for the event-driven kinds (session close, day close,
    opposite cross), whose end is defined by something happening rather than by
    elapsed time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: HorizonKind
    value: int | None = Field(
        default=None, description="Candle/minute count; None for event-driven kinds."
    )

    @model_validator(mode="after")
    def _counted_kinds_need_a_value(self) -> Horizon:
        counted = {HorizonKind.CANDLES, HorizonKind.MINUTES}
        if self.kind in counted and (self.value is None or self.value <= 0):
            raise ValueError(f"horizon {self.id}: kind {self.kind} requires a positive value")
        if self.kind not in counted and self.value is not None:
            raise ValueError(
                f"horizon {self.id}: kind {self.kind} is event-driven and takes no value"
            )
        return self


class EvaluationRule(AureonModel):
    """A frozen recipe for evaluating detections (§21).

    Frozen in the literal sense: the model forbids assignment, so a test or a
    caller that tries to retune ``thresholds`` on a shipped rule raises instead of
    quietly changing the meaning of stored results.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    reference_price: ReferencePrice
    horizons: tuple[Horizon, ...]
    thresholds: tuple[float, ...] = Field(
        description="Favourable/adverse distances, in `threshold_unit`."
    )
    threshold_unit: ThresholdUnit = Field(
        default=ThresholdUnit.POINTS,
        description="Whether `thresholds` are broker points or quote-currency price.",
    )
    termination: str = Field(
        default="per_horizon",
        description="How a horizon ends; 'per_horizon' defers to each horizon's kind.",
    )

    @model_validator(mode="after")
    def _ids_unique_and_thresholds_sane(self) -> EvaluationRule:
        ids = [h.id for h in self.horizons]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.rule_id}: duplicate horizon ids in {ids}")
        if not self.horizons:
            raise ValueError(f"{self.rule_id}: a rule needs at least one horizon")
        if not self.thresholds:
            raise ValueError(f"{self.rule_id}: a rule needs at least one threshold")
        if any(t <= 0 for t in self.thresholds):
            raise ValueError(f"{self.rule_id}: thresholds must be positive")
        if list(self.thresholds) != sorted(self.thresholds):
            raise ValueError(f"{self.rule_id}: thresholds must be ascending")
        return self

    @property
    def threshold_keys(self) -> tuple[str, ...]:
        return tuple(threshold_key(t) for t in self.thresholds)

    def thresholds_in_points(self, point: float) -> tuple[float, ...]:
        """The thresholds expressed in points, for a symbol whose tick is ``point``.

        The tracker measures excursions in points, so a PRICE rule is converted here
        rather than at every comparison. ``point`` comes from ``symbol_info.point`` at
        evaluation time -- never a hard-coded 100 -- so the same rule means "$5" on any
        symbol instead of meaning $5 on gold and something else everywhere.
        """
        if point <= 0:
            raise ValueError(f"{self.rule_id}: point must be positive, got {point}")
        if self.threshold_unit is ThresholdUnit.POINTS:
            return self.thresholds
        return tuple(t / point for t in self.thresholds)

    @property
    def definition_hash(self) -> str:
        """A stable digest of everything that changes what this rule MEANS.

        Exists so a frozen rule can be pinned by a test without pinning its prose. A
        shipped rule's results are stored under ``{detection_id}__{rule_id}``, so
        editing its thresholds would silently redefine every number already recorded
        against it -- the digest turns that into a failing test naming the rule.
        """
        import hashlib
        import json

        payload = json.dumps(
            {
                "rule_id": self.rule_id,
                "reference_price": self.reference_price.value,
                "threshold_unit": self.threshold_unit.value,
                "thresholds": list(self.thresholds),
                "termination": self.termination,
                "horizons": [
                    {"id": h.id, "kind": h.kind.value, "value": h.value}
                    for h in self.horizons
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def horizon(self, horizon_id: str) -> Horizon:
        for h in self.horizons:
            if h.id == horizon_id:
                return h
        raise KeyError(f"{self.rule_id}: no horizon {horizon_id!r}")


class HorizonResult(AureonModel):
    """What happened within one horizon -- or that we do not know yet (§22, §23)."""

    horizon_id: str
    status: HorizonStatus = HorizonStatus.PENDING

    future_high: float | None = None
    future_low: float | None = None

    mfe: float | None = Field(default=None, description="Max favourable excursion, points.")
    mfe_at: UtcDatetime | None = None
    mfe_price: float | None = None
    mae: float | None = Field(default=None, description="Max adverse excursion, points.")
    mae_at: UtcDatetime | None = None
    mae_price: float | None = None

    # Decision 14: maps keyed by canonical threshold, not ten named fields, so a
    # rule can define its own thresholds without a schema change.
    reached: dict[str, bool] = Field(default_factory=dict)
    time_to: dict[str, float | None] = Field(
        default_factory=dict, description="Seconds from detection to first reach."
    )

    path: PathClassification = PathClassification.NONE
    path_ambiguous: bool = Field(
        default=False,
        description=(
            "True when the favourable and adverse thresholds were first crossed within "
            "the SAME candle, so their real order is unknowable from candle data. "
            "``path`` still follows the rule in §23, but a review can exclude these "
            "rather than trust an order that was never observed."
        ),
    )
    candles_seen: int = Field(default=0, ge=0)
    completed_at: UtcDatetime | None = None
    invalid_reason: str | None = None

    @model_validator(mode="after")
    def _status_and_payload_agree(self) -> HorizonResult:
        # Decision 13: a COMPLETE horizon must be fully populated. A COMPLETE
        # result with a missing excursion would be counted by the reviews as a
        # real answer while carrying none.
        if self.status is HorizonStatus.COMPLETE:
            missing = [
                name
                for name in ("future_high", "future_low", "mfe", "mae")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    f"horizon {self.horizon_id}: COMPLETE requires {missing} "
                    "(decision 13)"
                )
            if not self.reached:
                raise ValueError(
                    f"horizon {self.horizon_id}: COMPLETE requires reached[] for every threshold"
                )
            if self.completed_at is None:
                raise ValueError(f"horizon {self.horizon_id}: COMPLETE requires completed_at")
        # Decision 13: INVALID is for horizons that CANNOT complete, so it must
        # say why -- otherwise a data gap is indistinguishable from a bug.
        if self.status is HorizonStatus.INVALID and not self.invalid_reason:
            raise ValueError(f"horizon {self.horizon_id}: INVALID requires invalid_reason")
        # reached and time_to must describe the same thresholds.
        if set(self.time_to) - set(self.reached):
            raise ValueError(
                f"horizon {self.horizon_id}: time_to has keys absent from reached"
            )
        return self

    def reached_threshold(self, threshold: float) -> bool | None:
        """Whether a threshold was reached; ``None`` when not yet known.

        The three-valued return is the point: a caller cannot accidentally read
        "unknown" as "no" without noticing.
        """
        if self.status is not HorizonStatus.COMPLETE:
            return None
        return self.reached.get(threshold_key(threshold))


class SetupEvaluation(AureonDocument):
    """What a setup did after it confirmed, under one rule (12, T-7).

    Stored at ``setup_evaluations/{setup_id}__{rule_id}``, and separate from the setup for the
    reason §21 separates a detection from its evaluation: the setup document is edited as it
    advances, and an outcome written onto it would be future information sitting on a record of
    the present.

    The ``horizons`` are the SAME ``HorizonResult`` the detections use, produced by the same
    tracker under the same frozen rule. One definition of an outcome in the system; a second
    measurement here would differ from it on the first gap over a weekend, with no way to tell
    which was right.

    The family, direction and context ride along as a copy rather than as a reference to the
    setup. A review grouping thousands of these must not read thousands of setups to know which
    family each was, and the values are frozen at the moment of confirmation anyway.
    """

    setup_id: str
    rule_id: str
    evaluation_rule_id: str | None = Field(
        default=None, description="Alias of rule_id, for review documents (§84)."
    )
    family: SetupFamily
    direction_context: DirectionContext
    symbol: str
    timeframe: Timeframe
    market_date: str
    setup_version: str
    reference_price: ReferencePrice
    reference_value: float | None = None
    horizons: tuple[HorizonResult, ...] = ()
    context_summary: SetupContextSummary = Field(
        default_factory=lambda: _default_context_summary()
    )
    updated_at: UtcDatetime | None = None


def _default_context_summary():
    from aureon.models.setup import SetupContextSummary as _Summary

    return _Summary()


class DetectionEvaluation(AureonDocument):
    """Outcomes for one detection under one rule (§22).

    Stored at ``detection_evaluations/{detection_id}__{rule_id}``. Separate from
    the detection itself so that detections stay immutable and free of future
    information.
    """

    detection_id: str
    rule_id: str
    evaluation_rule_id: str | None = Field(
        default=None, description="Alias of rule_id, for review documents (§84)."
    )
    reference_price: ReferencePrice
    reference_value: float | None = Field(
        default=None, description="The actual price measured from."
    )
    horizons: tuple[HorizonResult, ...] = ()
    context_tags: dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "What else the machine had seen at this detection's candle close (§19, "
            "§23). Derived ONLY from data available at that close -- see "
            "aureon.evaluation.context_tags. Research grouping, never a gate."
        ),
    )
    updated_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _horizon_ids_unique(self) -> DetectionEvaluation:
        ids = [h.horizon_id for h in self.horizons]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.detection_id}: duplicate horizon results {ids}")
        return self

    @property
    def complete_horizons(self) -> tuple[HorizonResult, ...]:
        """The ONLY accessor review code may use (Phase 3).

        Phase 7 aggregates through this property so a PENDING horizon can never
        be silently counted as a miss. A lint test greps ``aureon/reviews`` for
        direct ``.horizons`` access to keep it that way.
        """
        return tuple(h for h in self.horizons if h.status is HorizonStatus.COMPLETE)

    @property
    def pending_horizons(self) -> tuple[HorizonResult, ...]:
        return tuple(h for h in self.horizons if h.status is HorizonStatus.PENDING)

    @property
    def invalid_horizons(self) -> tuple[HorizonResult, ...]:
        return tuple(h for h in self.horizons if h.status is HorizonStatus.INVALID)

    @property
    def is_fully_evaluated(self) -> bool:
        return not self.pending_horizons
