"""Setup evaluations, on PostgreSQL (§13, 12, T-7, T-9).

One row per (setup, rule), like a detection evaluation and for the same reason: a second
frozen rule can be measured over the same setups without touching the first rule's answers.

**Its own module rather than a section of ``setups.py``.** That file owns the §12
transaction, and the whole of its correctness argument is that every write inside it holds
the setup's row lock. An evaluation write is not part of the lifecycle -- it is a
measurement made afterwards, from bars the setup never saw -- and putting it in the same
module is how it would eventually acquire that lock "for consistency", turning a backfill
over a week of setups into something that blocks the observer.

**The cohort read is the method that matters.** ``before`` is the population a reference is
measured over, and it excludes the same broker day rather than merely ordering by it: a
reference built from this morning's outcome and used this afternoon is a measurement of
the future, which is the one thing §21 forbids. The Firestore version made the same
exclusion with a ``<`` on the stored ``market_date`` string; so does this one, for the same
reason -- ISO ``YYYY-MM-DD`` sorts lexicographically exactly as it sorts chronologically,
and parsing it would be the same answer with more ways to be wrong.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select

from aureon.models.base import to_utc, utc_now
from aureon.models.evaluation import SetupEvaluation
from aureon.models.setup import Setup
from aureon.storage.postgres import tables
from aureon.storage.postgres.repositories.base import PostgresRepository


def setup_evaluation_row_id(setup_id: str, rule_id: str) -> str:
    """``{setup_id}__{rule_id}`` -- the id Phase 12 already used, unchanged.

    Kept byte for byte so the Firestore export (C-12) lands on the same primary key and a
    row written before the migration is the same row after it.
    """
    if not setup_id:
        raise ValueError("setup_id must not be empty")
    if not rule_id:
        raise ValueError("rule_id must not be empty")
    return f"{setup_id}__{rule_id}"


class SetupEvaluationRepository(PostgresRepository):
    """Reads and upserts ``setup_evaluations``.

    An upsert at a deterministic id, not a transaction. Two processes cannot race here: the
    observer is the only writer, the id is a pure function of (setup, rule), and a horizon
    advanced twice produces the same numbers the second time.
    """

    table = tables.SetupEvaluation.__table__

    # ── Writing ───────────────────────────────────────────────────────────────

    def write(
        self, evaluation: SetupEvaluation, *, now: datetime | None = None
    ) -> SetupEvaluation:
        """Upsert one evaluation, stamping ``updated_at``."""
        stamped = evaluation.model_copy(update={"updated_at": to_utc(now or utc_now())})
        self._upsert(self._to_row(stamped))
        return stamped

    # ── Reading ───────────────────────────────────────────────────────────────

    def get(self, setup_id: str, rule_id: str) -> SetupEvaluation | None:
        row = self._row(setup_evaluation_row_id(setup_id, rule_id))
        if row is None:
            return None
        return SetupEvaluation.model_validate(self._to_model_dict(row))

    def get_many(
        self, setups: Sequence[Setup], rule_id: str
    ) -> dict[str, SetupEvaluation]:
        """The stored evaluations for these setups under one rule, keyed by setup id.

        One ``WHERE id = ANY(...)`` rather than the Firestore version's document-at-a-time
        loop. The loop existed there because a QUERY could silently miss a row, and a missed
        evaluation makes its setup count as unevaluated rather than as answered -- a quietly
        wrong denominator. A primary-key ``IN`` has neither failure mode.
        """
        wanted = [setup_evaluation_row_id(setup.setup_id, rule_id) for setup in setups]
        if not wanted:
            return {}
        statement = select(self.table).where(self.table.c.id.in_(wanted))
        found = self._parse_all(self._rows(statement), SetupEvaluation, what="setup_evaluation")
        return {one.setup_id: one for one in found}

    def before(self, *, symbol: str, market_date: str) -> list[SetupEvaluation]:
        """Every evaluation for one symbol on a STRICTLY EARLIER broker day (T-9).

        The population a reference is measured over. Same-day rows are excluded here as
        well as in ``prior_to`` further up, so a caller that forgot the filter still cannot
        put this morning's outcome into this afternoon's reference.
        """
        statement = (
            select(self.table)
            .where(self.table.c.symbol == symbol.upper())
            .where(self.table.c.market_date < market_date)
            .order_by(self.table.c.market_date, self.table.c.setup_id)
        )
        return self._parse_all(self._rows(statement), SetupEvaluation, what="setup_evaluation")

    def for_market_date(self, *, symbol: str, market_date: str) -> list[SetupEvaluation]:
        """Every evaluation for one symbol's day, for a review.

        On the stored ``symbol`` and ``market_date`` columns rather than by parsing the id:
        the id's shape is a storage detail, and a reader that parsed it would break the
        first time a rule id contained an underscore.
        """
        statement = (
            select(self.table)
            .where(self.table.c.symbol == symbol.upper())
            .where(self.table.c.market_date == market_date)
            .order_by(self.table.c.setup_id)
        )
        return self._parse_all(self._rows(statement), SetupEvaluation, what="setup_evaluation")

    # ── Mapping ───────────────────────────────────────────────────────────────

    @staticmethod
    def _to_row(evaluation: SetupEvaluation) -> dict[str, Any]:
        payload = evaluation.model_dump(mode="json")
        return {
            "id": setup_evaluation_row_id(evaluation.setup_id, evaluation.rule_id),
            "schema_version": evaluation.schema_version,
            "setup_id": evaluation.setup_id,
            "rule_id": evaluation.rule_id,
            "evaluation_rule_id": evaluation.evaluation_rule_id,
            # §7: cohort dimensions are columns because `before` SELECTs on them.
            "family": evaluation.family.value,
            "direction_context": evaluation.direction_context.value,
            "symbol": evaluation.symbol,
            "timeframe": evaluation.timeframe.value,
            "market_date": evaluation.market_date,
            "setup_version": evaluation.setup_version,
            "reference_price": evaluation.reference_price.value,
            "reference_value": evaluation.reference_value,
            "context_summary": payload["context_summary"],
            "horizons": {"items": payload["horizons"]},
            "updated_at": evaluation.updated_at,
        }

    @staticmethod
    def _to_model_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        data.pop("id", None)
        data["horizons"] = data["horizons"]["items"]
        return data
