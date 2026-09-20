"""The Phase 7 gate: weekly reviews must agree with the recorded Phase 2 baseline.

``docs/PHASE2_BASELINE.md`` counts the replay one way -- a ``Counter`` over the engine's
output. A weekly review counts it another: half-open week windows on the **broker** clock,
reached counts taken from COMPLETE horizons only. Both are here, and they must produce the
same numbers.

The point is not that a document is tidy. Either path could be wrong in a way its own output
would look perfectly reasonable: a week bounded in UTC files a Sunday-evening detection under
the wrong week and every per-week figure shifts by one; a PENDING horizon folded into a
denominator turns an unknown answer into a miss. Neither shows up as an anomaly. Both show
up here, as a mismatch, immediately.

This reads the **committed** document, not a freshly generated one, so a baseline committed
while unreconciled fails even if the generator that produced it has since been fixed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureon.data.historical_provider import HistoricalDataProvider
from aureon.evaluation.backfill import run_backfill
from aureon.evaluation.rules import EMA_OUTCOME_V1
from aureon.reviews.reconcile import reconcile, week_reviews, weeks_spanned
from tests.conftest import cross_agent

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / "docs" / "PHASE2_BASELINE.md"
FIXTURE = REPO_ROOT / "aureon" / "data" / "fixtures" / "XAUUSD_M5.csv"
MARKET_TZ = "Europe/Athens"

ROW = re.compile(r"^\|\s*`?([^`|]+?)`?\s*\|(.+)\|\s*$")


def _section(name: str) -> list[str]:
    """The lines of one ``###``/``##`` section of the committed baseline."""
    lines = BASELINE.read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.strip().rstrip() == name), None
    )
    assert start is not None, f"{name} is missing from {BASELINE.name}"
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#") or line.startswith("---"):
            break
        out.append(line)
    return out


def _table(name: str) -> list[list[str]]:
    rows = []
    for line in _section(name):
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [cell.strip().strip("`").strip() for cell in line.strip("|").split("|")]
        rows.append(cells)
    return rows


def _synthetic_detection(close: datetime):
    """One cross detection at an exact instant, for boundary tests."""
    from aureon.models.base import MarketTime
    from aureon.models.detection import Detection, SessionContext
    from aureon.models.enums import Direction, SessionName, Timeframe
    from aureon.models.identity import detection_id

    return Detection(
        detection_id=detection_id(
            account_scope="primary",
            symbol="XAUUSD",
            timeframe="M5",
            candle_close=close,
            agent_name="ema_cross",
            agent_version="2.0.0",
            event_key="bullish",
        ),
        account_scope="primary",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        agent_name="ema_cross",
        agent_version="2.0.0",
        event_key="bullish",
        direction=Direction.BUY,
        detected_at=MarketTime.from_utc(close, MARKET_TZ),
        candle_open_time=MarketTime.from_utc(close - timedelta(minutes=5), MARKET_TZ),
        price=2400.0,
        session=SessionContext(session=SessionName.ASIA, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
    )


@pytest.fixture(scope="module")
def replay():
    provider = HistoricalDataProvider(FIXTURE, market_tz=MARKET_TZ)
    return run_backfill(
        provider.candles,
        [cross_agent()],
        EMA_OUTCOME_V1,
        account_scope="primary",
        market_tz=MARKET_TZ,
        point=0.01,
    )


@pytest.fixture(scope="module")
def baseline_by_day() -> dict[str, int]:
    days = {
        date: int(count)
        for date, count in _table("### By broker trading day")
        if count.isdigit()
    }
    assert days, "the baseline records no per-day detection counts"
    return days


@pytest.fixture(scope="module")
def baseline_outcomes() -> dict[str, dict[str, int]]:
    """``{horizon_id: {complete, pending, invalid, reached}}`` from the committed doc."""
    out: dict[str, dict[str, int]] = {}
    for cells in _table("## Detection outcomes (Phase 3)"):
        if cells[0] == "horizon" or len(cells) < 7:
            continue
        reached = cells[4].split("/")[0]
        out[cells[0]] = {
            "complete": int(cells[1]),
            "pending": int(cells[2]),
            "invalid": int(cells[3]),
            "reached": int(reached),
        }
    assert out, "the baseline records no horizon outcomes"
    return out


def _reconcile(replay, baseline_by_day, baseline_outcomes):
    return reconcile(
        replay.detections,
        {e.detection_id: e for e in replay.evaluations},
        EMA_OUTCOME_V1,
        market_tz=MARKET_TZ,
        by_market_date=baseline_by_day,
        complete_by_horizon={h: v["complete"] for h, v in baseline_outcomes.items()},
        reached_by_horizon={h: v["reached"] for h, v in baseline_outcomes.items()},
        pending_total=sum(v["pending"] for v in baseline_outcomes.values()),
        invalid_total=sum(v["invalid"] for v in baseline_outcomes.values()),
        threshold_key=EMA_OUTCOME_V1.threshold_keys[0],
    )


def test_weekly_reviews_reconcile_with_the_committed_baseline(
    replay, baseline_by_day, baseline_outcomes
) -> None:
    result = _reconcile(replay, baseline_by_day, baseline_outcomes)
    assert result.reconciled, "\n".join(
        ["weekly reviews disagree with docs/PHASE2_BASELINE.md:", *result.mismatches]
    )


def test_the_baseline_records_the_reconciliation(
    replay, baseline_by_day, baseline_outcomes
) -> None:
    """The document itself must state the per-week figures, not just be consistent.

    A reader trusting the baseline should be able to see the cross-check without running
    it, and a reconciliation that lived only in a test could be committed as a claim the
    document never made.
    """
    result = _reconcile(replay, baseline_by_day, baseline_outcomes)
    text = BASELINE.read_text(encoding="utf-8")
    assert "## Weekly review reconciliation (Phase 7)" in text
    assert "Reconciled: every figure above agrees." in text
    for iso in result.weeks:
        row = f"`{iso[0]}-W{iso[1]:02d}`"
        assert row in text, f"the baseline does not report week {row}"


def test_weeks_tile_without_gaps_or_overlaps(replay) -> None:
    """Every detection lands in exactly one week.

    Checked directly rather than inferred from a matching total: two errors that cancel --
    one detection lost from one week and gained by another -- leave the total intact.
    """
    reviews = week_reviews(
        replay.detections,
        {e.detection_id: e for e in replay.evaluations},
        EMA_OUTCOME_V1,
        market_tz=MARKET_TZ,
    )
    assert reviews
    counted = sum(review.detections_total for _period, review in reviews)
    assert counted == len(replay.detections)

    for detection in replay.detections:
        containing = [
            (review.iso_year, review.iso_week)
            for period, review in reviews
            if period.contains(detection.detected_at.utc)
        ]
        assert len(containing) == 1, (
            f"{detection.detection_id} at {detection.detected_at.utc.isoformat()} "
            f"falls in {len(containing)} weeks: {containing}"
        )


def test_the_weeks_come_from_the_market_clock_not_utc() -> None:
    """A detection just before broker midnight belongs to the broker's week.

    Sunday 21:00 UTC is already Monday in Athens. Derived in UTC, that whole evening
    would be filed under the ISO week that just ended, and the per-week reconciliation
    above would be off by exactly those detections while the grand total stayed right.

    The detection is placed ON the boundary deliberately rather than hoping the fixture
    has one there. An earlier version of this test compared the fixture's own crosses
    under both clocks; it stopped proving anything the moment the agent's periods changed
    and no cross happened to fall in that three-hour window. A property worth testing is
    worth constructing an input for.
    """
    from aureon.reviews.periods import week_period

    # Sunday 2026-09-20 21:00 UTC == Monday 2026-09-21 00:00 Athens (UTC+3).
    boundary = datetime(2026, 9, 20, 21, 0, tzinfo=UTC)
    detection = _synthetic_detection(boundary)

    assert detection.detected_at.market_date == "2026-09-21"
    assert boundary.isocalendar().week == 38, "the instant is still ISO week 38 in UTC"

    market_week = week_period(2026, 39, MARKET_TZ)
    utc_week = week_period(2026, 39, "UTC")

    assert market_week.contains(boundary), (
        "on the broker clock this detection belongs to week 39"
    )
    assert not utc_week.contains(boundary), (
        "on a UTC clock it would fall in week 38 -- the bug this guards"
    )
    # And the reviews built from it agree with the period, not with UTC.
    market = week_reviews([detection], {}, EMA_OUTCOME_V1, market_tz=MARKET_TZ)
    assert [(r.iso_year, r.iso_week) for _p, r in market] == [(2026, 39)]
    utc = week_reviews([detection], {}, EMA_OUTCOME_V1, market_tz="UTC")
    assert [(r.iso_year, r.iso_week) for _p, r in utc] == [(2026, 38)]


def test_the_replay_weeks_are_the_ones_the_baseline_reports(replay) -> None:
    """The fixture's own span, kept as a cheap sanity check on the ISO labelling."""
    assert weeks_spanned(replay.detections, MARKET_TZ) == [(2026, 38), (2026, 39)]


def test_a_review_of_the_replay_is_byte_identical_on_re_run(replay) -> None:
    evaluations = {e.detection_id: e for e in replay.evaluations}
    first = week_reviews(
        replay.detections, evaluations, EMA_OUTCOME_V1, market_tz=MARKET_TZ
    )
    second = week_reviews(
        replay.detections, evaluations, EMA_OUTCOME_V1, market_tz=MARKET_TZ
    )
    assert [r.model_dump_json() for _p, r in first] == [
        r.model_dump_json() for _p, r in second
    ]
