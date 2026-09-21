"""The tuning report measures what the agent measures, and applies nothing (11C, F-4).

Two properties carry this file, and they pull in opposite directions.

**It has to measure the same quantity the agent compares.** A report on "wick size" that
computed a slightly different ratio from ``WickAgent``'s would be advice about a different
number, and the advice would look identical -- so each measure is checked against a candle
whose value is known by construction, and one is checked against the agent itself.

**It must not be able to change anything.** A threshold change is an ``agent_version`` bump
(§12), which is a commit a human makes. A flag that edited ``OVERRIDES`` would make it
possible to retune a live system by running a report, and afterwards nothing would say it had
happened. So one test parses the script and asserts no such argument exists -- against the
AST rather than the text, because a grep for the string trips on the docstring explaining
why there isn't one, and that is how a guard comes to be deleted for being annoying.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.config.symbol_tuning import known_symbols, tuning_for
from aureon.config.tuning_report import (
    MEASURES,
    QUANTILES,
    admitted_at,
    candidates,
    describe,
    quantiles_of,
)
from aureon.models.base import MarketTime
from aureon.models.enums import Timeframe
from aureon.models.market import Candle

MARKET_TZ = "Europe/Athens"
START = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)


def candle(
    *, open_: float, high: float, low: float, close: float, index: int = 0
) -> Candle:
    return Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(START + timedelta(minutes=5 * index), MARKET_TZ),
        open=open_,
        high=high,
        low=low,
        close=close,
        tick_volume=100,
    )


def measure(field: str):
    return next(m for m in MEASURES if m.field == field)


def value_of(field: str, bar: Candle, point: float = 0.01) -> float | None:
    return measure(field).of(bar, point)


# ── Each measure is the quantity it claims to be ─────────────────────────────


def test_the_range_measure_is_the_range_in_points() -> None:
    bar = candle(open_=2400.0, high=2401.0, low=2399.5, close=2400.5)
    assert value_of("min_range_points", bar) == pytest.approx(150.0)


def test_the_wick_ratio_is_the_LARGER_wick_over_the_range() -> None:
    """Larger, because ``WickAgent`` tests the upper and lower wick separately and a bar
    qualifies on either. Reporting only one would understate what the threshold admits."""
    upper = candle(open_=2400.0, high=2410.0, low=2399.0, close=2400.5)
    assert value_of("min_wick_range_ratio", upper) == pytest.approx(9.5 / 11.0)
    lower = candle(open_=2400.0, high=2400.5, low=2390.0, close=2400.0)
    assert value_of("min_wick_range_ratio", lower) == pytest.approx(10.0 / 10.5)


def test_a_bar_with_no_body_has_no_wick_body_ratio() -> None:
    """Dividing by zero, not a value of zero. Excluded and counted, so a quantile is not
    quietly computed over a subset that pretends to be the whole."""
    doji = candle(open_=2400.0, high=2401.0, low=2399.0, close=2400.0)
    assert value_of("min_wick_body_ratio", doji) is None

    distribution = describe(measure("min_wick_body_ratio"), [doji], tuning_for("XAUUSD"))
    assert distribution.n == 0
    assert distribution.skipped == 1


def test_the_close_position_is_the_distance_from_the_NEARER_end() -> None:
    """``WickAgent`` measures from the rejected end, which is the top for an upper wick and
    the bottom for a lower one. The distance from whichever end is nearer is the quantity the
    threshold bounds either way."""
    near_the_low = candle(open_=2400.0, high=2410.0, low=2399.0, close=2400.1)
    assert value_of("max_close_position", near_the_low) == pytest.approx(1.1 / 11.0)
    near_the_high = candle(open_=2400.0, high=2410.0, low=2399.0, close=2409.9)
    assert value_of("max_close_position", near_the_high) == pytest.approx(0.1 / 11.0)


def test_a_zero_range_bar_is_skipped_by_every_ratio() -> None:
    flat = candle(open_=2400.0, high=2400.0, low=2400.0, close=2400.0)
    for field in ("min_range_points", "min_wick_range_ratio", "max_close_position"):
        assert value_of(field, flat) is None, field


def test_the_wick_measure_agrees_with_the_agent_on_a_bar_it_admits() -> None:
    """The check that matters most: the report and the agent, on the same bar.

    Driven through ``AnalysisEngine``, which is how the observer reaches the agent, so the
    bar's context is built the same way rather than by a hand-assembled stand-in. If the two
    ever disagree the report is advice about a number nothing compares.
    """
    from aureon.agents.wick_agent import WickAgent
    from aureon.engine.analysis_engine import AnalysisEngine

    bar = candle(open_=2409.0, high=2410.0, low=2390.0, close=2409.5)
    tuning = tuning_for("XAUUSD")
    assert value_of("min_range_points", bar) >= tuning.min_range_points
    assert value_of("min_wick_range_ratio", bar) >= tuning.min_wick_range_ratio
    assert value_of("max_close_position", bar) <= tuning.max_close_position
    assert value_of("min_wick_body_ratio", bar) >= tuning.min_wick_body_ratio

    engine = AnalysisEngine(
        [
            WickAgent(
                point=tuning.point,
                min_wick_range_ratio=tuning.min_wick_range_ratio,
                min_wick_body_ratio=tuning.min_wick_body_ratio,
                max_close_position=tuning.max_close_position,
                min_range_points=tuning.min_range_points,
            )
        ],
        account_scope="primary",
        market_tz=MARKET_TZ,
    )
    assert engine.on_closed_candle(bar), (
        "the report says this bar clears every wick threshold, so the agent must emit on it"
    )


def test_a_bar_the_report_excludes_is_one_the_agent_ignores() -> None:
    """The other direction, which is the one a vacuous test would miss: a bar below
    ``min_range_points`` has to produce nothing, or the measure is describing a filter that
    is not there."""
    from aureon.agents.wick_agent import WickAgent
    from aureon.engine.analysis_engine import AnalysisEngine

    tuning = tuning_for("XAUUSD")
    tiny = candle(open_=2400.0, high=2400.05, low=2400.0, close=2400.04)
    assert value_of("min_range_points", tiny) < tuning.min_range_points

    engine = AnalysisEngine(
        [WickAgent(point=tuning.point, min_range_points=tuning.min_range_points)],
        account_scope="primary",
        market_tz=MARKET_TZ,
    )
    assert engine.on_closed_candle(tiny) == []


# ── The distribution, and what a threshold admits ────────────────────────────


def sample() -> list[Candle]:
    """Ten bars whose ranges are 100, 200, … 1000 points, so every quantile is known."""
    return [
        candle(open_=2400.0, high=2400.0 + i, low=2399.0, close=2400.0 + i / 2, index=i)
        for i in range(1, 11)
    ]


def test_a_threshold_above_every_bar_admits_nothing_and_says_so() -> None:
    """The finding this whole tool exists for: a threshold outside the data produces no
    detections and looks exactly like a quiet market."""
    distribution = describe(measure("min_range_points"), sample(), tuning_for("XAUUSD"))
    assert admitted_at(distribution, 1e9) == 0
    huge = describe(
        measure("min_range_points"),
        sample(),
        tuning_for("XAUUSD").__class__(min_range_points=1e9),
    )
    assert huge.admitted == 0
    assert "ADMITS NOTHING" in huge.verdict


def test_a_threshold_below_every_bar_admits_everything_and_says_so() -> None:
    tiny = describe(
        measure("min_range_points"),
        sample(),
        tuning_for("XAUUSD").__class__(min_range_points=0.0),
    )
    assert tiny.admitted == tiny.n == 10
    assert "almost anything qualifies" in tiny.verdict


def test_the_shipped_gold_range_threshold_is_inert_on_gold() -> None:
    """A real finding, pinned so it cannot quietly change.

    ``min_range_points = 20`` is $0.20 of gold, and on the fixture week it admits about 99%
    of five-minute bars -- so as a filter it does almost nothing. That is exactly what the
    module docstring in ``symbol_tuning`` warns these placeholders might be, and the number
    belongs in a test rather than in a paragraph.
    """
    from aureon.data.historical_provider import HistoricalDataProvider
    from tests.conftest import FIXTURE_CSV

    candles = HistoricalDataProvider(FIXTURE_CSV, market_tz=MARKET_TZ).candles
    distribution = describe(measure("min_range_points"), candles, tuning_for("XAUUSD"))
    assert distribution.share_admitted > 0.95, distribution.verdict


def test_a_max_threshold_counts_bars_at_or_BELOW_it() -> None:
    """``max_close_position`` is the one threshold a bar has to stay under, and a report
    that counted it the other way would recommend its own inverse."""
    distribution = describe(
        measure("max_close_position"), sample(), tuning_for("XAUUSD")
    )
    assert not distribution.measure.admits_above
    loose = admitted_at(distribution, 0.5)
    tight = admitted_at(distribution, 0.0)
    assert loose >= tight, "a LARGER maximum admits more, not fewer"


def test_a_candidate_share_is_counted_not_interpolated() -> None:
    """The first version derived each candidate's share from the quantile it had derived
    from the share, which agreed with itself and with nothing else. A tied distribution --
    many bars at one value, which is what a tick boundary produces -- is where the two part
    company."""
    tied = [candle(open_=2400.0, high=2401.0, low=2400.0, close=2400.5, index=i) for i in range(20)]
    distribution = describe(measure("min_range_points"), tied, tuning_for("XAUUSD"))
    for value, admitted, share in candidates(distribution):
        assert admitted == admitted_at(distribution, value)
        assert share == pytest.approx(admitted / distribution.n)


def test_the_quantiles_are_the_ones_pandas_would_give() -> None:
    """Type 7, the numpy default, for the same reason 9D chose it: it is the one a reader
    who checks their own numbers will get."""
    import statistics

    values = [float(i) for i in range(1, 101)]
    got = quantiles_of(values)
    cuts = statistics.quantiles(values, n=100, method="inclusive")
    for q in QUANTILES:
        assert got[q] == pytest.approx(cuts[round(q * 100) - 1])


def test_one_value_reports_itself_at_every_quantile() -> None:
    assert set(quantiles_of([4.0]).values()) == {4.0}
    assert quantiles_of([]) == {}


# ── It cannot apply anything ─────────────────────────────────────────────────


def test_the_tool_has_no_apply_option_and_assigns_into_no_table() -> None:
    """Pinned against the AST, not against the prose.

    The danger is a helpful future edit rather than a bug: a ``--apply`` would make it
    possible to retune a live system by running a report, and afterwards nothing would say
    it had happened -- the detections before and after would sit at ids produced by the same
    ``agent_version``, indistinguishable and incomparable. Checked as a parser ARGUMENT
    because a grep for the string trips on the docstring that explains why there isn't one,
    which is how a guard comes to be deleted for being annoying.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "tune_report.py"
    tree = ast.parse(script.read_text(encoding="utf-8"))

    flags = {
        arg.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        for arg in node.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    }
    assert "--apply" not in flags, flags
    assert "--write-tuning" not in flags, flags

    for path in (script, root / "aureon" / "config" / "tuning_report.py"):
        text = path.read_text(encoding="utf-8")
        assert "OVERRIDES[" not in text, f"{path} assigns into the tuning table"
        assert "OVERRIDES.update" not in text, f"{path} mutates the tuning table"


def test_an_unreviewed_symbol_is_refused() -> None:
    from scripts.tune_report import main

    with pytest.raises(SystemExit):
        main(["--symbol", "EURUSD"])
    assert "EURUSD" not in known_symbols()


def test_a_report_with_no_archive_refuses_rather_than_writing_an_empty_page(
    tmp_path, capsys
) -> None:
    """An empty report under a real title would be filed as "we looked and found nothing",
    which is a different claim from "nothing has been recorded yet"."""
    from scripts.tune_report import main

    code = main(
        [
            "--symbol",
            "XAUUSD",
            "--days",
            "3",
            "--archive-dir",
            str(tmp_path),
            "--out",
            str(tmp_path / "TUNING.md"),
        ]
    )
    assert code == 1
    assert not (tmp_path / "TUNING.md").exists()
    assert "no archived bars" in capsys.readouterr().err


def test_a_fixture_report_is_stamped_synthetic(tmp_path) -> None:
    """A generated random walk has the distribution its generator was given, so a threshold
    chosen from one is a threshold chosen from ``gen_fixtures.py``."""
    from scripts.tune_report import main

    out = tmp_path / "TUNING_XAUUSD.md"
    assert main(["--symbol", "XAUUSD", "--fixture", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "SYNTHETIC" in text
    assert "not a tuning decision" in text
    assert "agent_version" in text, "and it says what applying one would require"


def test_a_real_report_names_its_days_and_flags_immaturity(tmp_path) -> None:
    """Under ten verified days the page is a description of the sample, and says so."""
    from aureon.data.historical_provider import HistoricalDataProvider
    from aureon.data.live_candle_archive import LiveCandleArchive
    from scripts.tune_report import main
    from tests.conftest import FIXTURE_CSV

    archive = LiveCandleArchive(tmp_path)
    candles = HistoricalDataProvider(FIXTURE_CSV, market_tz=MARKET_TZ).candles
    days = sorted({c.open_time.market_date for c in candles})[:2]
    for one in candles:
        if one.open_time.market_date in days:
            archive.add(one)
    archive.flush_all()

    out = tmp_path / "TUNING_XAUUSD.md"
    # --days spans the fixture's dates, which are in the past relative to nothing: the
    # collector walks back from today, so the test reads the archive directly instead.
    from aureon.config import AureonConfig
    from aureon.config.tuning_report import describe as describe_one
    from scripts.tune_report import collect, render

    collected, found = collect(
        "XAUUSD",
        days=400,
        timeframe=Timeframe.M5,
        market_tz=MARKET_TZ,
        archive_dir=tmp_path,
        today=datetime.fromisoformat(days[-1]).date(),
    )
    assert found == days, found
    text = render(
        "XAUUSD",
        [describe_one(m, collected, tuning_for("XAUUSD")) for m in MEASURES],
        days=found,
        synthetic=False,
        timeframe=Timeframe.M5,
        generated_at=START,
    )
    out.write_text(text, encoding="utf-8")
    assert "Immature history: 2 real day(s)" in text
    assert "SYNTHETIC" not in text
    assert AureonConfig is not None  # imported for the CLI path above
    assert main is not None
