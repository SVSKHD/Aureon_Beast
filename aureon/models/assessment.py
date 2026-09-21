"""What the measured record says about a detection, and a trader's own notes (9D).

Two documents that exist to keep two very different kinds of statement apart.

``Assessment`` is arithmetic over stored outcomes. Every number in it is a count, a
frequency or a quantile of ``detection_evaluations`` rows that were already complete before
the detection it describes was made -- never a forecast, never a model, never a preference.
Its whole job is to answer "when this shape of thing happened before, what followed?" and to
say how many times "before" was.

``TradeNote`` is the opposite: a human sentence about one trade, stored in its own
collection precisely so it cannot touch the trade. A CLOSED trade refuses every field write
but reconciliation stamps (§45), and a note is not a reconciliation -- so it lives beside
the trade rather than in it, and the weekly review prints the two together.

## Why n, and the interval, ride with every percentage

A reached-rate of 60% from five detections and one from three hundred are different
statements, and rendered as "60%" they are the same words. The Wilson interval is carried
next to the rate for the same reason the ``pending_horizons_excluded`` field exists on a
review: the honest thing to publish is the uncertainty, not the point estimate alone.

## Why the estimates are quantiles and labelled estimates

TP and SL here are ``p50``/``p25`` of the cohort's measured MFE and ``p75``/``p90`` of its
measured MAE. They are descriptions of what already happened to similar detections, not
targets -- so nothing in Aureon ever prefills them onto an order, and the rendering carries
"measured from n=... · not advice" every time (§62-§64).

## Why the assessment is never scored in place

The weekly review asks whether price reached the TP quantile before the SL quantile, and
that answer is a property of the *review*, not of the assessment: re-scoring a stored
assessment would make a document that said one thing when it was written say another
afterwards, which is the failure §21 freezes evaluation rules to avoid. The assessment
records what was believed and from what evidence; the review records how that turned out.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from aureon.models.base import AureonDocument, AureonModel, UtcDatetime
from aureon.models.enums import Direction, SessionName, TrendBias

#: Cohort dimensions, in the order they are given up when there is not enough history.
#:
#: The order is the claim: the wick tag is the narrowest and least load-bearing (it says
#: something about one candle), the value-area position next, and the volatility regime last
#: because a high-volatility cohort and a low-volatility one genuinely answer different
#: questions -- $5 of gold is a common excursion in one and a rare one in the other.
#: Symbol, agent, direction and session are NEVER dropped: a cohort that mixed them would be
#: answering about a different instrument, a different signal, or a different time of day.
WIDENING_ORDER: tuple[str, ...] = ("wick_tag", "price_vs_va", "volatility_regime")

#: Below this many COMPLETE evaluations, no percentage is published at all. Thirty is not a
#: statistical threshold so much as a legibility one: a Wilson interval on n=12 is so wide
#: that printing the point estimate beside it invites reading the estimate and ignoring the
#: interval.
MIN_COHORT = 30


class TrendRead(AureonModel):
    """What the last N closed candles did, as facts rather than as a verdict (9D).

    ``evidence`` is deliberately a list of raw statements -- "ema20 above ema50",
    "3 higher highs, 1 lower low", "session trend up" -- with no adjectives. The bias is a
    summary of those facts and the facts stay visible beside it, so a reader who disagrees
    with the summary can see exactly what it was summarising.
    """

    model_config = ConfigDict(extra="forbid")

    bias: TrendBias
    evidence: tuple[str, ...] = ()
    candles: int = Field(default=0, ge=0, description="How many closed candles were read.")
    as_of: UtcDatetime | None = None


class CohortFilter(AureonModel):
    """Which prior detections this assessment counted, and what it had to give up."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    agent_name: str
    direction: Direction
    session: SessionName | None = None
    trend_aligned: bool | None = None
    volatility_regime: str | None = None
    price_vs_va: str | None = None
    wick_tag: str | None = None
    #: Dimensions widened away, in the order they were dropped. Reported, never silent: a
    #: cohort that quietly stopped matching on volatility regime is a different question
    #: wearing the same words.
    dropped: tuple[str, ...] = ()


class ThresholdConfirmation(AureonModel):
    """How often the cohort reached one threshold, with n and its interval."""

    model_config = ConfigDict(extra="forbid")

    threshold: float
    reached: int = Field(default=0, ge=0)
    evaluated: int = Field(default=0, ge=0)
    ci_low: float | None = None
    ci_high: float | None = None
    median_seconds_to_first: float | None = None

    @model_validator(mode="after")
    def _reached_within_evaluated(self) -> ThresholdConfirmation:
        if self.reached > self.evaluated:
            raise ValueError(
                f"threshold {self.threshold}: reached {self.reached} exceeds "
                f"evaluated {self.evaluated}"
            )
        return self

    @property
    def rate(self) -> float | None:
        """``None`` rather than 0.0 on an empty cohort.

        A rate of zero is a measurement -- "it never happened" -- and an empty cohort is
        the absence of one. Rendering them the same way is how "we have no data" becomes
        "it never works".
        """
        if self.evaluated == 0:
            return None
        return self.reached / self.evaluated


class HorizonConfirmation(AureonModel):
    """One horizon's confirmation table for the cohort."""

    model_config = ConfigDict(extra="forbid")

    horizon_id: str
    evaluated: int = Field(default=0, ge=0)
    thresholds: tuple[ThresholdConfirmation, ...] = ()
    #: How often adversity came first (§23's MAE_FIRST). The other half of a reached-rate:
    #: a threshold reached after a drawdown a human would have closed through is not the
    #: same outcome as one reached cleanly.
    mae_first: int = Field(default=0, ge=0)
    #: Excluded from ``mae_first`` rather than counted either way: these are the cases where
    #: both thresholds were first crossed inside one candle, so the order was never observed.
    path_ambiguous: int = Field(default=0, ge=0)


class Estimate(AureonModel):
    """One quantile of the cohort's measured excursion, in points and in price."""

    model_config = ConfigDict(extra="forbid")

    quantile: float = Field(gt=0.0, lt=1.0)
    points: float
    price: float | None = Field(
        default=None, description="The points applied to this detection's reference price."
    )


class PairedOutcome(AureonModel):
    """How often the cohort reached +T before -S, for the symbol's configured pair."""

    model_config = ConfigDict(extra="forbid")

    favourable: float
    adverse: float
    favourable_first: int = Field(default=0, ge=0)
    evaluated: int = Field(default=0, ge=0)
    ci_low: float | None = None
    ci_high: float | None = None

    @property
    def rate(self) -> float | None:
        if self.evaluated == 0:
            return None
        return self.favourable_first / self.evaluated


class Assessment(AureonDocument):
    """One measured readout for one detection (§62-§64, 9D).

    Stored so the weekly review can ask, later, whether the quantiles it published held --
    which is the only way a readout like this earns or loses trust. Never scored in place;
    see the module docstring.
    """

    model_config = ConfigDict(extra="forbid")

    assessment_id: str
    detection_id: str
    symbol: str
    #: The frozen rule the cohort's numbers were measured under (§21). Without it, an
    #: assessment from before a rule change and one from after would be indistinguishable
    #: while meaning different things.
    rule_id: str
    trend_read: TrendRead
    cohort_filter: CohortFilter
    n: int = Field(default=0, ge=0)
    #: Named ``confirmations`` rather than ``horizons``: it holds confirmation ROWS, one
    #: per horizon, not horizons. The accurate name also keeps it clear of the blunt grep
    #: that stops review code reading ``DetectionEvaluation.horizons`` unfiltered (§22) --
    #: a guard worth keeping blunt, since the thing it prevents is counting a PENDING
    #: outcome as a miss.
    confirmations: tuple[HorizonConfirmation, ...] = ()
    tp_estimates: tuple[Estimate, ...] = ()
    sl_estimates: tuple[Estimate, ...] = ()
    paired: PairedOutcome | None = None
    #: Set when the cohort never reached MIN_COHORT even fully widened. The assessment is
    #: still stored -- "we looked and there was not enough history" is a finding, and one
    #: worth being able to count later.
    insufficient: bool = False
    #: True when the trend read and the detection's own direction disagree. Stored rather
    #: than derived at render time so a review can count how often the two parted company.
    disagrees_with_detection: bool = False
    created_at: UtcDatetime | None = None


class TradeNote(AureonDocument):
    """A human sentence about one trade, kept out of the trade (§45, 9D).

    Its own collection precisely because a CLOSED trade refuses field writes: a note is not
    a reconciliation and must not be smuggled in as one. Nothing automated ever reads these
    -- not the assessment, not the cohort, not any gate. The weekly review prints them.
    """

    model_config = ConfigDict(extra="forbid")

    note_id: str
    trade_id: str
    author: str
    text: str = Field(min_length=1, max_length=2000)
    at: UtcDatetime | None = None

    @property
    def tags(self) -> tuple[str, ...]:
        """``#tag`` words in the text, lowercased, in order of first appearance.

        Parsed at read time rather than stored: a tag is part of what the human wrote, and
        a stored copy would drift from the sentence if the sentence were ever edited.
        """
        import re

        seen: list[str] = []
        for match in re.findall(r"#([A-Za-z0-9_-]+)", self.text):
            tag = match.lower()
            if tag not in seen:
                seen.append(tag)
        return tuple(seen)
