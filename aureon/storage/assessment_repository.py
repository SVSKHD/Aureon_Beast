"""Stored ``/monitor`` readouts (9D).

One document per readout, never per detection: the same detection assessed an hour later
has an hour more history behind it, and those are two statements about two evidence bases.
Keeping both is what lets the weekly review ask whether the quantiles held -- and whether
they held better once the cohort grew.

## Written, and then left alone

Nothing here updates an assessment. The review scores them by reading the detections'
evaluations alongside, and records the rate on the review document. Writing the outcome
back would make a stored document say something different from what it said when it was
written, which is the failure §21 freezes evaluation rules to avoid.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aureon.models.assessment import Assessment
from aureon.models.base import to_utc
from aureon.storage import paths


def _where(query: Any, field: str, op: str, value: Any) -> Any:
    """``where`` across client versions, positional or keyword."""
    try:
        return query.where(filter=_field_filter(field, op, value))
    except (TypeError, ImportError):
        return query.where(field, op, value)


def _field_filter(field: str, op: str, value: Any) -> Any:
    from google.cloud.firestore_v1.base_query import FieldFilter

    return FieldFilter(field, op, value)


class AssessmentRepository:
    """Reads and writes ``{prefix}_assessments``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def store(self, assessment: Assessment) -> Assessment:
        self._client.document(paths.assessment_path(assessment.assessment_id)).set(
            assessment.model_dump(mode="json")
        )
        return assessment

    def get(self, assessment_id: str) -> Assessment | None:
        snapshot = self._client.document(paths.assessment_path(assessment_id)).get()
        if not getattr(snapshot, "exists", False):
            return None
        return Assessment.model_validate(snapshot.to_dict())

    def for_detection(self, detection_id: str) -> list[Assessment]:
        """Every readout ever produced for one detection, oldest first."""
        query = _where(
            self._client.collection(paths.ASSESSMENTS), "detection_id", "==", detection_id
        )
        found = [Assessment.model_validate(doc.to_dict()) for doc in query.stream()]
        return sorted(found, key=_created)

    def latest_for_detection(self, detection_id: str) -> Assessment | None:
        found = self.for_detection(detection_id)
        return found[-1] if found else None

    def latest_for_symbol(self, symbol: str) -> Assessment | None:
        """The most recent readout for a symbol, for ``/execute``'s info line (9D-4)."""
        query = _where(
            self._client.collection(paths.ASSESSMENTS), "symbol", "==", symbol
        )
        found = sorted(
            (Assessment.model_validate(doc.to_dict()) for doc in query.stream()),
            key=_created,
        )
        return found[-1] if found else None

    def in_period(self, start: datetime, end: datetime) -> list[Assessment]:
        """Readouts created within a period, oldest first -- the review's input.

        Filtered in Python on ``created_at`` after a symbol-free read, like the other
        period reads here: the alternative is a composite index that has to be declared,
        deployed and kept in step for a collection holding tens of documents a week.
        """
        first, last = to_utc(start), to_utc(end)
        found = [
            Assessment.model_validate(doc.to_dict())
            for doc in self._client.collection(paths.ASSESSMENTS).stream()
        ]
        return sorted(
            (
                a
                for a in found
                if a.created_at is not None and first <= to_utc(a.created_at) < last
            ),
            key=_created,
        )


def _created(assessment: Assessment) -> datetime:
    """Sort key that cannot raise on a document written before ``created_at`` existed."""
    from datetime import UTC

    if assessment.created_at is None:
        return datetime.min.replace(tzinfo=UTC)
    return to_utc(assessment.created_at)
