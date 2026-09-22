"""Daily and weekly reviews on a real server (§37, §61-§63, S-3b).

Two things are worth asserting here and neither is the round trip. First, regenerating a
period must OVERWRITE rather than accumulate: a period has one answer. Second, the reader
must have no way to write, because a human interface holding an object that can rewrite a
review makes every figure built on those reviews unfalsifiable.
"""

from __future__ import annotations

import pytest

from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.reviews import (
    ReviewReader,
    ReviewRepository,
    daily_review_row_id,
    weekly_review_row_id,
)
from tests.postgres.factories import a_daily_review, a_weekly_review

pytestmark = pytest.mark.postgres


# ── The ids ───────────────────────────────────────────────────────────────────


def test_a_review_without_a_symbol_keeps_the_pre_9a_id() -> None:
    """So nothing already stored moves when the export lands (C-12)."""
    assert daily_review_row_id("2026-09-22") == "2026-09-22"


def test_the_symbol_is_a_suffix_so_the_ids_still_sort_by_date() -> None:
    """Which is what makes "the latest review" an ordered read rather than a parse."""
    ids = [daily_review_row_id(d, "xauusd") for d in ("2026-09-21", "2026-09-22")]
    assert ids == sorted(ids)
    assert ids[0] == "2026-09-21_XAUUSD"


def test_the_weekly_id_zero_pads_so_week_three_sorts_before_week_ten() -> None:
    assert weekly_review_row_id(2026, 3) < weekly_review_row_id(2026, 10)


def test_the_weekly_id_carries_the_symbol_as_a_suffix() -> None:
    assert weekly_review_row_id(2026, 39, "xauusd") == "2026-W39_XAUUSD"


def test_the_daily_id_refuses_an_empty_date() -> None:
    with pytest.raises(ValueError):
        daily_review_row_id("")


# ── Writing ───────────────────────────────────────────────────────────────────


def test_a_daily_review_survives_the_database_unchanged(schema: Database) -> None:
    repo = ReviewRepository(schema)
    repo.upsert_daily(a_daily_review())

    assert repo.get_daily("2026-09-22", "XAUUSD") == a_daily_review()


def test_a_weekly_review_survives_the_database_unchanged(schema: Database) -> None:
    repo = ReviewRepository(schema)
    repo.upsert_weekly(a_weekly_review())

    assert repo.get_weekly(2026, 39, "XAUUSD") == a_weekly_review()


def test_regenerating_a_day_overwrites_rather_than_accumulates(schema: Database) -> None:
    """A period has one answer, and if the answer changed the old one was wrong rather
    than also true."""
    repo = ReviewRepository(schema)
    repo.upsert_daily(a_daily_review(detections_total=7))
    repo.upsert_daily(a_daily_review(detections_total=9))

    stored = repo.get_daily("2026-09-22", "XAUUSD")
    assert stored is not None and stored.detections_total == 9


def test_regenerating_a_week_overwrites_rather_than_accumulates(schema: Database) -> None:
    repo = ReviewRepository(schema)
    repo.upsert_weekly(a_weekly_review(detections_total=31))
    repo.upsert_weekly(a_weekly_review(detections_total=33))

    stored = repo.get_weekly(2026, 39, "XAUUSD")
    assert stored is not None and stored.detections_total == 33


def test_two_symbols_reviews_of_one_day_do_not_collide(schema: Database) -> None:
    repo = ReviewRepository(schema)
    repo.upsert_daily(a_daily_review(symbol="XAUUSD", detections_total=7))
    repo.upsert_daily(a_daily_review(symbol="XAGUSD", detections_total=2))

    gold = repo.get_daily("2026-09-22", "XAUUSD")
    silver = repo.get_daily("2026-09-22", "XAGUSD")
    assert gold is not None and gold.detections_total == 7
    assert silver is not None and silver.detections_total == 2


def test_an_ungenerated_review_reads_as_none(schema: Database) -> None:
    repo = ReviewRepository(schema)
    assert repo.get_daily("2026-01-01", "XAUUSD") is None
    assert repo.get_weekly(2026, 1, "XAUUSD") is None


# ── Latest ────────────────────────────────────────────────────────────────────


def test_the_latest_daily_is_the_most_recent_date(schema: Database) -> None:
    repo = ReviewRepository(schema)
    repo.upsert_daily(a_daily_review("2026-09-21"))
    repo.upsert_daily(a_daily_review("2026-09-22"))

    latest = repo.latest_daily("XAUUSD")
    assert latest is not None and latest.market_date == "2026-09-22"


def test_the_latest_weekly_is_the_most_recent_week(schema: Database) -> None:
    repo = ReviewRepository(schema)
    repo.upsert_weekly(a_weekly_review(iso_week=38))
    repo.upsert_weekly(a_weekly_review(iso_week=39))

    latest = repo.latest_weekly("XAUUSD")
    assert latest is not None and latest.iso_week == 39


def test_latest_narrows_to_the_symbol_before_taking_the_maximum(
    schema: Database,
) -> None:
    """Without narrowing first, the maximum id is whichever symbol happens to sort last,
    which is not an answer to any question."""
    repo = ReviewRepository(schema)
    repo.upsert_daily(a_daily_review("2026-09-22", symbol="XAUUSD"))
    repo.upsert_daily(a_daily_review("2026-09-21", symbol="XAGUSD"))

    latest = repo.latest_daily("XAGUSD")
    assert latest is not None and latest.market_date == "2026-09-21"


def test_latest_uses_the_stored_column_rather_than_the_id(schema: Database) -> None:
    """An id is a naming convention; the column is the row's own statement about what it
    measured."""
    repo = ReviewRepository(schema)
    repo.upsert_daily(a_daily_review("2026-09-22", symbol="XAUUSD"))

    assert repo.latest_daily("XAGUSD") is None


def test_latest_for_prefers_the_weekly(schema: Database) -> None:
    """On a weekend the most recent daily covers Friday alone; the weekly is the wider
    picture."""
    repo = ReviewRepository(schema)
    repo.upsert_daily(a_daily_review())
    repo.upsert_weekly(a_weekly_review())

    latest = ReviewReader(schema).latest_for("XAUUSD")
    assert latest is not None and getattr(latest, "iso_week", None) == 39


def test_latest_for_falls_back_to_the_daily(schema: Database) -> None:
    ReviewRepository(schema).upsert_daily(a_daily_review())

    latest = ReviewReader(schema).latest_for("XAUUSD")
    assert latest is not None and getattr(latest, "market_date", None) == "2026-09-22"


def test_latest_is_none_when_nothing_has_been_generated(schema: Database) -> None:
    reader = ReviewReader(schema)
    assert reader.latest_daily() is None
    assert reader.latest_weekly() is None
    assert reader.latest_for() is None


# ── The boundary ──────────────────────────────────────────────────────────────


def test_the_reader_has_no_way_to_write_a_review() -> None:
    """The capability split is visible in the type rather than resting on nobody calling
    the wrong method. Discord imports this one."""
    writes = {"upsert_daily", "upsert_weekly"}

    assert writes.isdisjoint(dir(ReviewReader))
    assert writes.issubset(dir(ReviewRepository))


def test_the_writer_and_the_reader_agree_on_which_review_is_latest(
    schema: Database,
) -> None:
    """One implementation, delegated rather than duplicated, so they cannot diverge."""
    repo = ReviewRepository(schema)
    repo.upsert_daily(a_daily_review("2026-09-21"))
    repo.upsert_daily(a_daily_review("2026-09-22"))

    assert repo.latest_daily("XAUUSD") == ReviewReader(schema).latest_daily("XAUUSD")
