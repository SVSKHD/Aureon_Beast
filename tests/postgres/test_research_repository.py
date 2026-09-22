"""Ops events, assessments and trade notes on a real server (11A F-15, 9D, S-3b).

Three tables nothing gates on, so no transaction and no race to prove. What they do have
is C-13: ``history_source`` is a column precisely so a readout measured over generated bars
can be EXCLUDED by a query rather than by a caller remembering to check.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aureon.models.enums import HistorySource
from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.research import (
    AssessmentRepository,
    OpsEventRepository,
    TradeNoteRepository,
    ops_event_id,
)
from tests.postgres.factories import CLOSE, OPEN, an_assessment, an_ops_event

pytestmark = pytest.mark.postgres


# ── The ops event id ──────────────────────────────────────────────────────────


def test_an_unscoped_condition_is_named_by_itself() -> None:
    assert ops_event_id("database_unavailable") == "database_unavailable"


def test_a_scoped_condition_carries_its_scope() -> None:
    assert ops_event_id("observer_stale", "xauusd") == "observer_stale__XAUUSD"


def test_the_condition_id_refuses_an_empty_name() -> None:
    with pytest.raises(ValueError):
        ops_event_id("")


# ── Ops events ────────────────────────────────────────────────────────────────


def test_a_condition_survives_the_database_unchanged(schema: Database) -> None:
    repo = OpsEventRepository(schema)
    written = repo.write(an_ops_event(), now=CLOSE)

    assert repo.get("observer_stale", "XAUUSD") == written


def test_the_row_is_keyed_by_what_the_condition_is_about(schema: Database) -> None:
    """Derived rather than taken from ``event_id``: a model carrying an id that disagrees
    with its own name would write a row nothing can read back."""
    repo = OpsEventRepository(schema)
    repo.write(an_ops_event(event_id="something_else"), now=CLOSE)

    stored = repo.get("observer_stale", "XAUUSD")
    assert stored is not None and stored.event_id == "observer_stale__XAUUSD"


def test_re_announcing_a_condition_addresses_the_same_row(schema: Database) -> None:
    """A service that restarts mid-condition finds the existing row and does not
    re-announce -- the difference between one alert and one per restart."""
    repo = OpsEventRepository(schema)
    repo.write(an_ops_event(), now=CLOSE)
    repo.write(an_ops_event(detail="still stale"), now=CLOSE + timedelta(minutes=1))

    assert len(repo.all_events()) == 1
    stored = repo.get("observer_stale", "XAUUSD")
    assert stored is not None and stored.detail == "still stale"


def test_the_detail_is_the_line_an_operator_reads(schema: Database) -> None:
    """A string column, not a payload: JSONB would round-trip correctly and lie about the
    shape to everything that inspects the schema (decision 358)."""
    repo = OpsEventRepository(schema)
    repo.write(an_ops_event(detail="no candle for 3 intervals"), now=CLOSE)

    stored = repo.get("observer_stale", "XAUUSD")
    assert stored is not None and stored.detail == "no candle for 3 intervals"


def test_active_returns_only_what_is_currently_true(schema: Database) -> None:
    repo = OpsEventRepository(schema)
    repo.write(an_ops_event("observer_stale"), now=CLOSE)
    repo.write(an_ops_event("feed_stale", active=False), now=CLOSE)

    assert [e.name for e in repo.active()] == ["observer_stale"]


def test_a_cleared_condition_is_kept_rather_than_deleted(schema: Database) -> None:
    """"This has not happened since the deployment started" and "this happened twice this
    morning and cleared" are different things for an operator to know."""
    repo = OpsEventRepository(schema)
    repo.write(an_ops_event(), now=CLOSE)
    repo.write(an_ops_event(active=False, onsets=2), now=CLOSE + timedelta(minutes=5))

    assert repo.active() == []
    assert [e.onsets for e in repo.all_events()] == [2]


def test_all_events_puts_the_active_ones_first(schema: Database) -> None:
    repo = OpsEventRepository(schema)
    repo.write(an_ops_event("aaa_cleared", active=False), now=CLOSE)
    repo.write(an_ops_event("zzz_live", active=True), now=CLOSE)

    assert [e.name for e in repo.all_events()] == ["zzz_live", "aaa_cleared"]


def test_the_onset_count_is_the_services_to_keep(schema: Database) -> None:
    """A counter incremented by the storage layer would count re-announcements as
    recurrences, which is the one number this row exists to get right."""
    repo = OpsEventRepository(schema)
    repo.write(an_ops_event(onsets=3), now=CLOSE)
    repo.write(an_ops_event(onsets=3), now=CLOSE + timedelta(minutes=1))

    stored = repo.get("observer_stale", "XAUUSD")
    assert stored is not None and stored.onsets == 3


def test_an_unrecorded_condition_reads_as_none(schema: Database) -> None:
    assert OpsEventRepository(schema).get("never_happened") is None


# ── Assessments ───────────────────────────────────────────────────────────────


def test_an_assessment_survives_the_database_unchanged(schema: Database) -> None:
    repo = AssessmentRepository(schema)
    written = repo.store(an_assessment())

    assert repo.get("as1") == written


def test_a_readout_for_one_detection_is_found_by_it(schema: Database) -> None:
    repo = AssessmentRepository(schema)
    repo.store(an_assessment("as1", detection_id="d1"))
    repo.store(an_assessment("as2", detection_id="d2"))

    assert [a.assessment_id for a in repo.for_detection("d1")] == ["as1"]


def test_the_latest_readout_for_a_detection_is_the_newest(schema: Database) -> None:
    repo = AssessmentRepository(schema)
    repo.store(an_assessment("early", created_at=OPEN))
    repo.store(an_assessment("late", created_at=CLOSE))

    latest = repo.latest_for_detection("d1")
    assert latest is not None and latest.assessment_id == "late"


def test_the_latest_readout_for_a_symbol_is_the_newest(schema: Database) -> None:
    repo = AssessmentRepository(schema)
    repo.store(an_assessment("early", created_at=OPEN))
    repo.store(an_assessment("late", created_at=CLOSE))
    repo.store(an_assessment("other", symbol="XAGUSD", created_at=CLOSE))

    latest = repo.latest_for_symbol("XAUUSD")
    assert latest is not None and latest.assessment_id == "late"


def test_in_period_is_half_open(schema: Database) -> None:
    repo = AssessmentRepository(schema)
    repo.store(an_assessment("start", created_at=OPEN))
    repo.store(an_assessment("end", created_at=CLOSE))

    found = [a.assessment_id for a in repo.in_period(OPEN, CLOSE)]
    assert found == ["start"]


def test_in_period_can_be_narrowed_to_one_symbol(schema: Database) -> None:
    repo = AssessmentRepository(schema)
    repo.store(an_assessment("gold", created_at=OPEN))
    repo.store(an_assessment("silver", symbol="XAGUSD", created_at=OPEN))

    found = repo.in_period(OPEN, CLOSE, symbol="XAGUSD")
    assert [a.assessment_id for a in found] == ["silver"]


def test_a_synthetic_cohort_is_excluded_from_the_real_history_read(
    schema: Database,
) -> None:
    """C-13. A readout built on replayed fixture bars and one built on bars a broker served
    are the same arithmetic over incomparable data."""
    repo = AssessmentRepository(schema)
    repo.store(an_assessment("real", history_source=HistorySource.REAL, created_at=OPEN))
    repo.store(
        an_assessment("fake", history_source=HistorySource.SYNTHETIC, created_at=OPEN)
    )

    found = repo.real_history_only(OPEN, CLOSE)
    assert [a.assessment_id for a in found] == ["real"]


def test_unknown_provenance_is_not_evidence_of_real_history(schema: Database) -> None:
    """The Firestore export stamps ``unknown`` on rows that predate the field (C-13).
    "Nobody recorded it" must not read as "a broker served it"."""
    repo = AssessmentRepository(schema)
    repo.store(
        an_assessment("old", history_source=HistorySource.UNKNOWN, created_at=OPEN)
    )

    assert repo.real_history_only(OPEN, CLOSE) == []


def test_provenance_is_a_column_and_not_only_inside_the_payload(
    schema: Database,
) -> None:
    """The whole reason C-13 asks for a column: a flag readable only after deserialising
    is one the filter cannot use, and one that gets forgotten."""
    from sqlalchemy import select

    from aureon.storage.postgres import tables

    AssessmentRepository(schema).store(an_assessment(real_days=44))
    with schema.connect() as connection:
        row = connection.execute(select(tables.Assessment.__table__)).mappings().first()

    assert row is not None
    assert row["history_source"] == HistorySource.REAL.value
    assert row["real_days"] == 44


def test_an_unrecorded_assessment_reads_as_none(schema: Database) -> None:
    assert AssessmentRepository(schema).get("nobody") is None


# ── Trade notes ───────────────────────────────────────────────────────────────


def test_a_note_survives_the_database_unchanged(schema: Database) -> None:
    repo = TradeNoteRepository(schema)
    written = repo.add("t1", author="trader", text="  half off at the first target  ")

    assert repo.get(written.note_id) == written
    assert written.text == "half off at the first target"


def test_notes_for_a_trade_read_in_the_order_they_were_written(
    schema: Database,
) -> None:
    repo = TradeNoteRepository(schema)
    first = repo.add("t1", author="trader", text="entered", now=OPEN)
    second = repo.add("t1", author="trader", text="closed", now=CLOSE)
    repo.add("t2", author="trader", text="different trade", now=OPEN)

    assert [n.note_id for n in repo.for_trade("t1")] == [first.note_id, second.note_id]


def test_notes_for_several_trades_come_back_grouped(schema: Database) -> None:
    repo = TradeNoteRepository(schema)
    repo.add("t1", author="trader", text="one", now=OPEN)
    repo.add("t2", author="trader", text="two", now=OPEN)

    grouped = repo.for_trades(["t1", "t2", "t3"])
    assert [len(grouped[key]) for key in ("t1", "t2", "t3")] == [1, 1, 0]


def test_a_trade_with_no_notes_still_gets_its_empty_list(schema: Database) -> None:
    """So a caller that iterates the mapping does not have to know which were missing."""
    assert TradeNoteRepository(schema).for_trades(["t9"]) == {"t9": []}


def test_asking_for_no_trades_reads_nothing(schema: Database) -> None:
    assert TradeNoteRepository(schema).for_trades([]) == {}


def test_notes_in_a_period_are_half_open(schema: Database) -> None:
    repo = TradeNoteRepository(schema)
    repo.add("t1", author="trader", text="inside", now=OPEN)
    repo.add("t1", author="trader", text="on the boundary", now=CLOSE)

    assert [n.text for n in repo.in_period(OPEN, CLOSE)] == ["inside"]


def test_a_note_never_touches_the_trade_row(schema: Database) -> None:
    """§58: a closed trade is frozen. Its own table means a note cannot be mistaken for a
    field write by anything that iterates a record's parts."""
    from sqlalchemy import select

    from aureon.storage.postgres import tables

    TradeNoteRepository(schema).add("t1", author="trader", text="anything")
    with schema.connect() as connection:
        trades = connection.execute(select(tables.Trade.__table__)).mappings().all()

    assert list(trades) == []
