"""Read-only access to ``detection_evaluations`` (§22, 9D).

Exists because of a boundary, not for tidiness — the same one ``ReviewReader`` exists for.
`/monitor` must **read** the stored outcomes to count how often detections of a shape
reached a threshold, but ``EvaluationRepository`` can also upsert one, and an evaluation is
the measured record of what followed a detection. A human interface holding an object that
could rewrite it would make every number built on those rows unfalsifiable, and the boundary
test that keeps writers out of ``aureon/discord`` caught exactly that when the writing
repository was first handed to ``BotContext``.

So Discord imports this, which has no write method at all. The capability split is visible in
the import graph rather than resting on nobody calling the wrong method.
"""

from __future__ import annotations

from typing import Any

from aureon.models.evaluation import DetectionEvaluation
from aureon.storage import paths


class EvaluationReader:
    """Reads ``detection_evaluations``. Cannot write them."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self, detection_id: str, rule_id: str) -> DetectionEvaluation | None:
        snapshot = self._client.document(
            paths.detection_evaluation_path(detection_id, rule_id)
        ).get()
        if not getattr(snapshot, "exists", False):
            return None
        return DetectionEvaluation.model_validate(snapshot.to_dict())

    def get_many(
        self, detection_ids: list[str], rule_id: str
    ) -> dict[str, DetectionEvaluation]:
        """Evaluations for these detections under one rule, by exact document id.

        By id rather than by query: no index, and no chance of a query silently missing one
        -- which for a cohort would mean a detection counted as unevaluated rather than as
        answered, quietly shrinking the very n the readout is reporting.
        """
        found: dict[str, DetectionEvaluation] = {}
        for detection_id in detection_ids:
            evaluation = self.get(detection_id, rule_id)
            if evaluation is not None:
                found[detection_id] = evaluation
        return found
