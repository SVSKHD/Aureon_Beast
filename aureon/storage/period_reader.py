"""Read-only access to everything a review aggregates (§50, §61).

Exists to resolve a real conflict between two CLAUDE.md rules, not for tidiness:

* Firestore access must go through a repository -- so the review service cannot stream
  collections off the client itself, which is what it used to do.
* Reviews must not hold anything that can write a detection, trade, evaluation or
  session -- an inferred link is analysis, and analysis must not edit its subject.

Satisfying the first with the obvious change (import the four repositories) breaks the
second, because those classes can write. So the composition happens HERE, inside
``aureon/storage`` where writing is allowed, and what reviews import is this: four period
reads and no write method at all.

Same shape as ``ReviewReader``, same reason. The capability split is visible in the
import graph rather than resting on nobody calling the wrong method, and two boundary
tests hold both halves in place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aureon.models.assessment import Assessment, TradeNote
from aureon.models.detection import Detection
from aureon.models.evaluation import DetectionEvaluation
from aureon.models.session import SessionSummary
from aureon.models.trade import Trade
from aureon.storage.assessment_repository import AssessmentRepository
from aureon.storage.detection_repository import DetectionRepository
from aureon.storage.evaluation_repository import EvaluationRepository
from aureon.storage.note_repository import TradeNoteRepository
from aureon.storage.session_repository import SessionRepository
from aureon.storage.trade_repository import TradeRepository


class PeriodReader:
    """Reads detections, evaluations, trades and sessions for a period. Cannot write."""

    def __init__(self, client: Any, *, account_scope: str = "primary") -> None:
        # Private, and deliberately not exposed: a caller that could reach the
        # repositories could write with them, which is the whole thing this prevents.
        self.__detections = DetectionRepository(client)
        self.__evaluations = EvaluationRepository(client)
        self.__trades = TradeRepository(client, account_scope=account_scope)
        self.__sessions = SessionRepository(client)
        # 9D. Both READ-ONLY through this surface, like everything else here: a review
        # describes a period, and one that could rewrite an assessment or a note would be
        # able to make its own numbers agree with itself.
        self.__assessments = AssessmentRepository(client)
        self.__notes = TradeNoteRepository(client)

    def detections_in(self, start: datetime, end: datetime) -> list[Detection]:
        return self.__detections.in_period(start, end)

    def evaluations_for(
        self, detections: list[Detection], rule_id: str
    ) -> dict[str, DetectionEvaluation]:
        return self.__evaluations.get_many(
            [d.detection_id for d in detections], rule_id
        )

    def trades_opened_in(self, start: datetime, end: datetime) -> list[Trade]:
        return self.__trades.opened_in_period(start, end)

    def sessions_in(self, start: datetime, end: datetime) -> list[SessionSummary]:
        return self.__sessions.in_period(start, end)

    def assessments_in(self, start: datetime, end: datetime) -> list[Assessment]:
        """`/monitor` readouts produced in the period, for scoring them (9D)."""
        return self.__assessments.in_period(start, end)

    def notes_for(self, trades: list[Trade]) -> dict[str, list[TradeNote]]:
        """What the trader wrote about each of the period's trades (9D)."""
        return self.__notes.for_trades([t.trade_id for t in trades])
