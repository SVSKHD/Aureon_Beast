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
    """One trading week, generated after Friday's close (§63)."""

    iso_year: int
    iso_week: int = Field(ge=1, le=53)
    daily_review_ids: tuple[str, ...] = ()
