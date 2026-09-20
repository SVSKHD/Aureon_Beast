"""The per-agent, per-session outcome report, on data whose answers are arithmetic (P-1).

Two layers, deliberately:

* ``aggregate`` against **hand-built** evaluations, where every expected count, median
  and fraction is written out in the test rather than copied from a run. This is the
  layer that can prove the things that matter -- that a PENDING horizon never enters a
  denominator, that a median of nothing is ``None`` and not zero, that MFE/MAE are
  reported in the rule's own unit.
* the whole path -- candles, agents, tracker, aggregate -- over synthetic candles whose
  excursion is $7 by construction, so a wiring mistake between the tracker and the
  report cannot hide behind a plausible-looking number.

The CLI is then driven end to end over a trimmed slice of the committed fixture, because
``--from``/``--to`` silently doing nothing is exactly the failure a report about "last
week" would not reveal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureon.evaluation.outcome_report import (
    ALL_SESSIONS,
    aggregate,
    render_markdown,
    render_text,
)
from aureon.evaluation.rules import (
    EMA_OUTCOME_V1,
    HORIZON_5_CANDLES,
    HORIZON_20_CANDLES,
    XAU_OUTCOME_V2,
)
from aureon.models.base import MarketTime
from aureon.models.detection import Detection, SessionContext
from aureon.models.enums import (
    Direction,
    HorizonStatus,
    PathClassification,
    SessionName,
    Timeframe,
)
from aureon.models.evaluation import DetectionEvaluation, HorizonResult
from aureon.models.identity import detection_id
from aureon.models.market import Candle

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "aureon" / "data" / "fixtures" / "XAUUSD_M5.csv"
TZ = "Europe/Athens"
START = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)  # a Wednesday, London session
#: XAUUSD's tick. The V2 thresholds are in PRICE, so $5 is 500 of these.
POINT = 0.01


# ── Builders ─────────────────────────────────────────────────────────────────


def detection(
    name: str,
    *,
    agent: str = "liquidity",
    session: SessionName = SessionName.LONDON,
    index: int = 0,
    direction: Direction | None = Direction.BUY,
) -> Detection:
    """A detection with a distinct id per ``name``, so groups stay separable."""
    close = START + timedelta(minutes=5 * (index + 1))
    return Detection(
        detection_id=detection_id(
            account_scope="primary",
            symbol="XAUUSD",
            timeframe="M5",
            candle_close=close,
            agent_name=agent,
            agent_version="1.0.0",
            event_key=name,
        ),
        account_scope="primary",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        agent_name=agent,
        agent_version="1.0.0",
        event_key=name,
        direction=direction,
        detected_at=MarketTime.from_utc(close, TZ),
        candle_open_time=MarketTime.from_utc(close - timedelta(minutes=5), TZ),
        price=2400.0,
        session=SessionContext(session=session, session_config_version=1),
        sequence_today=index + 1,
        sequence_session=index + 1,
    )


def complete(
    *,
    mfe: float,
    mae: float,
    reached: dict[str, bool],
    time_to: dict[str, float | None],
    path: PathClassification,
    horizon_id: str = HORIZON_5_CANDLES,
) -> HorizonResult:
    return HorizonResult(
        horizon_id=horizon_id,
        status=HorizonStatus.COMPLETE,
        future_high=2400.0 + mfe * POINT,
        future_low=2400.0 + mae * POINT,
        mfe=mfe,
        mae=mae,
        reached=reached,
        time_to=time_to,
        path=path,
        completed_at=START + timedelta(hours=1),
    )


def pending(horizon_id: str = HORIZON_5_CANDLES) -> HorizonResult:
    return HorizonResult(horizon_id=horizon_id, status=HorizonStatus.PENDING)


def evaluation(
    subject: Detection, *horizons: HorizonResult, rule_id: str = "XAU_OUTCOME_V2"
) -> DetectionEvaluation:
    return DetectionEvaluation(
        detection_id=subject.detection_id,
        rule_id=rule_id,
        reference_price=XAU_OUTCOME_V2.reference_price,
        reference_value=2400.0,
        horizons=horizons,
    )


ALL_MISSED = {"3": False, "5": False, "10": False, "15": False, "20": False}
REACHED_5 = {"3": True, "5": True, "10": False, "15": False, "20": False}
NO_TIMES: dict[str, float | None] = dict.fromkeys(ALL_MISSED, None)


@pytest.fixture
def known():
    """Five detections whose every aggregate figure is written out below.

    London, agent ``liquidity``, horizon ``c5``:

    | detection | status   | MFE   | MAE  | reached      | t→$5  | path      |
    |-----------|----------|-------|------|--------------|-------|-----------|
    | a         | COMPLETE |  +700 | -100 | $3, $5       |  600s | MFE_FIRST |
    | b         | COMPLETE |  +900 | -300 | $3, $5       | 1200s | MAE_FIRST |
    | c         | COMPLETE |  +200 |  -50 | none         |     — | NONE      |
    | d         | PENDING  |     — |    — | —            |     — | —         |

    Asia, same agent: one PENDING. Plus a ``wick`` detection with no direction and no
    evaluation at all.
    """
    a = detection("a", index=0)
    b = detection("b", index=1)
    c = detection("c", index=2)
    d = detection("d", index=3)
    asia = detection("e", index=4, session=SessionName.ASIA)
    context = detection("f", agent="wick", index=5, direction=None)

    evaluations = [
        evaluation(
            a,
            complete(
                mfe=700,
                mae=-100,
                reached=REACHED_5,
                time_to={**NO_TIMES, "3": 300.0, "5": 600.0},
                path=PathClassification.MFE_FIRST,
            ),
        ),
        evaluation(
            b,
            complete(
                mfe=900,
                mae=-300,
                reached=REACHED_5,
                time_to={**NO_TIMES, "3": 600.0, "5": 1200.0},
                path=PathClassification.MAE_FIRST,
            ),
        ),
        evaluation(
            c,
            complete(
                mfe=200,
                mae=-50,
                reached=dict(ALL_MISSED),
                time_to=dict(NO_TIMES),
                path=PathClassification.NONE,
            ),
        ),
        evaluation(d, pending()),
        evaluation(asia, pending()),
    ]
    return [a, b, c, d, asia, context], evaluations


# ── aggregate ────────────────────────────────────────────────────────────────


def test_every_figure_for_a_group_is_the_arithmetic_one(known) -> None:
    detections, evaluations = known
    report = aggregate(detections, evaluations, XAU_OUTCOME_V2, point=POINT)

    london = report.group("liquidity", "london")
    assert london.detections == 4
    assert london.evaluated == 4
    assert london.with_complete == 3, "d is PENDING and has no COMPLETE horizon"

    c5 = london.horizons[HORIZON_5_CANDLES]
    assert (c5.complete, c5.pending, c5.invalid) == (3, 1, 0)

    # Denominators are COMPLETE only: 3, never the 4 detections.
    assert c5.reached == {"3": 2, "5": 2, "10": 0, "15": 0, "20": 0}
    assert c5.fraction_reached("3") == pytest.approx(2 / 3)
    assert c5.fraction_reached("20") == 0.0, "reached by none of three IS a measurement"

    # Medians in points, then in the rule's unit -- $7.00 and -$1.00.
    assert c5.median_mfe_points == 700
    assert c5.median_mae_points == -100
    assert report.in_rule_unit(c5.median_mfe_points) == pytest.approx(7.0)
    assert report.in_rule_unit(c5.median_mae_points) == pytest.approx(-1.0)

    # Time-to-$5 is over the two that reached it, never over all three.
    assert c5.median_time_to_seconds("5") == 900.0
    assert c5.reachers("5") == 2
    assert c5.median_time_to_seconds("20") is None

    assert (c5.mfe_first, c5.mae_first, c5.path_none) == (1, 1, 1)
    assert c5.path_ambiguous == 0


def test_a_pending_only_group_reports_unknown_not_zero(known) -> None:
    """Asia has one PENDING horizon and nothing else. Every figure must be absent."""
    detections, evaluations = known
    report = aggregate(detections, evaluations, XAU_OUTCOME_V2, point=POINT)

    asia = report.group("liquidity", "asia").horizons[HORIZON_5_CANDLES]
    assert (asia.complete, asia.pending) == (0, 1)
    assert asia.fraction_reached("3") is None, "0/0 is unknown, not 0%"
    assert asia.median_mfe_points is None
    assert asia.median_time_to_seconds("5") is None


def test_an_agent_with_no_direction_is_counted_and_not_evaluated(known) -> None:
    detections, evaluations = known
    report = aggregate(detections, evaluations, XAU_OUTCOME_V2, point=POINT)

    wick = report.group("wick", ALL_SESSIONS)
    assert (wick.detections, wick.evaluated, wick.context_only) == (1, 0, 1)
    assert wick.horizons == {}
    # And it is named in the markdown rather than silently dropped.
    text = "\n".join(render_markdown(report))
    assert "### `wick` — no outcomes" in text


def test_the_all_sessions_row_is_the_sum_of_its_sessions(known) -> None:
    detections, evaluations = known
    report = aggregate(detections, evaluations, XAU_OUTCOME_V2, point=POINT)

    merged = report.group("liquidity", ALL_SESSIONS).horizons[HORIZON_5_CANDLES]
    london = report.group("liquidity", "london").horizons[HORIZON_5_CANDLES]
    asia = report.group("liquidity", "asia").horizons[HORIZON_5_CANDLES]

    assert merged.complete == london.complete + asia.complete
    assert merged.pending == london.pending + asia.pending
    assert merged.reached["5"] == london.reached["5"] + asia.reached.get("5", 0)
    # A merged median comes from the merged SAMPLES, not from averaging two medians.
    assert merged.median_mfe_points == london.median_mfe_points
    assert report.group("liquidity", ALL_SESSIONS).detections == 5


def test_an_evaluation_under_another_rule_is_ignored(known) -> None:
    """A repository read returns every rule's results for a detection (§21).

    Counting a V1 result in a V2 report would mix two threshold scales into one
    column, and nothing in the output would say so.
    """
    detections, evaluations = known
    foreign = evaluation(
        detections[0],
        complete(
            mfe=5000,
            mae=-1,
            reached=dict.fromkeys(ALL_MISSED, True),
            time_to=dict(NO_TIMES),
            path=PathClassification.MFE_FIRST,
        ),
        rule_id=EMA_OUTCOME_V1.rule_id,
    )
    report = aggregate(
        detections, [*evaluations, foreign], XAU_OUTCOME_V2, point=POINT
    )
    c5 = report.group("liquidity", "london").horizons[HORIZON_5_CANDLES]
    assert c5.complete == 3, "the V1 result must not become a fourth COMPLETE horizon"
    assert c5.median_mfe_points == 700


def test_a_points_rule_reports_points_and_a_price_rule_reports_price(known) -> None:
    detections, evaluations = known
    price = aggregate(detections, evaluations, XAU_OUTCOME_V2, point=POINT)
    assert price.unit_scale == POINT
    assert price.unit_label == "price"
    assert "reach $5" in "\n".join(render_markdown(price))

    points = aggregate(
        detections,
        [e.model_copy(update={"rule_id": EMA_OUTCOME_V1.rule_id}) for e in evaluations],
        EMA_OUTCOME_V1,
        point=POINT,
    )
    assert points.unit_scale == 1.0
    assert points.in_rule_unit(700) == 700
    assert "reach 5pt" in "\n".join(render_markdown(points))


def test_agents_can_be_restricted(known) -> None:
    detections, evaluations = known
    report = aggregate(
        detections, evaluations, XAU_OUTCOME_V2, point=POINT, agents=["liquidity"]
    )
    assert report.agents == ("liquidity",)


def test_a_non_positive_point_is_refused(known) -> None:
    detections, evaluations = known
    with pytest.raises(ValueError, match="point must be positive"):
        aggregate(detections, evaluations, XAU_OUTCOME_V2, point=0.0)


def test_both_renderers_emit_every_session_and_horizon(known) -> None:
    detections, evaluations = known
    report = aggregate(detections, evaluations, XAU_OUTCOME_V2, point=POINT)
    for text in ("\n".join(render_markdown(report)), render_text(report)):
        assert "liquidity" in text
        assert "london" in text and "asia" in text
        assert HORIZON_5_CANDLES in text
        assert "2/3" in text, "the reached count and its denominator"
    assert "+7.00" in "\n".join(render_markdown(report))
    assert "-1.00" in "\n".join(render_markdown(report))
    assert "15m (n=2)" in "\n".join(render_markdown(report))


def test_an_empty_period_says_so_rather_than_rendering_zeros() -> None:
    report = aggregate([], [], XAU_OUTCOME_V2, point=POINT)
    assert report.agents == ()
    assert "no detections in this period" in render_text(report)
    assert render_markdown(report) == []


# ── The whole path, over candles ─────────────────────────────────────────────


def _candle(index: int, *, high: float, low: float) -> Candle:
    opened = START + timedelta(minutes=5 * index)
    return Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(opened, TZ),
        open=2400.0,
        high=high,
        low=low,
        close=2400.0,
    )


def test_a_seven_dollar_run_reports_seven_dollars() -> None:
    """Tracker → aggregate, on a path built to reach exactly $7 favourably.

    Reference is the NEXT open (2400.0). The highs give $1, $3.50, $5.50, $7.00, $6.00
    over five candles, so within ``c5``: $3 and $5 reached, $10/$15/$20 not, MFE $7.00,
    MAE -$0.50, and $5 first reached at the close of the third candle after the
    detection -- 15 minutes after the detection's own close.
    """
    from aureon.evaluation.outcome_tracker import OutcomeTracker

    subject = detection("bullish", index=0)
    highs = [2401.0, 2403.5, 2405.5, 2407.0, 2406.0]
    tracker = OutcomeTracker(XAU_OUTCOME_V2, market_tz=TZ, point=POINT)
    tracker.track(subject)
    for offset, high in enumerate(highs, start=1):
        tracker.on_closed_candle(_candle(offset, high=high, low=2399.5))

    report = aggregate(
        [subject], tracker.all_evaluations(), XAU_OUTCOME_V2, point=POINT
    )
    c5 = report.group("liquidity", "london").horizons[HORIZON_5_CANDLES]

    assert c5.complete == 1
    assert c5.reached == {"3": 1, "5": 1, "10": 0, "15": 0, "20": 0}
    assert report.in_rule_unit(c5.median_mfe_points) == pytest.approx(7.0)
    assert report.in_rule_unit(c5.median_mae_points) == pytest.approx(-0.5)
    assert c5.median_time_to_seconds("5") == 15 * 60
    assert (c5.mfe_first, c5.mae_first) == (1, 0)

    # c20 never terminates on five candles, so it stays PENDING and contributes to no
    # reached count -- the distinction the whole report rests on.
    c20 = c5 = report.group("liquidity", "london").horizons[HORIZON_20_CANDLES]
    assert (c20.complete, c20.pending) == (0, 1)
    assert c20.fraction_reached("5") is None


def test_an_invalidated_horizon_is_excluded_from_every_figure() -> None:
    """A gap makes a horizon INVALID; it must not become a miss (§22).

    Built as a gap rather than by constructing an INVALID result, so the exclusion is
    proven through the tracker that produces them.
    """
    from aureon.evaluation.outcome_tracker import OutcomeTracker

    subject = detection("bullish", index=0)
    tracker = OutcomeTracker(XAU_OUTCOME_V2, market_tz=TZ, point=POINT)
    tracker.track(subject)
    tracker.on_closed_candle(_candle(1, high=2401.0, low=2399.5))
    # Two candles' worth of silence, then a bar: past the gap tolerance.
    tracker.on_closed_candle(_candle(6, high=2450.0, low=2399.0))

    report = aggregate(
        [subject], tracker.all_evaluations(), XAU_OUTCOME_V2, point=POINT
    )
    c5 = report.group("liquidity", "london").horizons[HORIZON_5_CANDLES]
    assert (c5.complete, c5.invalid) == (0, 1)
    assert c5.fraction_reached("20") is None, "a $50 move inside a gap is not evidence"
    assert c5.median_mfe_points is None


# ── The CLI ──────────────────────────────────────────────────────────────────

#: Data rows of the committed fixture the CLI tests replay: three broker days
#: (2026-09-14 … 2026-09-16), the last one partial. Trimmed rather than whole because a
#: full-week replay of seven horizons costs half a minute, and what these tests prove --
#: that the range filters, that the exits are right -- does not need the other four days.
#: It is the committed fixture's own bytes, so it is as deterministic as the fixture.
TRIM_ROWS = 600
TRIM_FIRST_DATE = "2026-09-14"
TRIM_LAST_DATE = "2026-09-16"
TRIM_MIDDLE_DATE = "2026-09-15"


@pytest.fixture(scope="module")
def trimmed(tmp_path_factory) -> Path:
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    path = tmp_path_factory.mktemp("replay") / "XAUUSD_M5_trimmed.csv"
    path.write_text("\n".join(lines[: TRIM_ROWS + 1]) + "\n", encoding="utf-8")
    return path


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Drive ``main`` and capture both streams.

    Not ``capsys``: the replaying runs are shared by several tests through a
    module-scoped fixture, and a function-scoped capture cannot outlive one test.
    """
    import contextlib
    import io

    from scripts.report_outcomes import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(scope="module")
def runs(trimmed) -> dict[str, tuple[int, str, str]]:
    """Exactly two replaying invocations, shared by every test that needs one."""
    return {
        "span": _run(
            [
                "--rule",
                "XAU_OUTCOME_V2",
                "--replay",
                str(trimmed),
                "--from",
                TRIM_FIRST_DATE,
                "--to",
                TRIM_LAST_DATE,
            ]
        ),
        # No --rule and no --to: the configured rule, and one day.
        "one_day": _run(["--replay", str(trimmed), "--from", TRIM_MIDDLE_DATE]),
    }


def _detections(output: str) -> int:
    line = next(ln for ln in output.splitlines() if ln.endswith("evaluations"))
    return int(line.split()[0])


def test_the_cli_reports_every_directional_agent(runs) -> None:
    code, out, _err = runs["span"]
    assert code == 0
    assert "XAU_OUTCOME_V2" in out
    assert f"broker dates {TRIM_FIRST_DATE} … {TRIM_LAST_DATE}" in out
    for agent in ("ema_cross", "liquidity", "breakout"):
        assert agent in out, f"{agent} produced no rows"
    assert "COMPLETE horizons ONLY" in out
    assert _detections(out) > 0


def test_the_date_range_actually_narrows_the_report(runs) -> None:
    """A range that does nothing is the failure a weekly report would never reveal."""
    span, one_day = runs["span"][1], runs["one_day"][1]
    assert _detections(one_day) > 0
    assert _detections(one_day) < _detections(span)


def test_a_single_date_means_that_day_only(runs) -> None:
    code, out, _err = runs["one_day"]
    assert code == 0
    assert f"{TRIM_MIDDLE_DATE} … {TRIM_MIDDLE_DATE}" in out


def test_a_backwards_range_is_refused() -> None:
    code, _out, err = _run(["--from", "2026-09-18", "--to", "2026-09-14"])
    assert code == 2
    assert "is before" in err


def test_a_missing_replay_file_is_refused() -> None:
    code, _out, err = _run(["--replay", "does/not/exist.csv", "--from", "2026-09-16"])
    assert code == 2
    assert "does not exist" in err


def test_markdown_and_text_come_from_the_same_run(runs, trimmed) -> None:
    """``--markdown`` changes the rendering, never the numbers."""
    code, out, _err = _run(
        ["--replay", str(trimmed), "--from", TRIM_MIDDLE_DATE, "--markdown"]
    )
    assert code == 0
    assert out.count("| session | horizon |") >= 1
    assert _detections(out) == _detections(runs["one_day"][1])


def test_the_default_range_is_the_week_before_now() -> None:
    from scripts.report_outcomes import DEFAULT_DAYS, parse_range

    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    first, last = parse_range(None, None, market_tz=TZ, now=now)
    assert last.isoformat() == "2026-09-20", "yesterday on the broker clock"
    assert (last - first).days == DEFAULT_DAYS - 1
