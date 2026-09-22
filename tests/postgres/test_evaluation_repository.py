"""Detection evaluations through the repository (§21, §22, plan §9).

The composite key is the subject: one answer per (detection, rule), a second rule adding
rows rather than replacing them, and a re-run overwriting only its own.
"""

from __future__ import annotations

import pytest

from aureon.models.enums import HorizonStatus
from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.detections import DetectionRepository
from aureon.storage.postgres.repositories.evaluations import (
    EvaluationRepository,
    evaluation_row_id,
)
from tests.postgres.factories import a_detection, an_evaluation

pytestmark = pytest.mark.postgres


@pytest.fixture
def repos(schema: Database) -> tuple[DetectionRepository, EvaluationRepository]:
    """Both, because an evaluation needs its detection to exist (the foreign key)."""
    detections = DetectionRepository(schema)
    for name in ("d1", "d2", "d3"):
        detections.upsert(a_detection(name))
    return detections, EvaluationRepository(schema)


# ── The round trip ────────────────────────────────────────────────────────────


def test_an_evaluation_survives_the_database_unchanged(repos) -> None:
    _, evaluations = repos
    written = an_evaluation()
    evaluations.upsert(written)
    assert evaluations.get("d1", "XAU_OUTCOME_V2") == written


def test_a_complete_horizon_stays_complete_and_a_pending_one_pending(repos) -> None:
    """§22's distinction, through storage.

    A round trip that lost the status would make every pending horizon look answered --
    and a review counting those as COMPLETE reports a reached-N rate over measurements that
    have not finished, which is worse than reporting nothing.
    """
    _, evaluations = repos
    evaluations.upsert(an_evaluation())
    read = evaluations.get("d1", "XAU_OUTCOME_V2")
    assert read is not None
    assert [h.horizon_id for h in read.complete_horizons] == ["h5"]
    assert [h.horizon_id for h in read.pending_horizons] == ["h10"]
    assert read.is_fully_evaluated is False


def test_the_frozen_measurements_survive(repos) -> None:
    """The numbers a report is built from, not just their shape."""
    _, evaluations = repos
    evaluations.upsert(an_evaluation())
    read = evaluations.get("d1", "XAU_OUTCOME_V2")
    assert read is not None
    horizon = read.complete_horizons[0]
    assert horizon.mfe == 7.5
    assert horizon.mae == -2.0
    assert horizon.reached == {"3": True, "20": False}
    assert horizon.time_to == {"3": 2.0, "20": None}
    assert horizon.completed_at is not None


def test_a_missing_evaluation_reads_as_none(repos) -> None:
    _, evaluations = repos
    assert evaluations.get("d1", "NO_SUCH_RULE") is None


# ── One answer per rule, and rules do not collide ─────────────────────────────


def test_re_running_a_rule_overwrites_its_own_answer(repos) -> None:
    """The backfill re-runs over the same week whenever a horizon matures.

    A second row per re-run would make every reached-N count grow without anything having
    been observed -- the same class of error as a duplicate detection, but harder to see,
    because the number it inflates is a rate rather than a count.
    """
    _, evaluations = repos
    evaluations.upsert(an_evaluation(reference_value=2403.5))
    evaluations.upsert(an_evaluation(reference_value=2404.0))
    assert len(evaluations.for_rule("XAU_OUTCOME_V2")) == 1
    read = evaluations.get("d1", "XAU_OUTCOME_V2")
    assert read is not None and read.reference_value == 2404.0


def test_a_second_rule_adds_a_row_rather_than_replacing(repos) -> None:
    """The reason the key is composite.

    Two frozen rules describe the same detection side by side, which is what makes "did
    V2's thresholds change the conclusion" answerable from stored data instead of by
    re-deriving it and hoping the code has not moved.
    """
    _, evaluations = repos
    evaluations.upsert(an_evaluation(rule_id="XAU_OUTCOME_V2"))
    evaluations.upsert(an_evaluation(rule_id="EMA_OUTCOME_V1"))
    assert evaluations.get("d1", "XAU_OUTCOME_V2") is not None
    assert evaluations.get("d1", "EMA_OUTCOME_V1") is not None
    assert len(evaluations.for_rule("XAU_OUTCOME_V2")) == 1
    assert len(evaluations.for_rule("EMA_OUTCOME_V1")) == 1


def test_the_row_id_shape_is_the_one_phase_3_used(repos) -> None:
    """Kept identical so a Firestore export lands on the same primary key (C-12).

    If the shape changed, the export would create a second row for every evaluation and
    every rate in every review would halve.
    """
    assert evaluation_row_id("abc", "XAU_OUTCOME_V2") == "abc__XAU_OUTCOME_V2"
    with pytest.raises(ValueError):
        evaluation_row_id("", "rule")
    with pytest.raises(ValueError):
        evaluation_row_id("abc", "")


# ── The batch read ────────────────────────────────────────────────────────────


def test_get_many_returns_only_the_requested_rule(repos) -> None:
    _, evaluations = repos
    evaluations.upsert(an_evaluation("d1", "XAU_OUTCOME_V2"))
    evaluations.upsert(an_evaluation("d2", "XAU_OUTCOME_V2"))
    evaluations.upsert(an_evaluation("d3", "EMA_OUTCOME_V1"))

    found = evaluations.get_many(["d1", "d2", "d3"], "XAU_OUTCOME_V2")
    assert sorted(found) == ["d1", "d2"]


def test_get_many_omits_what_is_not_there_rather_than_inventing_it(repos) -> None:
    """An unevaluated detection must be ABSENT from the result, not present and empty.

    A caller that received an empty evaluation would count the detection as answered with
    nothing reached -- turning "we have not measured this yet" into "this one failed",
    which biases every rate downward.
    """
    _, evaluations = repos
    evaluations.upsert(an_evaluation("d1"))
    found = evaluations.get_many(["d1", "d2", "d3"], "XAU_OUTCOME_V2")
    assert list(found) == ["d1"]
    assert "d2" not in found


def test_get_many_with_no_ids_returns_nothing(repos) -> None:
    """Asking about no detections returns no evaluations, with or without the fast path.

    The guard in the repository saves a round trip; it is not protecting against invalid
    SQL, because SQLAlchemy renders ``IN ()`` as a false expression (decision 353). This
    asserts the OUTCOME, which is what a caller depends on either way -- a version that
    dropped the guard must still not hand back somebody else's rows.
    """
    _, evaluations = repos
    evaluations.upsert(an_evaluation("d1"))
    assert evaluations.get_many([], "XAU_OUTCOME_V2") == {}


def test_a_batch_write_is_one_transaction(repos) -> None:
    """A backfill that committed per row and died halfway would leave a period partly
    evaluated, with no way for the next run to tell which rows were its own."""
    _, evaluations = repos
    written = evaluations.upsert_many(
        [an_evaluation("d1"), an_evaluation("d2"), an_evaluation("d3")]
    )
    assert written == 3
    assert len(evaluations.for_rule("XAU_OUTCOME_V2")) == 3


def test_a_batch_write_that_fails_writes_nothing(repos) -> None:
    """The point of the single transaction, asserted rather than assumed.

    The third evaluation references a detection that does not exist, so the foreign key
    rejects it -- and the two valid ones before it must not survive.
    """
    from sqlalchemy.exc import IntegrityError

    _, evaluations = repos
    with pytest.raises(IntegrityError):
        evaluations.upsert_many(
            [an_evaluation("d1"), an_evaluation("d2"), an_evaluation("no-such-detection")]
        )
    assert evaluations.for_rule("XAU_OUTCOME_V2") == []


def test_an_empty_batch_is_not_an_error(repos) -> None:
    _, evaluations = repos
    assert evaluations.upsert_many([]) == 0


# ── Invalid horizons ──────────────────────────────────────────────────────────


def test_an_invalid_horizon_round_trips_as_invalid(repos) -> None:
    """§22: an INVALID horizon is a measurement that could not be made -- a gap in the
    archive, a market close inside the window. It is reported separately and never counted
    as a miss, so the status has to survive storage intact."""
    from aureon.models.evaluation import HorizonResult

    _, evaluations = repos
    evaluations.upsert(
        an_evaluation(
            horizons=(
                HorizonResult(
                    horizon_id="h5",
                    status=HorizonStatus.INVALID,
                    invalid_reason="archive gap",
                ),
            )
        )
    )
    read = evaluations.get("d1", "XAU_OUTCOME_V2")
    assert read is not None
    assert [h.horizon_id for h in read.invalid_horizons] == ["h5"]
    assert read.invalid_horizons[0].invalid_reason == "archive gap"
