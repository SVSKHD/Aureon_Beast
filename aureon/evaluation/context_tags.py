"""Context tags: what else the machine had seen when a detection fired (§19, §23).

A reached-N figure for "bullish cross" averages together crosses that arrived in very
different contexts. A cross into a lower-rejection wick and a cross with nothing else
around it are the same row in the current reports, and if one of them works and the
other does not, the average hides both facts. These tags exist so a review can split
the population instead of averaging it: "bullish cross WITH a lower rejection wick"
versus "without".

## The rule that makes them trustworthy

**A tag may only use information available at the detection's own candle close.** That
is the same no-hindsight rule Phase 3 rests on (§19), and it is easier to break here
than anywhere else in the system, because the obvious implementation -- look at the
detections around this one -- has no natural sense of direction in time. A wick that
forms on the NEXT candle is not context, it is the future, and a tag built from it
would make the correlation look real while being unfalsifiable.

So ``derive`` takes only detections whose ``detected_at`` is at or before the subject's,
and a test feeds it a wick on the following candle to prove it is ignored.

## These are correlations, not causes

Nothing here claims a wick makes a cross work. The tags partition a population so a
human can look; with 29 crosses in a week, any difference between two subsets is
anecdote. They are labelled research in ``docs/PHASES.md`` for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from aureon.models.detection import Detection
from aureon.models.enums import Direction

#: How many candles back a sweep still counts as context for a cross. From
#: ``AUREON_CONTEXT_WINDOW_CANDLES``; 3 by default, which on M5 is fifteen minutes --
#: close enough that a human watching the chart would have seen both events together.
DEFAULT_CONTEXT_WINDOW_CANDLES = 3

#: Tag names, fixed so a review can group by them without string-matching guesswork.
TAG_LOWER_REJECTION_WICK = "has_lower_rejection_wick"
TAG_UPPER_REJECTION_WICK = "has_upper_rejection_wick"
TAG_SWEEP_SAME_DIRECTION = "sweep_same_direction_within_N_candles"
TAG_SESSION_TREND_ALIGNED = "session_trend_aligned"

CONTEXT_TAGS: tuple[str, ...] = (
    TAG_LOWER_REJECTION_WICK,
    TAG_UPPER_REJECTION_WICK,
    TAG_SWEEP_SAME_DIRECTION,
    TAG_SESSION_TREND_ALIGNED,
)

# Event keys the tags read. Named rather than inlined so a change to an agent's event
# vocabulary breaks here, at the mapping, instead of silently producing all-False tags.
_WICK_AGENT = "wick"
_WICK_LOWER = "lower_rejection"
_WICK_UPPER = "upper_rejection"
_LIQUIDITY_AGENT = "liquidity"
_SESSION_TREND_AGENT = "session_trend"


@dataclass(frozen=True)
class ContextWindow:
    """The detections a subject is allowed to know about.

    Constructed by ``window_for``, which is the only place the "at or before" rule is
    applied -- so there is one line to read when asking whether hindsight can leak.
    """

    subject: Detection
    prior: tuple[Detection, ...] = field(default_factory=tuple)


def window_for(
    subject: Detection,
    others: list[Detection],
    *,
    window_candles: int = DEFAULT_CONTEXT_WINDOW_CANDLES,
) -> ContextWindow:
    """Detections on the same symbol at or before ``subject``, within the window.

    Half-open backwards: a detection on the subject's own candle counts (it was known
    at the same close), one on a later candle never does.
    """
    span = timedelta(minutes=subject.timeframe.minutes * window_candles)
    earliest = subject.detected_at.utc - span
    subject_at = subject.detected_at.utc

    prior = [
        other
        for other in others
        if other.detection_id != subject.detection_id
        and other.symbol == subject.symbol
        and other.timeframe == subject.timeframe
        # THE no-hindsight line. Everything else in this module is arithmetic.
        and earliest <= other.detected_at.utc <= subject_at
    ]
    prior.sort(key=lambda d: (d.detected_at.utc, d.detection_id))
    return ContextWindow(subject=subject, prior=tuple(prior))


def derive(
    subject: Detection,
    others: list[Detection],
    *,
    window_candles: int = DEFAULT_CONTEXT_WINDOW_CANDLES,
) -> dict[str, bool]:
    """The four context tags for one detection (§19, §23).

    Every tag is present in the result, ``False`` included. A missing key and a False
    value are different claims -- "we did not look" versus "we looked and it was not
    there" -- and a review grouping by tag needs the second.
    """
    window = window_for(subject, others, window_candles=window_candles)
    return {
        TAG_LOWER_REJECTION_WICK: _has_wick(window, _WICK_LOWER),
        TAG_UPPER_REJECTION_WICK: _has_wick(window, _WICK_UPPER),
        TAG_SWEEP_SAME_DIRECTION: _has_aligned_sweep(window),
        TAG_SESSION_TREND_ALIGNED: _session_trend_aligned(window),
    }


def _has_wick(window: ContextWindow, event_key: str) -> bool:
    """A wick rejection of the given kind on or just before the subject's candle."""
    return any(
        d.agent_name == _WICK_AGENT and d.event_key == event_key for d in window.prior
    )


def _has_aligned_sweep(window: ContextWindow) -> bool:
    """A liquidity sweep pointing the same way as the subject.

    Same *direction*, not same event key: the liquidity agent's key carries the sweep's
    direction while its ``direction`` field carries the implied reversal, and the
    reversal is the tradeable side -- so that is the one that has to agree.
    """
    if window.subject.direction is None:
        return False
    return any(
        d.agent_name == _LIQUIDITY_AGENT and d.direction is window.subject.direction
        for d in window.prior
    )


def _session_trend_aligned(window: ContextWindow) -> bool:
    """The session's own trend agrees with the subject's direction.

    ``session_trend`` emits ``{session}|{up|down|flat}`` with no direction of its own,
    so the trend is read from the event key. ``flat`` is never alignment: a flat session
    agrees with nothing, and calling it aligned would put half the flat sessions in the
    "aligned" bucket for each direction.
    """
    direction = window.subject.direction
    if direction is None:
        return False
    wanted = "up" if direction is Direction.BUY else "down"
    for other in window.prior:
        if other.agent_name != _SESSION_TREND_AGENT:
            continue
        _, _, trend = other.event_key.partition("|")
        if trend == wanted:
            return True
    return False


def derive_all(
    detections: list[Detection],
    *,
    window_candles: int = DEFAULT_CONTEXT_WINDOW_CANDLES,
    only_agents: frozenset[str] | None = None,
) -> dict[str, dict[str, bool]]:
    """Tags for every detection worth tagging, keyed by ``detection_id``.

    ``only_agents`` restricts the subjects (not the context): there is no point tagging
    a context-only detection, which has no outcome to correlate the tags with. The full
    list is still passed as context for the ones that are tagged.
    """
    subjects = [
        d
        for d in detections
        if only_agents is None or d.agent_name in only_agents
        if d.direction is not None
    ]
    return {
        d.detection_id: derive(d, detections, window_candles=window_candles)
        for d in subjects
    }
