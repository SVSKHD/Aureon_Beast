"""Detections through the repository, against a real PostgreSQL (§19, plan §8).

The claim under test is the ROUND TRIP: a `Detection` written and read back is the same
detection. Everything else here is a consequence of that plus §12's identity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.models.base import MarketTime
from aureon.models.enums import Direction, Timeframe
from aureon.storage.postgres.database import Database
from aureon.storage.postgres.repositories.detections import (
    DetectionRepository,
    market_date_of,
)
from tests.postgres.factories import CLOSE, TZ, a_detection

pytestmark = pytest.mark.postgres


@pytest.fixture
def repo(schema: Database) -> DetectionRepository:
    return DetectionRepository(schema)


# ── The round trip ────────────────────────────────────────────────────────────


def test_a_detection_survives_the_database_unchanged(repo: DetectionRepository) -> None:
    """Equality on the whole model, not field by field.

    A per-field assertion passes while a field nobody thought to check is silently
    dropped -- and the fields most likely to be dropped are the newest ones, which is
    exactly where a migration loses data.
    """
    written = a_detection()
    repo.upsert(written)
    assert repo.get("d1") == written


def test_the_optional_context_blocks_survive_too(repo: DetectionRepository) -> None:
    """§9B and 11D hang whole objects off a detection, stored as JSONB.

    A round trip that only ever saw ``None`` in those columns would prove nothing about
    the ones that carry a volume profile, a volatility read or an MTF context.
    """
    from aureon.models.mtf import MtfContext
    from aureon.models.profile import VolatilityContext

    written = a_detection(
        volatility=VolatilityContext(atr_14=3.2, atr_points=320.0, regime="normal"),
        mtf=MtfContext(ema_fast_period=20, ema_slow_period=50),
    )
    repo.upsert(written)
    read = repo.get("d1")
    assert read == written
    # And a value from inside the JSONB, so the assertion is not satisfied by two
    # equally-empty objects comparing equal.
    assert read is not None and read.volatility is not None
    assert read.volatility.atr_14 == 3.2
    assert read.volatility.regime == "normal"
    assert read.mtf is not None and read.mtf.ema_slow_period == 50


def test_a_missing_detection_reads_as_none(repo: DetectionRepository) -> None:
    assert repo.get("never-written") is None
    assert repo.exists("never-written") is False


# ── §12: the id is the idempotency ────────────────────────────────────────────


def test_writing_the_same_detection_twice_leaves_one(repo: DetectionRepository) -> None:
    """The outbox may deliver twice after an ambiguous first attempt.

    That is safe only because the id is a pure function of the candle, so the retry
    addresses the same row. If this appended, a restart mid-delivery would double every
    count in the next review.
    """
    written = a_detection()
    repo.upsert(written)
    repo.upsert(written)
    assert len(repo.in_period(CLOSE - timedelta(hours=1), CLOSE + timedelta(hours=1))) == 1


def test_a_payload_delivery_is_the_same_write(repo: DetectionRepository) -> None:
    """The outbox stores payloads, not models, so a queued detection can outlive the code
    version that queued it."""
    written = a_detection()
    repo.upsert(written)
    repo.upsert_payload(written.model_dump(mode="json"))
    assert repo.get("d1") == written


def test_a_payload_that_no_longer_parses_is_refused(repo: DetectionRepository) -> None:
    """Rather than writing a partial row.

    A row missing the fields a newer model requires is worse than a failed delivery: the
    delivery would be retried, and the row would be read for years as though complete.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        repo.upsert_payload({"detection_id": "broken"})
    assert repo.get("broken") is None


# ── Time, and the broker day ──────────────────────────────────────────────────


def test_the_two_instants_are_kept_apart(repo: DetectionRepository) -> None:
    """C-6. The bar's open and its close are different instants and both are stored.

    ``detected_at`` is the CLOSE -- the moment the detection became knowable, and what
    §12 hashes. A round trip that collapsed them would still look correct on a five-minute
    candle unless the test checks the values.
    """
    repo.upsert(a_detection())
    read = repo.get("d1")
    assert read is not None
    assert read.detected_at.utc == CLOSE
    assert read.candle_open_time.utc == CLOSE - timedelta(minutes=5)
    assert read.detected_at.utc != read.candle_open_time.utc


def test_the_market_zone_comes_back_on_both_times(repo: DetectionRepository) -> None:
    """The rendering is derived, so the zone has to survive or it cannot be."""
    repo.upsert(a_detection())
    read = repo.get("d1")
    assert read is not None
    assert read.detected_at.market_tz == TZ
    assert read.candle_open_time.market_tz == TZ


def test_the_broker_day_is_derived_not_the_utc_day() -> None:
    """§8. A broker day is not a UTC day, and a review that assumed so loses the edges.

    22:30 UTC is already tomorrow in Athens. A detection at that instant belongs to the
    NEXT broker day, and counting it under today would move it between two reviews.
    """
    assert market_date_of(datetime(2026, 9, 22, 22, 30, tzinfo=UTC), TZ) == "2026-09-23"
    assert market_date_of(datetime(2026, 9, 22, 0, 30, tzinfo=UTC), TZ) == "2026-09-22"


def test_a_day_query_uses_the_broker_day(repo: DetectionRepository) -> None:
    late = a_detection(
        "late",
        detected_at=MarketTime.from_utc(datetime(2026, 9, 22, 22, 30, tzinfo=UTC), TZ),
        candle_open_time=MarketTime.from_utc(datetime(2026, 9, 22, 22, 25, tzinfo=UTC), TZ),
    )
    repo.upsert(a_detection())  # 13:10 UTC -> 2026-09-22 in Athens
    repo.upsert(late)  # 22:30 UTC -> 2026-09-23 in Athens

    assert [d.detection_id for d in repo.for_market_date("XAUUSD", "2026-09-22")] == ["d1"]
    assert [d.detection_id for d in repo.for_market_date("XAUUSD", "2026-09-23")] == ["late"]


# ── Queries ───────────────────────────────────────────────────────────────────


def test_a_period_is_half_open(repo: DetectionRepository) -> None:
    """``[start, end)``. A closed interval double-counts the boundary candle across two
    adjacent periods, which is how a weekly total exceeds the sum of its days."""
    repo.upsert(a_detection())
    assert repo.in_period(CLOSE, CLOSE + timedelta(minutes=1)) != []
    assert repo.in_period(CLOSE - timedelta(minutes=1), CLOSE) == []


def test_recent_for_symbol_is_newest_first_and_bounded(repo: DetectionRepository) -> None:
    """The Discord selector shows a few, most recent first."""
    for index in range(5):
        repo.upsert(
            a_detection(
                f"d{index}",
                detected_at=MarketTime.from_utc(CLOSE + timedelta(minutes=5 * index), TZ),
            )
        )
    recent = repo.recent_for_symbol("XAUUSD", limit=3)
    assert [d.detection_id for d in recent] == ["d4", "d3", "d2"]


def test_recent_for_symbol_does_not_mix_symbols(repo: DetectionRepository) -> None:
    """9A runs two instruments in one process and one database."""
    repo.upsert(a_detection("gold"))
    repo.upsert(a_detection("silver", symbol="XAGUSD", price=31.2))
    assert [d.detection_id for d in repo.recent_for_symbol("XAGUSD")] == ["silver"]


def test_a_period_query_is_ordered_oldest_first(repo: DetectionRepository) -> None:
    """A review walks a period forwards; an unordered read makes "the first detection of
    the day" whatever the planner happened to return."""
    for index in reversed(range(3)):
        repo.upsert(
            a_detection(
                f"d{index}",
                detected_at=MarketTime.from_utc(CLOSE + timedelta(minutes=5 * index), TZ),
            )
        )
    found = repo.in_period(CLOSE - timedelta(hours=1), CLOSE + timedelta(hours=1))
    assert [d.detection_id for d in found] == ["d0", "d1", "d2"]


# ── Immutability ──────────────────────────────────────────────────────────────


def test_the_repository_offers_no_way_to_edit_a_detection(
    repo: DetectionRepository,
) -> None:
    """CLAUDE.md: detections are immutable; outcomes go in ``detection_evaluations``.

    Asserted on the SURFACE rather than by trying an edit, because the guarantee is that
    no such method exists -- a caller cannot reach for what is not there, and a reviewer
    adding one has to notice this test.
    """
    forbidden = {"update", "set_outcome", "mark", "edit", "patch", "delete", "remove"}
    offered = {name for name in dir(repo) if not name.startswith("_")}
    assert not (forbidden & offered), sorted(forbidden & offered)


def test_an_upsert_of_a_changed_detection_overwrites_rather_than_appends(
    repo: DetectionRepository,
) -> None:
    """The id is the candle, so a re-observation of the same candle IS the same detection.

    If the agent's reading changed between the two writes, the id would have changed too
    (``agent_version`` is a component), so a differing payload at the same id means a
    correction, not a second event.
    """
    repo.upsert(a_detection(price=2403.5))
    repo.upsert(a_detection(price=2404.0))
    found = repo.in_period(CLOSE - timedelta(hours=1), CLOSE + timedelta(hours=1))
    assert len(found) == 1
    assert found[0].price == 2404.0


def test_a_version_bump_forks_history_rather_than_overwriting(
    repo: DetectionRepository,
) -> None:
    """Decision 120 / §12: ``agent_version`` is part of the id.

    So ema_cross 1.0.0 and 2.0.0 describe the same candle side by side instead of one
    silently replacing the other -- which is what makes "did the new parameters do better"
    answerable at all.
    """
    repo.upsert(a_detection("v1", agent_version="1.0.0"))
    repo.upsert(a_detection("v2", agent_version="2.0.0"))
    found = repo.in_period(CLOSE - timedelta(hours=1), CLOSE + timedelta(hours=1))
    assert sorted(d.agent_version for d in found) == ["1.0.0", "2.0.0"]


def test_a_detection_with_no_direction_round_trips(repo: DetectionRepository) -> None:
    """Not every agent emits one; a sweep is not a side."""
    repo.upsert(a_detection(direction=None))
    read = repo.get("d1")
    assert read is not None and read.direction is None


@pytest.mark.parametrize("direction", [Direction.BUY, Direction.SELL])
def test_both_directions_round_trip(repo: DetectionRepository, direction: Direction) -> None:
    repo.upsert(a_detection(direction=direction))
    read = repo.get("d1")
    assert read is not None and read.direction is direction


def test_the_timeframe_round_trips_as_an_enum(repo: DetectionRepository) -> None:
    """Stored as its string value; a read that returned the raw string would compare
    unequal to every ``Timeframe`` the engine uses."""
    repo.upsert(a_detection())
    read = repo.get("d1")
    assert read is not None and read.timeframe is Timeframe.M5
