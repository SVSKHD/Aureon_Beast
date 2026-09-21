"""Where a readout's history came from, on the document and on the screen (11C, F-9).

Every rate `/monitor` publishes is a measured frequency over a cohort of prior detections.
Until 11C nothing on the stored assessment said whether those detections came from bars a
broker served or from bars ``scripts/gen_fixtures.py`` generated -- and a generated random
walk has the distribution its generator was given, so a hit rate measured over one is a
statement about the generator.

The four states, and why each is separate:

* **REAL** -- every member from a broker day with a VERIFIED session. The only one that is
  evidence about the instrument.
* **SYNTHETIC** -- none of them. Every number is about the fixture.
* **MIXED** -- some of each, and the hardest to read silently: the rate is a weighted average
  of a real frequency and a generated one and nothing about it looks unusual.
* **UNKNOWN** -- nobody recorded it. Not the same as SYNTHETIC, because collapsing them would
  quietly relabel every readout written before the field existed.

And separately from all four: **immature**, meaning real but spanning too few days. Two
hundred detections from one Tuesday are not two hundred independent observations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureon.models.assessment import MATURE_REAL_DAYS, MIN_COHORT, Assessment
from aureon.models.base import MarketTime
from aureon.models.enums import Direction, HistorySource, Timeframe, TrendBias
from aureon.models.market import Candle
from aureon.services.assessment_service import classify_history
from aureon.services.session_evidence import VERIFIED_MARKER, verified_market_dates

MARKET_TZ = "Europe/Athens"
SYMBOL = "XAUUSD"
# 08:00 UTC is 11:00 in Athens, safely inside one broker day.
NOON = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)


# ── Which days really happened ───────────────────────────────────────────────


def write_evidence(root: Path, symbol: str, market_date: str, *, verified: bool) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"session_{market_date}_{symbol}.md"
    body = f"# session {market_date} {symbol}\n\n"
    path.write_text(
        body + (f"{VERIFIED_MARKER} — 6 pass\n" if verified else "_Not verified yet._\n"),
        encoding="utf-8",
    )
    return path


def test_only_a_verified_session_counts_as_a_real_day(tmp_path: Path) -> None:
    """A file is written when a session STARTS, so "there is a file" and "it was verified"
    are different claims and only the second one is evidence."""
    write_evidence(tmp_path, SYMBOL, "2026-09-16", verified=True)
    write_evidence(tmp_path, SYMBOL, "2026-09-17", verified=False)
    assert verified_market_dates(SYMBOL, root=tmp_path) == ("2026-09-16",)


def test_another_symbols_verified_session_is_not_this_ones(tmp_path: Path) -> None:
    """Thresholds are in points and points are different money per instrument (decision
    141), so a verified gold day says nothing about silver."""
    write_evidence(tmp_path, "XAUUSD", "2026-09-16", verified=True)
    assert verified_market_dates("XAGUSD", root=tmp_path) == ()
    assert verified_market_dates("XAUUSD", root=tmp_path) == ("2026-09-16",)


def test_no_evidence_directory_is_no_days_rather_than_an_error(tmp_path: Path) -> None:
    assert verified_market_dates(SYMBOL, root=tmp_path / "nothing") == ()


def test_this_repository_has_no_verified_days(tmp_path: Path) -> None:
    """The state of things, pinned. Nothing has run against a real terminal here, so every
    cohort this repository can build is synthetic -- and a test that said otherwise would be
    the first sign somebody had written an evidence file by hand."""
    assert verified_market_dates("XAUUSD") == ()
    assert verified_market_dates("XAGUSD") == ()


# ── Classifying a cohort ─────────────────────────────────────────────────────


def member(market_date: str, *, index: int = 0):
    """One cohort member on a given broker date. Only the date is load-bearing here."""
    from aureon.models.detection import (
        Detection,
        IndicatorSnapshot,
        SessionContext,
    )
    from aureon.models.enums import SessionName
    from aureon.models.evaluation import DetectionEvaluation, ReferencePrice
    from aureon.services.assessment_service import CohortMember

    opened = datetime.fromisoformat(f"{market_date}T08:00:00+00:00") + timedelta(
        minutes=5 * index
    )
    detection = Detection(
        detection_id=f"d-{market_date}-{index}",
        account_scope="primary",
        symbol=SYMBOL,
        timeframe=Timeframe.M5,
        candle_open_time=MarketTime.from_utc(opened, MARKET_TZ),
        detected_at=MarketTime.from_utc(opened + timedelta(minutes=5), MARKET_TZ),
        agent_name="ema_cross",
        agent_version="2.1.0",
        event_key="bullish",
        direction=Direction.BUY,
        price=2400.0,
        indicators=IndicatorSnapshot(ema={"fast": 2401.0, "slow": 2395.0}, rsi=61.4),
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        sequence_today=1 + index,
        sequence_session=1 + index,
    )
    evaluation = DetectionEvaluation(
        detection_id=detection.detection_id,
        rule_id="XAU_OUTCOME_V2",
        reference_price=ReferencePrice.NEXT_OPEN,
    )
    return CohortMember(detection=detection, evaluation=evaluation)


def test_a_cohort_wholly_inside_verified_days_is_real() -> None:
    cohort = [member("2026-09-16"), member("2026-09-17", index=1)]
    source, days = classify_history(cohort, {"2026-09-16", "2026-09-17"})
    assert source is HistorySource.REAL
    assert days == 2


def test_a_cohort_with_no_verified_day_is_synthetic() -> None:
    cohort = [member("2026-09-16"), member("2026-09-17", index=1)]
    source, days = classify_history(cohort, set())
    assert source is HistorySource.SYNTHETIC
    assert days == 0


def test_a_cohort_with_some_of_each_is_mixed() -> None:
    """The one that most needs its own label: the rate is a weighted average of a real
    frequency and a generated one, and nothing about it looks unusual."""
    cohort = [member("2026-09-16"), member("2026-09-17", index=1)]
    source, days = classify_history(cohort, {"2026-09-16"})
    assert source is HistorySource.MIXED
    assert days == 1


def test_a_caller_that_did_not_say_gets_UNKNOWN_and_not_synthetic() -> None:
    """A caller not yet wired up and a cohort we know was generated are different facts.
    Labelling the first as the second would quietly relabel every readout written before
    this field existed."""
    source, days = classify_history([member("2026-09-16")], None)
    assert source is HistorySource.UNKNOWN
    assert days == 0


def test_an_empty_cohort_is_unknown_rather_than_synthetic() -> None:
    """No members means no bars, so there is nothing to be real or synthetic about.
    SYNTHETIC would be a claim about data that does not exist."""
    assert classify_history([], set()) == (HistorySource.UNKNOWN, 0)


def test_many_members_on_one_day_count_as_one_day() -> None:
    """The whole point of counting DAYS: two hundred detections from one Tuesday share a
    session, a volatility regime and one day's news."""
    cohort = [member("2026-09-16", index=i) for i in range(50)]
    source, days = classify_history(cohort, {"2026-09-16"})
    assert source is HistorySource.REAL
    assert days == 1


# ── On the stored document ───────────────────────────────────────────────────


def assessment(**overrides) -> Assessment:
    from aureon.models.assessment import CohortFilter, TrendRead

    base = dict(
        assessment_id="a-1",
        detection_id="d-1",
        symbol=SYMBOL,
        rule_id="XAU_OUTCOME_V2",
        trend_read=TrendRead(bias=TrendBias.BULLISH),
        cohort_filter=CohortFilter(
            symbol=SYMBOL, agent_name="ema_cross", direction=Direction.BUY
        ),
        n=MIN_COHORT,
    )
    return Assessment(**(base | overrides))


def test_an_assessment_defaults_to_unknown_provenance() -> None:
    """So a document written before this field existed reads back as "not recorded" rather
    than as a claim."""
    stored = assessment()
    assert stored.history_source is HistorySource.UNKNOWN
    assert stored.real_days == 0


@pytest.mark.parametrize(
    ("days", "immature"),
    [(0, True), (MATURE_REAL_DAYS - 1, True), (MATURE_REAL_DAYS, False)],
)
def test_maturity_is_about_days_not_cohort_size(days: int, immature: bool) -> None:
    """A readout can clear the cohort floor and still be immature, and early on that is the
    common case."""
    stored = assessment(
        history_source=HistorySource.REAL, real_days=days, n=500, insufficient=False
    )
    assert stored.immature is immature
    assert not stored.insufficient, "which is a different question, about n"


def test_only_a_real_source_is_evidence() -> None:
    assert HistorySource.REAL.is_evidence
    for other in (HistorySource.SYNTHETIC, HistorySource.MIXED, HistorySource.UNKNOWN):
        assert not other.is_evidence, other


# ── On the screen ────────────────────────────────────────────────────────────


def line_for(**overrides) -> str | None:
    from aureon.discord.service import history_line

    return history_line(assessment(**overrides))


def test_a_mature_real_cohort_needs_no_caveat() -> None:
    assert line_for(history_source=HistorySource.REAL, real_days=MATURE_REAL_DAYS) is None


def test_a_synthetic_cohort_says_it_describes_the_generator() -> None:
    line = line_for(history_source=HistorySource.SYNTHETIC)
    assert line is not None and "SYNTHETIC" in line
    assert "generator" in line


def test_a_mixed_cohort_says_the_rate_is_a_weighted_average() -> None:
    line = line_for(history_source=HistorySource.MIXED, real_days=3)
    assert line is not None and "MIXED" in line
    assert "3 verified broker day" in line


def test_an_immature_real_cohort_says_how_few_days() -> None:
    line = line_for(history_source=HistorySource.REAL, real_days=2)
    assert line is not None
    assert "2 verified broker day" in line and "immature" in line


def test_an_unrecorded_cohort_says_so_rather_than_guessing() -> None:
    line = line_for(history_source=HistorySource.UNKNOWN)
    assert line is not None and "not recorded" in line


def test_the_caveat_is_in_the_description_above_the_numbers() -> None:
    """Not the footer. "Every rate here describes the generator" is not something a reader
    should meet after they have already read the rates -- the same reasoning that put 9C's
    and 9D's labels where they are (decisions 188, 204)."""
    from aureon.discord.embeds import monitor_embed
    from aureon.discord.service import build_monitor
    from tests.unit.test_history_source import member as make_member

    subject = make_member("2026-09-16").detection
    screen = build_monitor(
        assessment(history_source=HistorySource.SYNTHETIC, insufficient=True), subject
    )
    assert screen.history is not None
    embed = monitor_embed(screen)
    assert "SYNTHETIC" in (embed.description or "")


# ── In the weekly review ─────────────────────────────────────────────────────


def test_the_weekly_scorecard_splits_the_hit_rate_by_source() -> None:
    """The merged rate is an average over incomparable populations until this is read."""
    from aureon.models.enums import HorizonStatus
    from aureon.models.evaluation import (
        DetectionEvaluation,
        HorizonResult,
        ReferencePrice,
    )
    from aureon.reviews.assessment_scoring import score_assessments

    def scored(detection_id: str, *, reached: bool, source: HistorySource) -> Assessment:
        from aureon.models.assessment import Estimate

        return assessment(
            assessment_id=f"a-{detection_id}",
            detection_id=detection_id,
            history_source=source,
            real_days=MATURE_REAL_DAYS if source is HistorySource.REAL else 0,
            tp_estimates=(Estimate(quantile=0.5, points=100.0, price=2401.0),),
            sl_estimates=(Estimate(quantile=0.75, points=200.0, price=2398.0),),
        )

    def outcome(detection_id: str, *, mfe: float, mae: float) -> DetectionEvaluation:
        return DetectionEvaluation(
            detection_id=detection_id,
            rule_id="XAU_OUTCOME_V2",
            reference_price=ReferencePrice.NEXT_OPEN,
            horizons=(
                HorizonResult(
                    horizon_id="60m",
                    status=HorizonStatus.COMPLETE,
                    mfe=mfe,
                    mae=mae,
                    # A COMPLETE horizon carries the raw extremes too (decision 13): the
                    # excursions are direction-signed and these are not, and a horizon that
                    # claimed to be complete without them could not be re-derived.
                    future_high=2400.0 + mfe / 100.0,
                    future_low=2400.0 + mae / 100.0,
                    # A COMPLETE horizon also has an answer for every threshold the rule
                    # names, and one for a threshold it does not name would be a claim about
                    # a rule that never asked. The scoring here reads the excursions, not
                    # these, but the model refuses an incomplete COMPLETE either way.
                    reached={"5.0": mfe >= 500.0},
                    completed_at=datetime.fromisoformat("2026-09-16T09:00:00+00:00"),
                ),
            ),
        )

    assessments = [
        scored("d-real-hit", reached=True, source=HistorySource.REAL),
        scored("d-real-miss", reached=False, source=HistorySource.REAL),
        scored("d-fake-hit", reached=True, source=HistorySource.SYNTHETIC),
    ]
    evaluations = {
        "d-real-hit": outcome("d-real-hit", mfe=150.0, mae=-10.0),
        "d-real-miss": outcome("d-real-miss", mfe=10.0, mae=-250.0),
        "d-fake-hit": outcome("d-fake-hit", mfe=150.0, mae=-10.0),
    }
    scores = score_assessments(assessments, evaluations)

    assert scores.by_source.keys() == {"real", "synthetic"}
    assert scores.rate_for_source("real") == pytest.approx(0.5)
    assert scores.rate_for_source("synthetic") == pytest.approx(1.0)
    assert scores.hit_rate == pytest.approx(2 / 3), "the merged rate is still there"
    assert not scores.only_synthetic


def test_a_period_of_only_generated_readouts_says_so() -> None:
    """The state this repository is in. A review that printed a hit rate without saying so
    would read as a measurement of the market."""
    from datetime import datetime as dt

    from aureon.models.review import WeeklyReview

    review = WeeklyReview(
        iso_year=2026,
        iso_week=38,
        evaluation_rule_id="XAU_OUTCOME_V2",
        period_start=dt(2026, 9, 14, tzinfo=UTC),
        period_end=dt(2026, 9, 18, tzinfo=UTC),
        market_tz=MARKET_TZ,
        assessment_hit_by_source={"synthetic": "3/5"},
    )
    assert review.assessment_history_is_synthetic

    mixed = review.model_copy(
        update={"assessment_hit_by_source": {"synthetic": "3/5", "real": "1/2"}}
    )
    assert not mixed.assessment_history_is_synthetic

    unresolved = review.model_copy(
        update={"assessment_hit_by_source": {"synthetic": "0/0"}}
    )
    assert not unresolved.assessment_history_is_synthetic, (
        "nothing resolved is not the same as everything synthetic"
    )


def test_a_candle_is_dated_on_the_broker_clock_not_utc() -> None:
    """The dates a cohort is matched against are broker dates, because that is what an
    evidence file is named on: a candle at 22:00 UTC belongs to the next trading day in
    Athens, and matching it against the UTC date would call a real day synthetic."""
    late = Candle(
        symbol=SYMBOL,
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(datetime(2026, 9, 16, 22, 0, tzinfo=UTC), MARKET_TZ),
        open=2400.0,
        high=2400.5,
        low=2399.5,
        close=2400.2,
        tick_volume=10,
    )
    assert late.open_time.market_date == "2026-09-17"
