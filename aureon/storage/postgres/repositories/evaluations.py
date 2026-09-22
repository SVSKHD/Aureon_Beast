"""Detection evaluations, on PostgreSQL (§21, §22, plan §9).

One row per (detection, rule). That is the whole design, and it is why a second frozen
rule can be evaluated over the same history without touching the first rule's answers --
which is what makes "did V2's thresholds change the conclusion" a question you can ask of
stored data rather than one you have to re-derive.

Separate from the detection because §21 says so: an outcome written onto the detection
would be future information sitting on a record of the present, and nothing could then
say whether a row was written at the candle or edited afterwards.

The batch read is the interesting method. The Firestore version fetched evaluations one
document at a time BY ID, deliberately, because a query there could silently miss one --
and a missed evaluation makes its detection look unevaluated rather than answered, which
is a quietly wrong denominator in every rate a review reports. Here the same read is one
``WHERE detection_id = ANY(...)``: a real query, with a real index, that cannot return
fewer rows than exist.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from aureon.models.evaluation import DetectionEvaluation
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


def evaluation_row_id(detection_id: str, rule_id: str) -> str:
    """``{detection_id}__{rule_id}``, the same shape Phase 3 used.

    Kept identical so an export from Firestore lands on the same primary key (C-12) and a
    row written before the migration is the same row afterwards. The double underscore is
    unambiguous because a detection_id is a hex digest and a rule_id is ours: neither
    contains one.
    """
    if not detection_id:
        raise ValueError("detection_id must not be empty")
    if not rule_id:
        raise ValueError("rule_id must not be empty")
    return f"{detection_id}__{rule_id}"


class EvaluationRepository(PostgresRepository):
    """Reads and upserts ``detection_evaluations``."""

    table = tables.DetectionEvaluation.__table__

    # ── Writing ───────────────────────────────────────────────────────────────

    def upsert(self, evaluation: DetectionEvaluation, *, connection: Any = None) -> str:
        """Write one evaluation. Re-running a rule overwrites its own answer.

        Idempotent on purpose: the backfill is re-run over the same week whenever a horizon
        matures, and a second row per re-run would make every reached-N count grow without
        anything having been observed.
        """
        row_id = evaluation_row_id(evaluation.detection_id, evaluation.rule_id)
        self._upsert(self._to_row(evaluation, row_id), connection=connection)
        return row_id

    def upsert_many(
        self, evaluations: list[DetectionEvaluation], *, connection: Any = None
    ) -> int:
        """Write several. In ONE transaction when the caller did not supply a connection.

        A backfill that committed per row and died halfway would leave a period partly
        evaluated, and the next run would have no way to tell which rows were its own.
        """
        if not evaluations:
            return 0
        if connection is not None:
            for evaluation in evaluations:
                self.upsert(evaluation, connection=connection)
            return len(evaluations)
        with self._db.transaction() as own:
            for evaluation in evaluations:
                self.upsert(evaluation, connection=own)
        return len(evaluations)

    # ── Reading ───────────────────────────────────────────────────────────────

    def get(self, detection_id: str, rule_id: str) -> DetectionEvaluation | None:
        row = self._row(evaluation_row_id(detection_id, rule_id))
        return (
            None
            if row is None
            else DetectionEvaluation.model_validate(self._to_model_dict(row))
        )

    def get_many(
        self, detection_ids: list[str], rule_id: str
    ) -> dict[str, DetectionEvaluation]:
        """Evaluations for these detections under one rule, keyed by detection id.

        One query rather than N reads. The Firestore version could not do this safely --
        see the module docstring -- and the cost there was that a review of a thousand
        detections made a thousand round trips.

        The empty-list guard saves a pointless round trip and nothing more: SQLAlchemy
        renders ``IN ()`` as a false expression and PostgreSQL returns no rows, which was
        checked rather than assumed (decision 353). So removing it would be slower, not
        wrong -- stated plainly because the first version of this docstring claimed the
        guard was protecting against invalid SQL, and a comment that overstates a guard is
        how the guard later gets removed for the wrong reason.
        """
        if not detection_ids:
            return {}
        statement = (
            select(self.table)
            .where(self.table.c.detection_id.in_(detection_ids))
            .where(self.table.c.rule_id == rule_id)
        )
        found = self._parse_all(
            self._rows(statement), DetectionEvaluation, what="detection_evaluation"
        )
        return {evaluation.detection_id: evaluation for evaluation in found}

    def for_rule(self, rule_id: str) -> list[DetectionEvaluation]:
        """Every evaluation under one rule. The input to a threshold report."""
        statement = select(self.table).where(self.table.c.rule_id == rule_id)
        return self._parse_all(
            self._rows(statement), DetectionEvaluation, what="detection_evaluation"
        )

    # ── Mapping ───────────────────────────────────────────────────────────────

    @staticmethod
    def _to_row(evaluation: DetectionEvaluation, row_id: str) -> dict[str, Any]:
        payload = evaluation.model_dump(mode="json")
        return {
            "id": row_id,
            "schema_version": evaluation.schema_version,
            "detection_id": evaluation.detection_id,
            "rule_id": evaluation.rule_id,
            "evaluation_rule_id": evaluation.evaluation_rule_id,
            "reference_price": evaluation.reference_price.value,
            "reference_value": evaluation.reference_value,
            "context_tags": payload["context_tags"],
            # The horizons are a tuple on the model and an array inside JSONB here. Stored
            # whole because §22 freezes a horizon once COMPLETE and nothing queries into
            # one -- a report reads them all and counts.
            "horizons": {"items": payload["horizons"]},
            "updated_at": evaluation.updated_at,
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        return {
            "schema_version": data["schema_version"],
            "detection_id": data["detection_id"],
            "rule_id": data["rule_id"],
            "evaluation_rule_id": data["evaluation_rule_id"],
            "reference_price": data["reference_price"],
            "reference_value": data["reference_value"],
            "context_tags": data["context_tags"],
            "horizons": data["horizons"]["items"],
            "updated_at": data["updated_at"],
        }
