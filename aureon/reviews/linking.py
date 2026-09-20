"""Inferring which detection a trade was probably acting on (§50, §64).

## Inferred links live on the review and nowhere else

This is the whole point of the module, and decision 8 already encoded it: a
``TradeRequest`` and a ``Trade`` **reject** ``link_type=inferred``. Inferring that a trade
was "probably because of" a detection is useful for research and unreliable as a fact.
Written onto the trade it would become permanent and would eventually be read as though a
human had stated it -- and every statistic built on it would inherit a guess it could no
longer identify as one.

So the inference is computed here, attached to the review as ``inferred_links`` with a
confidence, and **nothing in this module writes to** ``trades`` **or** ``trade_requests``.
A test asserts that.

## What counts as a link (§50)

Same symbol, same direction, the trade opened **after** the detection and within
``AUREON_INFER_WINDOW_MINUTES``, in the same session. All four, because each one alone is
far too weak:

* same symbol and direction alone matches every long on a trending day;
* a time window alone matches anything that happened to be nearby;
* the session requirement stops a link spanning a session boundary, where the context the
  detection described has already changed.

An **explicit** link always wins: if a human chose a detection in Discord, there is nothing
to infer, and offering a competing guess would be noise.

## Confidence is a rank, not a probability

It orders candidates -- a trade two minutes after a detection is a better guess than one
twenty-nine minutes after -- and it is deliberately not calibrated. Calling it a
probability would invite arithmetic it cannot support.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from aureon.config.sessions import session_for
from aureon.models.base import to_utc
from aureon.models.detection import Detection
from aureon.models.enums import ExecutionClassification, LinkType, SessionName
from aureon.models.evaluation import DetectionEvaluation
from aureon.models.review import InferredLink
from aureon.models.trade import Trade

log = logging.getLogger(__name__)

DEFAULT_INFER_WINDOW_MINUTES = 30

# Confidence floor for a link at the very edge of the window, rising to 1.0 for a trade
# opened in the same instant as the detection.
MIN_CONFIDENCE = 0.30


@dataclass(frozen=True)
class LinkCandidate:
    """A possible detection→trade association, with why it was considered."""

    detection: Detection
    trade: Trade
    delay_seconds: float
    confidence: float

    def to_inferred(self) -> InferredLink:
        return InferredLink(
            detection_id=self.detection.detection_id,
            trade_id=self.trade.trade_id,
            confidence=self.confidence,
            reason=(
                f"same symbol and direction, trade opened {self.delay_seconds / 60:.1f}m "
                f"after the detection in the same session"
            ),
        )


def _confidence(delay_seconds: float, window_seconds: float) -> float:
    """Rank a candidate by how soon the trade followed the detection.

    Linear from 1.0 at zero delay to ``MIN_CONFIDENCE`` at the window's edge. A rank, not
    a calibrated probability -- see the module docstring.
    """
    if window_seconds <= 0:
        return MIN_CONFIDENCE
    fraction = max(0.0, min(1.0, delay_seconds / window_seconds))
    return round(1.0 - fraction * (1.0 - MIN_CONFIDENCE), 4)


def _session_of(moment: datetime, market_tz: str) -> SessionName:
    from zoneinfo import ZoneInfo

    return session_for(to_utc(moment).astimezone(ZoneInfo(market_tz)))


def infer_links(
    detections: list[Detection],
    trades: list[Trade],
    *,
    market_tz: str,
    window_minutes: int = DEFAULT_INFER_WINDOW_MINUTES,
) -> list[InferredLink]:
    """Infer detection→trade links for a period (§50).

    Each trade gets **at most one** inferred link -- its best candidate. A trade with two
    links would be double-counted by every aggregate downstream, and a human placed one
    trade for at most one reason.

    Trades carrying an **explicit** link are skipped entirely: a human already said which
    detection it was.
    """
    window_seconds = window_minutes * 60.0
    by_trade: dict[str, LinkCandidate] = {}

    for trade in trades:
        if trade.link_type is LinkType.EXPLICIT and trade.detection_id:
            continue  # the human already told us
        opened = trade.open_time.utc
        trade_session = _session_of(opened, market_tz)

        for detection in detections:
            if detection.direction is None:
                # Context-only detections assert no direction, so "same direction" is
                # undefined and a link would be meaningless.
                continue
            if detection.symbol != trade.symbol:
                continue
            if detection.direction is not trade.direction:
                continue
            delay = (opened - detection.detected_at.utc).total_seconds()
            # Strictly after: a trade opened before the detection cannot have been caused
            # by it, however close in time.
            if delay < 0 or delay > window_seconds:
                continue
            if _session_of(detection.detected_at.utc, market_tz) is not trade_session:
                continue

            candidate = LinkCandidate(
                detection=detection,
                trade=trade,
                delay_seconds=delay,
                confidence=_confidence(delay, window_seconds),
            )
            best = by_trade.get(trade.trade_id)
            if best is None or candidate.confidence > best.confidence:
                by_trade[trade.trade_id] = candidate

    links = [c.to_inferred() for c in by_trade.values()]
    # Deterministic order, so a re-run is byte-identical.
    links.sort(key=lambda link: (link.detection_id, link.trade_id))
    return links


# ── Classification (§64) ──────────────────────────────────────────────────────


def classify(
    detection: Detection,
    evaluation: DetectionEvaluation | None,
    *,
    threshold: float,
    horizon_id: str,
    was_traded: bool,
) -> ExecutionClassification:
    """Compare what the machine saw with what the human did (§64).

    Reads **only** ``complete_horizons``. A horizon that is still PENDING, or INVALID,
    yields ``UNKNOWN`` -- never "did not reach". That distinction is the entire point: an
    unknown outcome counted as a failure would make every comparison in the review
    pessimistic, and a reader would have no way to tell.
    """
    if evaluation is None:
        return ExecutionClassification.UNKNOWN

    horizon = next(
        (h for h in evaluation.complete_horizons if h.horizon_id == horizon_id), None
    )
    if horizon is None:
        return ExecutionClassification.UNKNOWN

    reached = horizon.reached_threshold(threshold)
    if reached is None:
        return ExecutionClassification.UNKNOWN

    if was_traded:
        return (
            ExecutionClassification.TAKEN_AND_REACHED
            if reached
            else ExecutionClassification.TAKEN_AND_NOT_REACHED
        )
    return (
        ExecutionClassification.MISSED_AND_REACHED
        if reached
        else ExecutionClassification.MISSED_AND_NOT_REACHED
    )


def classify_period(
    detections: list[Detection],
    evaluations: dict[str, DetectionEvaluation],
    trades: list[Trade],
    links: list[InferredLink],
    *,
    threshold: float,
    horizon_id: str,
) -> dict[ExecutionClassification, int]:
    """Classification counts for a period (§64).

    A trade with no detection behind it -- explicit or inferred -- is ``DISCRETIONARY``:
    the human's own idea, which is worth counting separately rather than silently omitting.
    """
    linked_detections = {link.detection_id for link in links}
    linked_detections.update(
        t.detection_id for t in trades if t.detection_id and t.link_type is LinkType.EXPLICIT
    )
    linked_trades = {link.trade_id for link in links}
    linked_trades.update(
        t.trade_id for t in trades if t.detection_id and t.link_type is LinkType.EXPLICIT
    )

    counts: dict[ExecutionClassification, int] = {c: 0 for c in ExecutionClassification}

    for detection in detections:
        if detection.direction is None:
            # Context-only: never an actionable signal, so it is not a missed one either.
            continue
        verdict = classify(
            detection,
            evaluations.get(detection.detection_id),
            threshold=threshold,
            horizon_id=horizon_id,
            was_traded=detection.detection_id in linked_detections,
        )
        counts[verdict] += 1

    counts[ExecutionClassification.DISCRETIONARY] = sum(
        1 for t in trades if t.trade_id not in linked_trades
    )
    return counts
