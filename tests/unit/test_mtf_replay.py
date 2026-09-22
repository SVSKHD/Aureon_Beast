"""Verifying the higher-timeframe context against a replay (12, T-12).

The default live-vs-replay comparison excludes the ``mtf`` block, for a good reason: one archived
day cannot reproduce a read built from a rolling multi-day buffer. ``--mtf`` closes that gap by
reading several days and diffing each detection's context **as of its own candle**.

What is being verified, said precisely, because it is narrower than "the MTF context is correct":
that the live observer's buffer held the bars this archive holds. If the aggregation itself were
wrong, both sides would be wrong identically and this would report IDENTICAL. That is a division
of labour rather than a hole — ``test_mtf.py`` checks the aggregation against buckets known by
construction, and this checks its input.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import compare_live_vs_replay as tool  # noqa: E402

from aureon.engine import mtf  # noqa: E402
from aureon.models.base import MarketTime  # noqa: E402
from aureon.models.detection import Detection, IndicatorSnapshot, SessionContext  # noqa: E402
from aureon.models.enums import (  # noqa: E402
    Direction,
    SessionName,
    Timeframe,
    TrendBias,
)
from aureon.models.market import Candle  # noqa: E402
from aureon.models.mtf import MtfContext, TimeframeRead  # noqa: E402

TZ = "Europe/Athens"
START = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
SYMBOL = "XAUUSD"
FAST, SLOW = 20, 50


def candles(count: int, *, start: datetime = START) -> list[Candle]:
    """A deterministic M5 walk long enough to seed an H4 EMA.

    Long on purpose: fifty H4 bars is fifty times forty-eight M5 bars, so a fixture that only
    covered a day would make every H4 read 'unreadable' and the tests would pass while checking
    nothing about H4 at all.
    """
    out = []
    for index in range(count):
        at = start + timedelta(minutes=5 * index)
        base = 2400.0 + index * 0.1
        out.append(
            Candle(
                symbol=SYMBOL,
                timeframe=Timeframe.M5,
                open_time=MarketTime.from_utc(at, TZ),
                open=round(base, 5),
                high=round(base + 0.4, 5),
                low=round(base - 0.4, 5),
                close=round(base + 0.2, 5),
                tick_volume=100 + index % 9,
                real_volume=0,
            )
        )
    return out


def context_at(bars: list[Candle]) -> MtfContext:
    """The context the observer would have stored at the last of those bars."""
    frames = mtf.frames_from(
        bars, fast=FAST, slow=SLOW, timeframes=tool.MTF_CHECKED, market_tz=TZ
    )
    return MtfContext(
        reads=tuple(
            TimeframeRead(
                timeframe=frame.timeframe,
                at=frame.at,
                ema_fast=frame.ema_fast,
                ema_slow=frame.ema_slow,
                close=frame.close,
                bias=frame.bias,
            )
            for frame in frames
        ),
        alignment=mtf.alignment(frames, Direction.BUY),
        ema_fast_period=FAST,
        ema_slow_period=SLOW,
    )


def detection(bars: list[Candle], *, ident: str = "det-1", context=None) -> Detection:
    last = bars[-1]
    return Detection(
        detection_id=ident,
        account_scope="primary",
        symbol=SYMBOL,
        timeframe=Timeframe.M5,
        agent_name="ema_cross",
        agent_version="2.2.0",
        event_key="bullish",
        direction=Direction.BUY,
        detected_at=MarketTime.from_utc(last.close_time, TZ),
        candle_open_time=last.open_time,
        price=last.close,
        indicators=IndicatorSnapshot(ema={"fast": 2401.0, "slow": 2395.0}),
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
        mtf=context_at(bars) if context is None else context,
    )


def run(detections, bars, *, market_date: str = "2026-09-14"):
    return tool.compare_mtf(
        detections,
        bars,
        market_date=market_date,
        symbol=SYMBOL,
        days_replayed=1,
        fast=FAST,
        slow=SLOW,
        market_tz=TZ,
    )


# ── it agrees with itself ─────────────────────────────────────────────────────


def test_a_context_recomputed_from_the_same_bars_agrees() -> None:
    bars = candles(3000)
    result = run([detection(bars)], bars)
    assert result.identical, result.render()
    assert result.compared >= len(tool.MTF_CHECKED)
    assert tool.MTF_VERIFIED_MARKER in result.render()


def test_every_checked_timeframe_is_actually_compared() -> None:
    """Otherwise 'identical' could mean 'we looked at M15 and gave up'."""
    bars = candles(3000)
    result = run([detection(bars)], bars)
    for timeframe in tool.MTF_CHECKED:
        assert result.agreed[timeframe.value] == 1, timeframe


def test_each_detection_is_checked_as_of_its_own_candle() -> None:
    """An H1 read at 09:00 and one at 15:00 are different bars. Comparing every detection
    against a single end-of-day read would report the morning as broken."""
    bars = candles(3000)
    early = detection(bars[:1500], ident="early")
    late = detection(bars, ident="late")
    result = run([early, late], bars)
    assert result.identical, result.render()
    assert result.agreed[Timeframe.H1.value] == 2


# ── it notices a difference ───────────────────────────────────────────────────


def test_a_changed_bias_is_a_disagreement() -> None:
    bars = candles(3000)
    stored = context_at(bars)
    first = stored.reads[0]
    flipped = stored.model_copy(
        update={
            "reads": (
                first.model_copy(update={"bias": TrendBias.BEARISH}),
                *stored.reads[1:],
            )
        }
    )
    result = run([detection(bars, context=flipped)], bars)
    assert not result.identical
    assert any("bias" in what for _, _, what in result.disagreed)


def test_a_read_from_a_different_bar_reports_the_BAR_not_the_emas() -> None:
    """The open time first, because it is the unambiguous field: two reads from different bars
    differ in every number as a consequence, and leading with the EMAs buries the cause."""
    bars = candles(3000)
    stored = context_at(bars)
    first = stored.reads[0]
    shifted = stored.model_copy(
        update={
            "reads": (
                first.model_copy(update={"at": first.at - timedelta(hours=1)}),
                *stored.reads[1:],
            )
        }
    )
    result = run([detection(bars, context=shifted)], bars)
    assert not result.identical
    _, _, what = result.disagreed[0]
    assert "bar open" in what
    assert "ema" not in what


def test_a_moved_ema_is_a_disagreement_and_a_float_wobble_is_not() -> None:
    """The two values travel different routes -- one through a Firestore JSON round trip -- so a
    bit-level comparison would fail on arithmetic that is in fact identical."""
    bars = candles(3000)
    stored = context_at(bars)
    first = stored.reads[0]

    wobbled = stored.model_copy(
        update={
            "reads": (
                first.model_copy(
                    update={"ema_fast": first.ema_fast + tool.EMA_TOLERANCE / 10}
                ),
                *stored.reads[1:],
            )
        }
    )
    assert run([detection(bars, context=wobbled)], bars).identical

    moved = stored.model_copy(
        update={
            "reads": (
                first.model_copy(update={"ema_fast": first.ema_fast + 0.5}),
                *stored.reads[1:],
            )
        }
    )
    result = run([detection(bars, context=moved)], bars)
    assert not result.identical
    assert any("ema_fast" in what for _, _, what in result.disagreed)


def test_a_detection_whose_candle_is_not_in_the_archive_is_a_disagreement() -> None:
    """Exactly the thing §82 exists to surface. Swallowing it here would hide it."""
    bars = candles(3000)
    orphan = detection(bars).model_copy(
        update={
            "candle_open_time": MarketTime.from_utc(
                START + timedelta(days=400), TZ
            )
        }
    )
    result = run([orphan], bars)
    assert not result.identical
    assert "not in the archive" in result.disagreed[0][2]


# ── the honest absences ───────────────────────────────────────────────────────


def test_a_detection_without_a_context_is_counted_not_compared() -> None:
    """A detection from before 11D has no context to disagree about, and calling that a
    mismatch would report every pre-11D session as broken."""
    bars = candles(3000)
    bare = detection(bars).model_copy(update={"mtf": None})
    result = run([bare, detection(bars, ident="det-2")], bars)
    assert result.without_context == 1
    assert result.identical


def test_a_timeframe_the_replay_cannot_read_is_its_own_bucket() -> None:
    """Almost always buffer depth, not arithmetic: an H4 read needs fifty aggregated bars,
    which is more than a week of M5. Reporting it as a mismatch would blame the aggregation."""
    deep = candles(3000)
    stored = context_at(deep)
    shallow = deep[-100:]
    subject = detection(deep, context=stored).model_copy(
        update={"candle_open_time": shallow[-1].open_time}
    )
    result = run([subject], shallow)
    assert result.unreadable
    assert any(tf == Timeframe.H4.value for _, tf in result.unreadable)
    assert "buffer depth" in result.render()


def test_nothing_compared_is_not_verified() -> None:
    """A run that read no context at all has zero disagreements. Handing out the marker for
    having looked at nothing is the failure this guards."""
    result = run([], candles(600))
    assert not result.identical
    assert result.compared == 0
    assert tool.MTF_VERIFIED_MARKER not in result.render()


# ── what it deliberately does not check ───────────────────────────────────────


def test_d1_is_excluded_and_the_output_says_so() -> None:
    """A broker day's length depends on the session and the calendar, so a D1 mismatch would be
    a statement about the calendar rather than about the aggregation."""
    assert Timeframe.D1 in tool.MTF_EXCLUDED
    assert Timeframe.D1 not in tool.MTF_CHECKED
    assert set(tool.MTF_CHECKED) | set(tool.MTF_EXCLUDED) == set(mtf.DERIVED)

    rendered = run([detection(candles(3000))], candles(3000)).render()
    assert "excluded" in rendered
    assert Timeframe.D1.value in rendered


def test_the_ema_pair_is_checked_rather_than_assumed() -> None:
    """A bias from 20/50 and one from 9/21 are different claims. Recomputing with the wrong pair
    would report every read as disagreeing -- a true statement about the wrong question."""
    bars = candles(3000)
    stored = context_at(bars).model_copy(
        update={"ema_fast_period": 9, "ema_slow_period": 21}
    )
    message = tool._pair_mismatch([detection(bars, context=stored)], FAST, SLOW)
    assert message is not None
    assert "9/21" in message and "20/50" in message
    assert tool._pair_mismatch([detection(bars)], FAST, SLOW) is None


def test_a_context_from_before_the_pair_was_recorded_is_not_a_mismatch() -> None:
    bars = candles(3000)
    stored = context_at(bars).model_copy(
        update={"ema_fast_period": 0, "ema_slow_period": 0}
    )
    assert tool._pair_mismatch([detection(bars, context=stored)], FAST, SLOW) is None


# ── the window ────────────────────────────────────────────────────────────────


def test_preceding_days_are_calendar_days_oldest_first() -> None:
    assert tool.preceding_days("2026-09-16", 3) == [
        "2026-09-13",
        "2026-09-14",
        "2026-09-15",
        "2026-09-16",
    ]
    assert tool.preceding_days("2026-09-16", 0) == ["2026-09-16"]


def test_a_missing_archived_day_is_skipped_rather_than_raised_on(tmp_path) -> None:
    """The buffer is a rolling window: a run that asked for ten days and found six has a
    shallower H4 than the live observer did, which shows up as 'unreadable'."""
    from aureon.data.live_candle_archive import LiveCandleArchive

    archive = LiveCandleArchive(root=tmp_path)
    for candle in candles(600):
        archive.add(candle)
    archive.flush_all()

    found, days = tool.read_days(
        SYMBOL,
        Timeframe.M5,
        ["2026-09-01", "2026-09-14", "2026-09-15"],
        market_tz=TZ,
        root=tmp_path,
    )
    assert days >= 1
    assert found
    assert found == sorted(found, key=lambda c: c.open_time.utc)


# ── the session verifier's MTF check ──────────────────────────────────────────


def test_the_verifier_skips_the_mtf_check_when_none_is_wired() -> None:
    """SKIP, not FAIL. A session verified before this check existed is not retroactively
    unverified, and defaulting to a run would read ten days of history the caller did not ask
    about and would not see in the output."""
    from aureon.services.session_verifier import SessionVerifier

    stub = type("V", (), {"market_date": "2026-09-16"})()
    stub.mtf_runner = None
    stub.blocks = []
    check = SessionVerifier.check_mtf_replay(stub)
    assert check.name == "mtf_replay"
    assert check.status.value in {"skip", "SKIP"}
    assert "--mtf" in (check.remedy or "")


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [(0, "pass"), (1, "fail"), (2, "skip")],
)
def test_the_verifier_maps_each_exit_code(exit_code: int, expected: str) -> None:
    """Exit 2 is SKIP rather than FAIL: the comparison could not RUN, which is a statement
    about the archive, not about the observer. Calling it a failure would tell an operator the
    session was bad when nothing was compared."""
    from aureon.services.session_verifier import SessionVerifier

    class Result:
        output = "..."

        def __init__(self, code: int) -> None:
            self.exit_code = code

    stub = type("V", (), {"market_date": "2026-09-16"})()
    # Set on the INSTANCE, not in the class body: a callable in a class body becomes a bound
    # method and would be handed a ``self`` the runner does not take.
    stub.mtf_runner = lambda: Result(exit_code)
    stub.blocks = []
    check = SessionVerifier.check_mtf_replay(stub)
    assert check.status.value.lower() == expected
