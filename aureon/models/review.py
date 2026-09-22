"""Reviews: machine observation vs human execution, kept separate (§61-§65).

A review is the only document where an *inferred* detection-to-trade link may
live (decision 8). The asymmetry is deliberate. Inferring that a trade was
"probably taken because of" a detection is useful for research and unreliable as
a fact; recording it on the trade would make a guess permanent and would
eventually be read as though a human had stated it. Keeping it on the review
keeps it clearly labelled as analysis, with a confidence attached.

The other rule this module encodes: reached-N counts come only from COMPLETE
horizons, and the number of horizons excluded for being PENDING is reported
alongside them. A reader can then tell "3 of 10 reached" from "3 of 10 reached,
40 still unknown", which are very different claims.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from aureon.models.base import AureonDocument, AureonModel, UtcDatetime
from aureon.models.enums import LinkType, SessionName


class InferredLink(AureonModel):
    """A guessed association between a detection and a trade (§50).

    Legal here and nowhere else. ``confidence`` is mandatory precisely because it
    forces the guess to admit it is one.
    """

    detection_id: str
    trade_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    link_type: LinkType = LinkType.INFERRED
    reason: str | None = Field(
        default=None, description='e.g. "same symbol+direction, opened 4m after".'
    )

    @model_validator(mode="after")
    def _must_stay_inferred(self) -> InferredLink:
        if self.link_type is not LinkType.INFERRED:
            raise ValueError(
                "InferredLink always carries link_type=inferred; an explicit link "
                "belongs on the trade itself"
            )
        return self


class ThresholdOutcome(AureonModel):
    """Reached counts for one threshold, from COMPLETE horizons only (§22)."""

    threshold: float
    reached: int = Field(default=0, ge=0)
    evaluated: int = Field(
        default=0, ge=0, description="COMPLETE horizons contributing to this count."
    )

    @model_validator(mode="after")
    def _reached_within_evaluated(self) -> ThresholdOutcome:
        if self.reached > self.evaluated:
            raise ValueError(
                f"threshold {self.threshold}: reached {self.reached} exceeds "
                f"evaluated {self.evaluated}"
            )
        return self

    @property
    def rate(self) -> float | None:
        """Hit rate, or ``None`` when nothing has completed.

        ``None`` rather than 0.0: a rate of zero claims the threshold was never
        reached, which is a finding. No data is not a finding.
        """
        if self.evaluated == 0:
            return None
        return self.reached / self.evaluated


class HorizonOutcome(AureonModel):
    """Aggregated outcomes for one horizon across a period."""

    horizon_id: str
    thresholds: tuple[ThresholdOutcome, ...] = ()
    mfe_mean: float | None = None
    mae_mean: float | None = None
    mfe_first: int = Field(default=0, ge=0)
    mae_first: int = Field(default=0, ge=0)


class ReviewBase(AureonDocument):
    """Fields common to the daily and weekly reviews (§61, §63).

    Regenerating a review for the same period must overwrite the same document id
    and produce byte-identical content, so reviews stay a pure function of the
    data they aggregate.
    """

    period_start: UtcDatetime
    period_end: UtcDatetime
    market_tz: str
    generated_at: UtcDatetime | None = None

    evaluation_rule_id: str = Field(description="Which frozen rule produced the counts (§84).")

    #: Which symbol this review is about (9A). ``None`` only on a review generated before
    #: reviews were split per symbol; every new one names its symbol.
    #:
    #: Split rather than aggregated because the two halves of a combined review would not
    #: mean the same thing: each symbol has its OWN frozen rule (decision 141), so one
    #: ``evaluation_rule_id`` on a document counting both would be a false statement about
    #: half the numbers, and the horizon tables would add up reached-counts measured against
    #: thresholds in different instruments' money.
    symbol: str | None = None

    detections_total: int = Field(default=0, ge=0)
    detections_by_agent: dict[str, int] = Field(default_factory=dict)
    detections_by_session: dict[SessionName, int] = Field(default_factory=dict)

    horizons: tuple[HorizonOutcome, ...] = ()
    # The honesty field: how much was NOT counted because it is still unknown.
    pending_horizons_excluded: int = Field(default=0, ge=0)
    invalid_horizons_excluded: int = Field(default=0, ge=0)

    trades_total: int = Field(default=0, ge=0)
    trades_closed: int = Field(default=0, ge=0)
    realized_pnl: float = 0.0
    trades_by_session: dict[SessionName, int] = Field(default_factory=dict)

    explicit_links: int = Field(default=0, ge=0)
    inferred_links: tuple[InferredLink, ...] = ()

    notes: str | None = None

    # ── 9D: was the readout any good, and what did the trader say? ────────────
    #: How the quantiles `/monitor` published fared (§63). Counted here rather than written
    #: back onto each assessment, which would make a stored document say something different
    #: from what it said when it was written (decision 195).
    assessments_total: int = Field(default=0, ge=0)
    assessments_hit: int = Field(default=0, ge=0)
    assessments_miss: int = Field(default=0, ge=0)
    #: Neither distance was reached inside the horizon. Its own count: "it went against you"
    #: and "it went nowhere" are different lessons.
    assessments_neither: int = Field(default=0, ge=0)
    #: Both distances were reached and candle data cannot order them (§23's problem again).
    #: Excluded from the rate rather than counted either way.
    assessments_unresolved: int = Field(default=0, ge=0)
    #: Readouts that published no target at all — `/monitor` refusing for want of history.
    #: How often that happens is itself a finding, and one that should improve over time.
    assessments_not_scored: int = Field(default=0, ge=0)
    #: cohort key -> "hit/resolved". A cohort that had to drop the volatility regime is a
    #: weaker claim than an exact one, and a single blended rate would hide which is which.
    assessment_hit_by_cohort: dict[str, str] = Field(default_factory=dict)
    #: history source -> "hit/resolved" (11C, F-9). The merged ``assessment_hit_rate`` above
    #: is an average over incomparable populations until this is read: a readout measured on
    #: replayed fixture bars and one measured on bars a broker served are the same arithmetic
    #: over different worlds. Kept beside the merged rate rather than replacing it, because
    #: the merged one is what a reader will quote and this is what tells them whether to.
    assessment_hit_by_source: dict[str, str] = Field(default_factory=dict)

    #: trade_id -> what the human wrote about it, oldest first (9D). Copied into the review
    #: rather than linked, so the document a person reads next year still says what they
    #: said at the time.
    trade_notes: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    #: `#tag` -> the trades carrying it, for grouping a week by the trader's own vocabulary
    #: rather than by anything Aureon invented.
    trades_by_tag: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @property
    def assessment_history_is_synthetic(self) -> bool:
        """True when every resolved readout this period came from generated bars.

        The state this repository is in, and the one a reader most needs told: a hit rate
        printed without it reads as a measurement of the market.
        """
        resolved = {
            source: row
            for source, row in self.assessment_hit_by_source.items()
            if not row.endswith("/0")
        }
        return set(resolved) == {"synthetic"}

    @property
    def assessment_hit_rate(self) -> float | None:
        """``None`` on nothing resolved, never 0.0.

        A rate of zero says the target never came first. Nothing resolved says nothing at
        all, and rendering them alike is how an absence becomes a verdict.
        """
        resolved = self.assessments_hit + self.assessments_miss + self.assessments_neither
        if resolved == 0:
            return None
        return self.assessments_hit / resolved

    @model_validator(mode="after")
    def _period_ordered(self) -> ReviewBase:
        if self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")
        return self


class DailyReview(ReviewBase):
    """One broker trading day (§61)."""

    market_date: str = Field(description="Broker-local date, YYYY-MM-DD.")
    sessions_covered: tuple[SessionName, ...] = ()


class WeeklyReview(ReviewBase):
    """One trading week, generated after Friday's close (§63).

    ## Why the setups section is weekly and not daily (T-9)

    A setup is a claim about a sequence, and the sequences worth counting do not fit inside a
    broker day: a trend pullback opened on Tuesday confirms on Wednesday and completes on
    Thursday. A daily section would therefore report a week's structures three times, each time
    with a different and equally incomplete answer. Counted once, over a week, the numbers are
    about the same population a reader has been watching.
    """

    iso_year: int
    iso_week: int = Field(ge=1, le=53)
    daily_review_ids: tuple[str, ...] = ()

    # ── T-9: the setups this week noticed, and what they did ──────────────────
    #: Setups OPENED in the period, whatever state they reached. Opened rather than closed, so
    #: a setup belongs to the week a reader was looking at it.
    setups_total: int = Field(default=0, ge=0)
    #: family -> count, and state -> count at the moment the review was generated. A setup still
    #: open appears under its current state, which is the honest answer: "we do not know yet" is
    #: a state, and folding it into COMPLETED or INVALIDATED would invent an outcome.
    setups_by_family: dict[str, int] = Field(default_factory=dict)
    setups_by_state: dict[str, int] = Field(default_factory=dict)
    setups_by_direction_context: dict[str, int] = Field(default_factory=dict)
    #: How many reached CONFIRMED, which is the first state with a claim in it. A setup that
    #: never confirmed is not a wrong prediction; it is an absence of one, and it is excluded
    #: from every rate below rather than counted as a miss.
    setups_confirmed: int = Field(default=0, ge=0)
    #: Confirmed setups with at least one COMPLETE horizon, and those whose horizons are all
    #: still pending. The second is this section's honesty field, the same role
    #: ``pending_horizons_excluded`` plays above.
    setups_evaluated: int = Field(default=0, ge=0)
    setups_unresolved: int = Field(default=0, ge=0)
    #: family -> "reached/evaluated" at the rule's first threshold and the counted horizon.
    #: A string rather than a float for the reason every rate in this document is: a bare
    #: percentage loses its n, and 1/1 and 60/100 render identically.
    setup_reached_by_family: dict[str, str] = Field(default_factory=dict)
    #: family -> the measured excursions, in points, as a one-liner. What actually happened,
    #: beside how often a threshold was reached: a family that reaches its threshold half the
    #: time while giving back twice as much is not the same finding as one that does not.
    setup_excursions_by_family: dict[str, str] = Field(default_factory=dict)
    #: How many of the week's setups carried a reference block built on fewer than
    #: ``MATURE_REAL_DAYS`` verified broker days (11C, T-9). Early on this is all of them, and a
    #: reader who cannot see that will read the reference numbers as measurements of the market.
    setups_with_immature_reference: int = Field(default=0, ge=0)

    @property
    def setup_confirmation_rate(self) -> float | None:
        """Confirmed over opened, or ``None`` on a week with no setups.

        ``None`` rather than 0.0, for the reason ``ThresholdConfirmation.rate`` gives: a rate of
        zero says nothing confirmed, and no setups says nothing at all.
        """
        if self.setups_total == 0:
            return None
        return self.setups_confirmed / self.setups_total
