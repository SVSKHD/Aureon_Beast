"""A setup: a structure the market is building, tracked over time (12, T-6).

A detection is a fact about one closed candle. A setup is a claim about a sequence — that a level
was swept, reclaimed, and confirmed — and it therefore has something a detection does not: a
**state that changes**. Everything awkward about this model follows from that one difference.

## Why it is not a detection with extra fields

Detections are immutable (CLAUDE.md), and they have to be: their ids are a hash over their
content, the replay/live parity check compares them directly, and the evaluation rules measure
them from a fixed instant. A setup is the opposite kind of object — it is edited on every candle
that advances it — so it cannot share a collection or a contract with them. What it shares is the
DISCIPLINE: a deterministic id, a state machine gated by ``assert_transition``, and an event
sub-collection that records every change so the document's current state is never the only record.

## The events are the history; the document is the summary

``setups/{setup_id}`` holds where the setup is now. ``setups/{setup_id}/events/{event_id}`` holds
how it got there, one row per change, with the context that was true at the time. The split is the
same one §21 makes between a detection and its evaluations, for the same reason: a summary that
tried to carry its own history grows without bound, and an unbounded array in a Firestore document
is how a document comes to fail its write at a size limit nobody was watching.

So every list on the summary is capped, and ``event_count`` is a number rather than a list.

## Nothing here is an instruction

``direction_context`` is BULLISH/BEARISH/NEUTRAL and never BUY/SELL, ``invalidation_price`` is
where the structure stops being true and not a stop-loss, and ``reference`` carries a measured
historical cohort that is labelled as research context. No field on this model is a target, a lot
size or a recommendation, and a test asserts the trading vocabulary appears nowhere in it. The one
thing this system must never do is decide.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from aureon.models.assessment import (
    MATURE_REAL_DAYS,
    SL_QUANTILES,
    TP_QUANTILES,
    Estimate,
    PairedOutcome,
)
from aureon.models.base import AureonDocument, AureonModel, MarketTime, UtcDatetime
from aureon.models.enums import (
    DirectionContext,
    HistorySource,
    MtfAlignment,
    SessionName,
    SetupAnchorKind,
    SetupEventType,
    SetupFamily,
    SetupState,
    Timeframe,
)

#: How many detection ids a setup's summary carries. The LAST twenty, because a card shows the
#: recent ones and a reviewer who wants all of them reads the events, where every single one is
#: recorded against the change it caused. Twenty rather than five because a long trend-pullback
#: setup legitimately accumulates a dozen, and rather than a hundred because this is a summary.
MAX_LINKED_DETECTIONS = 20

#: How many key/value pairs a context snapshot may carry, and how long a value may be. A snapshot
#: is a handful of numbers that were true at one candle close; a cap is what stops it becoming a
#: place to dump whatever was in scope.
MAX_CONTEXT_KEYS = 20
MAX_CONTEXT_VALUE_CHARS = 120


class SetupAnchor(AureonModel):
    """What the setup is anchored to, and where.

    Half the setup's identity (see ``setup_id_components``). The kind matters as much as the
    price: "the level at 2412.5" and "the session high, which is at 2412.5" are different things
    the moment the session high moves, and a setup that could not tell them apart would silently
    merge two structures into one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SetupAnchorKind
    price: float
    #: For a liquidity level: which kind it was, in the level tracker's own vocabulary. Optional
    #: because a POC or an EMA zone has no such sub-kind.
    level_type: str | None = None
    #: The detection that established the anchor, when one did. A reference, never a copy: the
    #: detection stays immutable and this setup does not own it.
    detection_id: str | None = None


class SetupContextSummary(AureonModel):
    """The market context this setup exists in, as four labels.

    Four and not forty. This block is what a review groups by — "did liquidity reversals work
    better in a high-volatility London?" — and a grouping key with forty dimensions is a grouping
    key with one setup in each bucket. The numbers behind each label live on the detections and in
    the event snapshots; these are the axes worth counting.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mtf_alignment: MtfAlignment = MtfAlignment.MIXED
    #: One of ``profile.VOLATILITY_REGIMES``. A string rather than an enum because the regime
    #: bands are versioned config, and pinning the labels in an enum would make a config change
    #: a code change.
    volatility_regime: str | None = None
    #: ``above`` / ``inside`` / ``below`` the value area, as ``VolumeProfileRef`` renders it.
    price_vs_va: str | None = None
    session: SessionName | None = None


#: The words every rendering of a ``SetupReference`` carries, verbatim (T-9).
#:
#: Three claims, in the order a reader needs them: it is HISTORY (not a forecast), it is a
#: MEASURED COHORT (arithmetic over stored outcomes, not a model), and it is RESEARCH CONTEXT
#: (not a target and not advice). A rendering that shortened it to "reference" would leave the
#: numbers looking like a recommendation, which is the one thing they must never look like.
REFERENCE_LABEL = "historical reference · measured cohort · research context"

#: What a reference says when the cohort never reached ``MIN_COHORT`` even fully widened, or when
#: the history behind it is not yet mature. Kept beside the label rather than replacing it, so a
#: reader sees both what the block is and why its numbers are not worth much yet.
IMMATURE_NOTE = "immature history"


class SetupReference(AureonModel):
    """A measured historical cohort, carried as research context and labelled as such (T-9).

    Not a target and not a prediction. It is the answer to "when this shape happened before, what
    did the population do" — produced by the same arithmetic ``/monitor`` uses (the same quantile
    and Wilson functions, the same ``MIN_COHORT`` floor, the same ``MATURE_REAL_DAYS`` maturity
    rule), so there is exactly one definition of a cohort in the system and no second TP/SL engine.

    ``mfe`` is the cohort's measured favourable excursion at p50 and p25; ``mae`` its measured
    adverse excursion at p75 and p90 — the same quantiles and the same asymmetry
    ``assessment_service`` publishes, in points. ``paired`` is how often the favourable distance
    came first, with its 95% interval and its n.

    ``history_source`` and ``real_days`` ride along because a cohort built from fixtures and one
    built from six months of real sessions are different claims, and a screen that showed them
    alike would let a synthetic number be read as a measured one (11C).

    Every field is optional and the default is an EMPTY reference: a setup opened before any
    history existed carries one that says nothing, which is different from one that says zero.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The frozen rule the cohort's outcomes were measured under (§21), and which of its horizons
    #: the excursions come from. Without both, a reference from before a rule change and one from
    #: after are indistinguishable while meaning different things.
    rule_id: str | None = None
    horizon_id: str | None = None
    cohort_n: int | None = Field(default=None, ge=0)
    #: Which cohort dimensions had to be given up to reach ``MIN_COHORT``, in the order they were
    #: dropped. Reported, never silent: a cohort that quietly stopped matching on volatility
    #: regime is a different question wearing the same words.
    dropped: tuple[str, ...] = ()
    #: p50 and p25 of the cohort's measured MFE, in points.
    mfe: tuple[Estimate, ...] = ()
    #: p75 and p90 of the cohort's measured MAE, in points.
    mae: tuple[Estimate, ...] = ()
    #: How often the favourable distance came first, with n and a 95% Wilson interval.
    paired: PairedOutcome | None = None
    #: Set when the cohort never reached ``MIN_COHORT`` even fully widened. Recorded rather than
    #: left blank: "we looked and there was not enough history" is a finding worth counting.
    insufficient: bool = False
    history_source: HistorySource | None = None
    real_days: int | None = Field(default=None, ge=0)
    measured_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _quantiles_are_the_published_ones(self) -> SetupReference:
        """The quantiles are not free text.

        A reference whose ``mfe`` carried p90 would read as a target somebody could hit rather
        than as the median of what already happened, and it would not be comparable with the
        ``/monitor`` readout beside it. The allowed sets are the ones ``assessment_service``
        publishes, imported rather than repeated.
        """
        for name, found, allowed in (
            ("mfe", self.mfe, TP_QUANTILES),
            ("mae", self.mae, SL_QUANTILES),
        ):
            unexpected = [e.quantile for e in found if e.quantile not in allowed]
            if unexpected:
                raise ValueError(
                    f"{name} carries quantiles {unexpected}, which are not the published "
                    f"{sorted(allowed)}"
                )
        return self

    @property
    def is_evidence(self) -> bool:
        """Whether this reference is about the instrument rather than about a fixture."""
        return self.history_source is not None and self.history_source.is_evidence

    @property
    def immature(self) -> bool:
        """Under ``MATURE_REAL_DAYS`` verified broker days, whatever the cohort size.

        The same floor ``Assessment.immature`` uses, and separate from ``insufficient`` for the
        same reason: a cohort of two hundred setups from one Tuesday is not two hundred
        independent observations. A reference that never measured anything (``real_days`` unset)
        is immature too — there is nothing mature about an absence.
        """
        return (self.real_days or 0) < MATURE_REAL_DAYS

    @property
    def measured(self) -> bool:
        """Whether this reference carries any arithmetic at all."""
        return self.cohort_n is not None

    @property
    def caption(self) -> str:
        """The one line every rendering of this block must carry.

        Built here rather than in the embed so the Discord card, the weekly review and any future
        surface cannot disagree about what the numbers are. A caption is the smallest place a
        measured number can quietly become advice, so it is not left to a caller.
        """
        if not self.measured:
            return f"{REFERENCE_LABEL} · nothing measured yet"
        parts = [REFERENCE_LABEL, f"n={self.cohort_n}"]
        if self.insufficient:
            parts.append(f"below the cohort floor · {IMMATURE_NOTE}")
        elif self.immature:
            parts.append(IMMATURE_NOTE)
        if self.dropped:
            parts.append("widened: " + ", ".join(self.dropped))
        if self.history_source is not None:
            parts.append(f"history: {self.history_source.value}")
        return " · ".join(parts)


class SetupEvent(AureonDocument):
    """One thing that happened to a setup, stored at ``setups/{setup_id}/events/{event_id}``.

    Both kinds of event live here: the lifecycle transitions and the descriptive ``WATCH_*``
    observations (T-8). A descriptive event has ``from_state == to_state``, which is how a reader
    tells them apart without consulting the enum — and why the states are stored rather than
    derived: a document should keep saying what it said, even after the machine's table changes.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str
    setup_id: str
    event_type: SetupEventType
    from_state: SetupState
    to_state: SetupState
    #: The detection that caused this event, when one did (T-9). A reference only: the detection
    #: is never touched, and a setup advancing does not edit what was detected.
    linked_detection_id: str | None = None
    #: The candle close this event belongs to, in both clocks (§8).
    market_time: MarketTime
    #: A few numbers that were true at that close. Bounded, and flattened to strings, because a
    #: nested structure here would be a second schema nobody versioned.
    context_snapshot: dict[str, str] = Field(default_factory=dict)
    #: Why, when the event type does not say it on its own. ``expired`` is the one that matters:
    #: an expiry is recorded as INVALIDATED with this reason, so "nothing happened in time" and
    #: "the structure broke" are separable in a review (12, T-6).
    reason: str | None = None
    created_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _the_snapshot_is_bounded_and_the_states_agree(self) -> SetupEvent:
        if len(self.context_snapshot) > MAX_CONTEXT_KEYS:
            raise ValueError(
                f"{self.event_id}: context_snapshot has {len(self.context_snapshot)} keys, "
                f"more than the {MAX_CONTEXT_KEYS} cap"
            )
        for key, value in self.context_snapshot.items():
            if len(value) > MAX_CONTEXT_VALUE_CHARS:
                raise ValueError(
                    f"{self.event_id}: context_snapshot[{key!r}] is {len(value)} chars, "
                    f"more than the {MAX_CONTEXT_VALUE_CHARS} cap"
                )
        if self.event_type.is_watch_event and self.from_state is not self.to_state:
            # A descriptive event that moved the state would be a lifecycle event wearing the
            # wrong name, and every reader that filters on `is_watch_event` would then be
            # filtering out a transition.
            raise ValueError(
                f"{self.event_id}: {self.event_type.value} is a descriptive event and must not "
                f"change state ({self.from_state.value} -> {self.to_state.value})"
            )
        return self


class Setup(AureonDocument):
    """A structure being tracked, stored at ``setups/{setup_id}``.

    The document is a SUMMARY: where the setup is now, plus enough of its history to render a card
    without a second query. The full history is the ``events`` sub-collection.
    """

    model_config = ConfigDict(extra="forbid")

    setup_id: str
    account_scope: str
    symbol: str
    timeframe: Timeframe
    family: SetupFamily
    direction_context: DirectionContext
    state: SetupState = SetupState.OBSERVING
    #: The BROKER date (§8). Part of the id: a level tested on two days is two setups, because
    #: each day's extremes and value area are different.
    market_date: str
    anchor: SetupAnchor
    #: Where the structure stops being true. **Not a stop-loss** — nothing places an order from
    #: this field, and the guard never reads it. It is the price at which the setup's own claim is
    #: falsified, which is what makes INVALIDATED a measurable event rather than a judgement.
    invalidation_price: float | None = None
    opened_at: UtcDatetime
    updated_at: UtcDatetime | None = None
    #: When this setup first reached CONFIRMED, and when it reached a terminal state (T-9).
    #:
    #: ``confirmed_at`` is stored rather than derived because it is NOT derivable from ``state``:
    #: a setup that confirmed and then invalidated and one that invalidated from WATCH both read
    #: INVALIDATED, and only the first ever made a claim. A review that could not tell them apart
    #: would count an absence of a prediction as a wrong one.
    confirmed_at: UtcDatetime | None = None
    closed_at: UtcDatetime | None = None
    #: The last event written, and how many there are. A count rather than a list, because a list
    #: of every event is what the sub-collection is for and what an unbounded array would become.
    last_event_id: str | None = None
    event_count: int = Field(default=0, ge=0)
    #: The most recent ``MAX_LINKED_DETECTIONS``, newest last. Capped by a validator rather than
    #: by convention: "the caller trims it" is how an array grows until a write fails.
    linked_detection_ids: tuple[str, ...] = ()
    context_summary: SetupContextSummary = Field(default_factory=SetupContextSummary)
    reference: SetupReference = Field(default_factory=SetupReference)
    #: The family's rule version. A component of the id, for the reason ``agent_version`` is a
    #: component of a detection's: when the rules change the populations must be separable.
    setup_version: str = "1.0.0"
    #: The thresholds this setup was opened under, flattened to strings. Stored because a setup
    #: judged by one set of distances and reviewed against another is not a comparison, and the
    #: numbers are not recoverable from config a month later.
    params_snapshot: dict[str, str] = Field(default_factory=dict)

    # There is deliberately NO ``message_id`` here, although T-11 edits one Discord message per
    # setup in place. The id lives on the ``notifications`` document Discord already claims for
    # this setup, because putting it here would make Discord a writer of ``setups`` -- a
    # collection §71 does not permit it, and the observer's own. One field is not worth widening
    # that boundary, and the notification document is where "what have we already said about
    # this" belongs anyway.

    @model_validator(mode="after")
    def _every_list_is_bounded_and_terminal_means_closed(self) -> Setup:
        if len(self.linked_detection_ids) > MAX_LINKED_DETECTIONS:
            raise ValueError(
                f"{self.setup_id}: {len(self.linked_detection_ids)} linked detections, more "
                f"than the {MAX_LINKED_DETECTIONS} cap"
            )
        if len(set(self.linked_detection_ids)) != len(self.linked_detection_ids):
            raise ValueError(f"{self.setup_id}: duplicate linked detection ids")
        if len(self.params_snapshot) > MAX_CONTEXT_KEYS:
            raise ValueError(
                f"{self.setup_id}: params_snapshot has {len(self.params_snapshot)} keys, more "
                f"than the {MAX_CONTEXT_KEYS} cap"
            )
        if self.is_terminal and self.closed_at is None:
            # A terminal setup with no closing time cannot be aged, and "when did this end" is
            # the first question a review asks of a completed one.
            raise ValueError(
                f"{self.setup_id}: state is {self.state.value} but closed_at is not set"
            )
        if not self.is_terminal and self.closed_at is not None:
            raise ValueError(
                f"{self.setup_id}: closed_at is set but state is {self.state.value}"
            )
        return self

    @property
    def reached_confirmation(self) -> bool:
        """Whether this setup ever made a claim. See ``confirmed_at``."""
        return self.confirmed_at is not None

    @property
    def is_terminal(self) -> bool:
        from aureon.models.enums import TERMINAL_SETUP_STATES

        return self.state in TERMINAL_SETUP_STATES

    def with_linked(self, detection_id: str) -> tuple[str, ...]:
        """``linked_detection_ids`` with this detection appended, trimmed to the cap.

        A method rather than the caller's problem, so the trim happens in one place. Already
        present is a no-op rather than a move to the end: the order is the order things happened,
        and re-ordering it would make the list lie about a sequence.
        """
        if detection_id in self.linked_detection_ids:
            return self.linked_detection_ids
        return (*self.linked_detection_ids, detection_id)[-MAX_LINKED_DETECTIONS:]
