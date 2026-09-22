"""Session summaries on a real server (§18, S-3b).

The one thing this table gains from the migration is a period read that is a range query
instead of a full scan. The one thing it needs from the schema is a ``timezone`` column:
``started_at`` is a ``MarketTime``, and a ``MarketTime`` cannot be rebuilt from an instant.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aureon.models.enums import SessionName
from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.sessions import (
    SessionRepository,
    session_row_id,
)
from tests.postgres.factories import CLOSE, OPEN, TZ, a_session_summary

pytestmark = pytest.mark.postgres


@pytest.fixture
def repo(schema: Database) -> SessionRepository:
    return SessionRepository(schema)


# ── The id ────────────────────────────────────────────────────────────────────


def test_the_id_is_the_broker_day_and_the_session() -> None:
    assert session_row_id("2026-09-22", "london") == "2026-09-22__london"


def test_the_id_takes_an_enum_or_its_value() -> None:
    assert session_row_id("2026-09-22", SessionName.LONDON) == session_row_id(
        "2026-09-22", SessionName.LONDON.value
    )


@pytest.mark.parametrize(("date", "session"), [("", "london"), ("2026-09-22", "")])
def test_the_id_refuses_an_empty_part(date: str, session: str) -> None:
    with pytest.raises(ValueError):
        session_row_id(date, session)


# ── Round trip ────────────────────────────────────────────────────────────────


def test_a_session_survives_the_database_unchanged(repo: SessionRepository) -> None:
    repo.upsert(a_session_summary())

    assert repo.get("2026-09-22", SessionName.LONDON) == a_session_summary()


def test_the_market_zone_comes_back_with_the_instants(repo: SessionRepository) -> None:
    """Decision 357. Derived from the running config instead, a session stored under one
    broker's clock would render itself in another's the day that config changed."""
    repo.upsert(a_session_summary())

    stored = repo.get("2026-09-22", SessionName.LONDON)
    assert stored is not None
    assert stored.started_at.market_tz == TZ
    assert stored.started_at.utc == OPEN


def test_the_zone_is_stored_and_not_guessed(repo: SessionRepository) -> None:
    from aureon.models.base import MarketTime

    repo.upsert(
        a_session_summary(
            started_at=MarketTime.from_utc(OPEN, "America/New_York"),
            ended_at=MarketTime.from_utc(CLOSE, "America/New_York"),
        )
    )

    stored = repo.get("2026-09-22", SessionName.LONDON)
    assert stored is not None and stored.started_at.market_tz == "America/New_York"


def test_regenerating_a_session_overwrites_rather_than_duplicates(
    repo: SessionRepository,
) -> None:
    """The same idempotence detections rely on: a replay of a day must be safe to re-run."""
    repo.upsert(a_session_summary(candle_count=90))
    repo.upsert(a_session_summary(candle_count=96))

    stored = repo.get("2026-09-22", SessionName.LONDON)
    assert stored is not None and stored.candle_count == 96


def test_two_sessions_of_one_day_do_not_collide(repo: SessionRepository) -> None:
    repo.upsert(a_session_summary(session=SessionName.LONDON))
    repo.upsert(a_session_summary(session=SessionName.NEW_YORK))

    assert repo.get("2026-09-22", SessionName.LONDON) is not None
    assert repo.get("2026-09-22", SessionName.NEW_YORK) is not None


def test_an_unrecorded_session_reads_as_none(repo: SessionRepository) -> None:
    assert repo.get("2026-01-01", SessionName.LONDON) is None


# ── Reading ───────────────────────────────────────────────────────────────────


def test_in_period_is_half_open(repo: SessionRepository) -> None:
    """A session starting exactly at a boundary belongs to one period; a closed interval
    would put it in both and double-count it in whichever review runs second."""
    from aureon.models.base import MarketTime

    repo.upsert(a_session_summary(session=SessionName.LONDON))
    repo.upsert(
        a_session_summary(
            session=SessionName.NEW_YORK,
            started_at=MarketTime.from_utc(CLOSE, TZ),
            ended_at=MarketTime.from_utc(CLOSE + timedelta(hours=1), TZ),
        )
    )

    found = repo.in_period(OPEN, CLOSE)
    assert [s.session for s in found] == [SessionName.LONDON]


def test_in_period_reads_in_order(repo: SessionRepository) -> None:
    from aureon.models.base import MarketTime

    repo.upsert(
        a_session_summary(
            session=SessionName.NEW_YORK,
            started_at=MarketTime.from_utc(OPEN + timedelta(hours=2), TZ),
            ended_at=MarketTime.from_utc(CLOSE + timedelta(hours=2), TZ),
        )
    )
    repo.upsert(a_session_summary(session=SessionName.LONDON))

    found = repo.in_period(OPEN, CLOSE + timedelta(days=1))
    assert [s.session for s in found] == [SessionName.LONDON, SessionName.NEW_YORK]


def test_a_days_sessions_are_found_by_its_broker_date(repo: SessionRepository) -> None:
    repo.upsert(a_session_summary("2026-09-21"))
    repo.upsert(a_session_summary("2026-09-22"))

    found = repo.for_market_date("2026-09-22")
    assert [s.market_date for s in found] == ["2026-09-22"]


def test_a_days_sessions_can_be_narrowed_to_one_symbol(repo: SessionRepository) -> None:
    repo.upsert(a_session_summary(symbol="XAUUSD", session=SessionName.LONDON))
    repo.upsert(a_session_summary(symbol="XAGUSD", session=SessionName.NEW_YORK))

    found = repo.for_market_date("2026-09-22", symbol="XAGUSD")
    assert [s.symbol for s in found] == ["XAGUSD"]
