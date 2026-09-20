"""Detection evaluation persistence (§22).

Stored at ``detection_evaluations/{detection_id}__{rule_id}``: one document per
(detection, rule) pair, so a second rule can be evaluated over the same history
without touching the first rule's frozen results.

Writes are idempotent ``set()`` by that id, like detections -- a re-run of the
backfill overwrites rather than duplicating.
"""

from __future__ import annotations

from typing import Any

from aureon.models.evaluation import DetectionEvaluation
from aureon.storage import paths


class EvaluationRepository:
    """Reads and upserts ``detection_evaluations`` documents."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def upsert(self, evaluation: DetectionEvaluation) -> str:
        path = paths.detection_evaluation_path(evaluation.detection_id, evaluation.rule_id)
        self._client.document(path).set(evaluation.model_dump(mode="json"))
        return path

    def get(self, detection_id: str, rule_id: str) -> DetectionEvaluation | None:
        snapshot = self._client.document(
            paths.detection_evaluation_path(detection_id, rule_id)
        ).get()
        if not getattr(snapshot, "exists", False):
            return None
        return DetectionEvaluation.model_validate(snapshot.to_dict())

    def upsert_many(self, evaluations: list[DetectionEvaluation]) -> int:
        for evaluation in evaluations:
            self.upsert(evaluation)
        return len(evaluations)

    def get_many(self, detection_ids: list[str], rule_id: str) -> dict[str, Any]:
        """Evaluations for these detections under one rule, by exact document id.

        By id rather than by query: no index, and no chance of a query silently missing
        one -- which for an evaluation would mean its detection counted as unevaluated
        rather than as answered.
        """
        found: dict[str, Any] = {}
        for detection_id in detection_ids:
            evaluation = self.get(detection_id, rule_id)
            if evaluation is not None:
                found[detection_id] = evaluation
        return found
