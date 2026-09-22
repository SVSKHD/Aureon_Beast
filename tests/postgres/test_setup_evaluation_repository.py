"""Setup evaluations on a real server (§13, T-7, T-9, S-3b).

``before`` is the method that matters. It is the population a reference is measured over,
and it excludes the SAME broker day rather than merely ordering by it: a reference built
from this morning's outcome and used this afternoon is a measurement of the future.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.setup_evaluations import (
    SetupEvaluationRepository,
    setup_evaluation_row_id,
)
from aureon.storage.postgres.repositories.setups import SetupRepository
from tests.postgres.factories import CLOSE, a_setup, a_setup_evaluation

pytestmark = pytest.mark.postgres


@pytest.fixture
def repo(schema: Database) -> SetupEvaluationRepository:
    """The evaluations, with their setups present -- the table has a foreign key."""
    setups = SetupRepository(schema)
    for setup_id in ("s1", "s2", "s3"):
        setups.open(a_setup(setup_id))
    return SetupEvaluationRepository(schema)


# ── The id ────────────────────────────────────────────────────────────────────


def test_the_id_is_the_setup_and_the_rule() -> None:
    assert setup_evaluation_row_id("s1", "V2") == "s1__V2"


@pytest.mark.parametrize(("setup_id", "rule_id"), [("", "V2"), ("s1", "")])
def test_the_id_refuses_an_empty_part(setup_id: str, rule_id: str) -> None:
    with pytest.raises(ValueError):
        setup_evaluation_row_id(setup_id, rule_id)


# ── Round trip ────────────────────────────────────────────────────────────────


def test_an_evaluation_survives_the_database_unchanged(
    repo: SetupEvaluationRepository,
) -> None:
    written = repo.write(a_setup_evaluation(), now=CLOSE)

    assert repo.get("s1", "XAU_OUTCOME_V2") == written


def test_writing_stamps_when_it_was_written(repo: SetupEvaluationRepository) -> None:
    written = repo.write(a_setup_evaluation(), now=CLOSE)

    assert written.updated_at == CLOSE


def test_re_running_a_rule_overwrites_its_own_answer(
    repo: SetupEvaluationRepository,
) -> None:
    """A horizon advanced twice produces the same numbers the second time; a second row
    per re-run would make every reached-N count grow without anything being observed."""
    repo.write(a_setup_evaluation(reference_value=2403.5), now=CLOSE)
    repo.write(
        a_setup_evaluation(reference_value=2404.0), now=CLOSE + timedelta(minutes=5)
    )

    stored = repo.get("s1", "XAU_OUTCOME_V2")
    assert stored is not None and stored.reference_value == 2404.0


def test_a_second_rule_does_not_touch_the_firsts_answers(
    repo: SetupEvaluationRepository,
) -> None:
    repo.write(a_setup_evaluation(rule_id="V1", reference_value=1.0), now=CLOSE)
    repo.write(a_setup_evaluation(rule_id="V2", reference_value=2.0), now=CLOSE)

    first = repo.get("s1", "V1")
    assert first is not None and first.reference_value == 1.0


def test_the_horizons_come_back_as_a_sequence(repo: SetupEvaluationRepository) -> None:
    repo.write(a_setup_evaluation(), now=CLOSE)

    stored = repo.get("s1", "XAU_OUTCOME_V2")
    assert stored is not None and [h.horizon_id for h in stored.horizons] == ["h5"]


def test_an_unevaluated_setup_reads_as_none(repo: SetupEvaluationRepository) -> None:
    assert repo.get("s1", "NEVER_RUN") is None


# ── Batch read ────────────────────────────────────────────────────────────────


def test_get_many_is_keyed_by_setup(repo: SetupEvaluationRepository) -> None:
    repo.write(a_setup_evaluation("s1"), now=CLOSE)
    repo.write(a_setup_evaluation("s2"), now=CLOSE)

    found = repo.get_many([a_setup("s1"), a_setup("s2")], "XAU_OUTCOME_V2")
    assert sorted(found) == ["s1", "s2"]


def test_get_many_omits_a_setup_with_no_answer(repo: SetupEvaluationRepository) -> None:
    """Omitted rather than given a ``None``: a caller that iterates the mapping is asking
    "which of these were evaluated", and an entry present with no value answers neither."""
    repo.write(a_setup_evaluation("s1"), now=CLOSE)

    found = repo.get_many([a_setup("s1"), a_setup("s2")], "XAU_OUTCOME_V2")
    assert list(found) == ["s1"]


def test_get_many_of_nothing_reads_nothing(repo: SetupEvaluationRepository) -> None:
    assert repo.get_many([], "XAU_OUTCOME_V2") == {}


def test_get_many_does_not_cross_rules(repo: SetupEvaluationRepository) -> None:
    repo.write(a_setup_evaluation("s1", rule_id="V1"), now=CLOSE)

    assert repo.get_many([a_setup("s1")], "V2") == {}


# ── The cohort ────────────────────────────────────────────────────────────────


def test_the_cohort_excludes_the_same_broker_day(
    repo: SetupEvaluationRepository,
) -> None:
    """T-9. A reference built from this morning's outcome and used this afternoon is a
    measurement of the future, which is the one thing §21 forbids."""
    repo.write(a_setup_evaluation("s1", market_date="2026-09-21"), now=CLOSE)
    repo.write(a_setup_evaluation("s2", market_date="2026-09-22"), now=CLOSE)

    found = repo.before(symbol="XAUUSD", market_date="2026-09-22")
    assert [e.setup_id for e in found] == ["s1"]


def test_the_cohort_is_one_symbols(repo: SetupEvaluationRepository) -> None:
    repo.write(a_setup_evaluation("s1", market_date="2026-09-21"), now=CLOSE)
    repo.write(
        a_setup_evaluation("s2", symbol="XAGUSD", market_date="2026-09-21"), now=CLOSE
    )

    found = repo.before(symbol="XAGUSD", market_date="2026-09-22")
    assert [e.setup_id for e in found] == ["s2"]


def test_the_cohort_reads_oldest_first(repo: SetupEvaluationRepository) -> None:
    repo.write(a_setup_evaluation("s2", market_date="2026-09-21"), now=CLOSE)
    repo.write(a_setup_evaluation("s1", market_date="2026-09-18"), now=CLOSE)

    found = repo.before(symbol="XAUUSD", market_date="2026-09-22")
    assert [e.market_date for e in found] == ["2026-09-18", "2026-09-21"]


def test_a_days_evaluations_are_found_by_its_broker_date(
    repo: SetupEvaluationRepository,
) -> None:
    repo.write(a_setup_evaluation("s1", market_date="2026-09-21"), now=CLOSE)
    repo.write(a_setup_evaluation("s2", market_date="2026-09-22"), now=CLOSE)

    found = repo.for_market_date(symbol="XAUUSD", market_date="2026-09-22")
    assert [e.setup_id for e in found] == ["s2"]


def test_the_day_read_uses_the_columns_and_not_the_id(
    repo: SetupEvaluationRepository,
) -> None:
    """The id's shape is a storage detail; a reader that parsed it would break the first
    time a rule id contained an underscore."""
    repo.write(
        a_setup_evaluation("s1", rule_id="XAU_OUTCOME_V2", market_date="2026-09-22"),
        now=CLOSE,
    )

    found = repo.for_market_date(symbol="XAUUSD", market_date="2026-09-22")
    assert [e.rule_id for e in found] == ["XAU_OUTCOME_V2"]


def test_the_cohort_dimensions_are_columns(repo: SetupEvaluationRepository) -> None:
    """§7: ``before`` SELECTs on them, which is the definition of a filtered field. Folded
    into ``context_summary`` they would be readable only after deserialising every row."""
    from sqlalchemy import select

    from aureon.storage.postgres import tables

    repo.write(a_setup_evaluation(), now=CLOSE)
    with schema_of(repo).connect() as connection:
        row = (
            connection.execute(select(tables.SetupEvaluation.__table__))
            .mappings()
            .first()
        )

    assert row is not None
    assert row["family"] == "trend_pullback"
    assert row["direction_context"] == "bullish"
    assert row["symbol"] == "XAUUSD"
    assert row["market_date"] == "2026-09-22"


def schema_of(repo: SetupEvaluationRepository) -> Database:
    return repo.database
