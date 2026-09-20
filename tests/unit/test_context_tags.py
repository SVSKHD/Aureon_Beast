"""Context tags, and the no-hindsight rule they could most easily break (§19, §23).

The tags partition a population of detections so a review can compare "bullish cross
with a lower rejection wick" against "without" instead of averaging the two together.
That is only worth anything if the tag describes what was knowable when the detection
fired.

This module is the easiest place in the system to leak the future. "Look at the
detections around this one" has no natural sense of direction in time, and a wick that
forms on the NEXT candle would tag the cross retroactively -- producing a correlation
that looks real and cannot be falsified, because the tag was computed from the outcome's
own neighbourhood. So the first tests here are about time, not about wicks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.evaluation.context_tags import (
    CONTEXT_TAGS,
    TAG_LOWER_REJECTION_WICK,
    TAG_SESSION_TREND_ALIGNED,
    TAG_SWEEP_SAME_DIRECTION,
    TAG_UPPER_REJECTION_WICK,
    derive,
    derive_all,
    window_for,
)
from aureon.models.base import MarketTime
from aureon.models.detection import Detection, SessionContext
from aureon.models.enums import Direction, SessionName, Timeframe
from aureon.models.identity import detection_id

TZ = "Europe/Athens"
# A Wednesday inside London, so the session is never in question.
BASE = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
CANDLE = timedelta(minutes=5)


def detection(
    *,
    agent: str,
    event_key: str,
    candles: float = 0.0,
    direction: Direction | None = None,
    symbol: str = "XAUUSD",
    timeframe: Timeframe = Timeframe.M5,
) -> Detection:
    """A detection ``candles`` candles after BASE. Negative is earlier."""
    close = BASE + CANDLE * candles
    return Detection(
        detection_id=detection_id(
            account_scope="primary",
            symbol=symbol,
            timeframe=timeframe.value,
            candle_close=close,
            agent_name=agent,
            agent_version="1.0.0",
            event_key=event_key,
        ),
        account_scope="primary",
        symbol=symbol,
        timeframe=timeframe,
        agent_name=agent,
        agent_version="1.0.0",
        event_key=event_key,
        direction=direction,
        detected_at=MarketTime.from_utc(close, TZ),
        candle_open_time=MarketTime.from_utc(close - CANDLE, TZ),
        price=2400.0,
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
    )


def bullish_cross(candles: float = 0.0) -> Detection:
    return detection(
        agent="ema_cross", event_key="bullish", candles=candles, direction=Direction.BUY
    )


def bearish_cross(candles: float = 0.0) -> Detection:
    return detection(
        agent="ema_cross", event_key="bearish", candles=candles, direction=Direction.SELL
    )


def lower_wick(candles: float) -> Detection:
    return detection(agent="wick", event_key="lower_rejection", candles=candles)


def upper_wick(candles: float) -> Detection:
    return detection(agent="wick", event_key="upper_rejection", candles=candles)


def sweep(candles: float, direction: Direction) -> Detection:
    return detection(
        agent="liquidity",
        event_key="up|swing_high" if direction is Direction.SELL else "down|swing_low",
        candles=candles,
        direction=direction,
    )


def session_trend(candles: float, trend: str) -> Detection:
    return detection(
        agent="session_trend", event_key=f"london|{trend}", candles=candles
    )


# ── No hindsight: the property the rest depends on (§19) ──────────────────────


def test_a_wick_on_the_next_candle_does_not_tag_the_cross() -> None:
    """The one that matters.

    A wick one candle LATER was not knowable when the cross fired. Tagging the cross
    with it would manufacture a correlation between the cross and its own future, and
    nothing downstream could tell the difference.
    """
    cross = bullish_cross()
    future_wick = lower_wick(candles=1)
    assert future_wick.detected_at.utc > cross.detected_at.utc

    tags = derive(cross, [cross, future_wick])

    assert tags[TAG_LOWER_REJECTION_WICK] is False, (
        "a wick on the following candle tagged the cross -- hindsight leaked"
    )


def test_a_wick_on_the_same_candle_close_does_tag_the_cross() -> None:
    """Same close means same instant of knowledge, so it is context, not the future."""
    cross = bullish_cross()
    tags = derive(cross, [cross, lower_wick(candles=0)])
    assert tags[TAG_LOWER_REJECTION_WICK] is True


def test_a_wick_before_the_cross_tags_it() -> None:
    cross = bullish_cross()
    tags = derive(cross, [cross, lower_wick(candles=-2)])
    assert tags[TAG_LOWER_REJECTION_WICK] is True


def test_nothing_after_the_subject_enters_the_window() -> None:
    """Checked at the window rather than through a tag, so the rule is pinned once."""
    cross = bullish_cross()
    later = [lower_wick(candles=c) for c in (0.5, 1, 2, 10)]
    window = window_for(cross, [cross, *later])
    assert window.prior == ()


@pytest.mark.parametrize("offset", [1, 2, 5, 100])
def test_no_tag_is_ever_set_by_a_later_detection(offset: int) -> None:
    """Every tag, not just the wick one, must be blind to the future."""
    cross = bullish_cross()
    future = [
        lower_wick(candles=offset),
        upper_wick(candles=offset),
        sweep(offset, Direction.BUY),
        session_trend(offset, "up"),
    ]
    tags = derive(cross, [cross, *future])
    assert tags == dict.fromkeys(CONTEXT_TAGS, False)


# ── The window ────────────────────────────────────────────────────────────────


def test_the_window_is_measured_in_candles_not_minutes() -> None:
    """N=3 on M5 is fifteen minutes; on M15 it is forty-five."""
    cross_m5 = bullish_cross()
    assert derive(cross_m5, [cross_m5, lower_wick(candles=-3)], window_candles=3)[
        TAG_LOWER_REJECTION_WICK
    ] is True
    assert derive(cross_m5, [cross_m5, lower_wick(candles=-4)], window_candles=3)[
        TAG_LOWER_REJECTION_WICK
    ] is False


def test_the_window_size_is_configurable() -> None:
    cross = bullish_cross()
    wick = lower_wick(candles=-5)
    assert derive(cross, [cross, wick], window_candles=3)[TAG_LOWER_REJECTION_WICK] is False
    assert derive(cross, [cross, wick], window_candles=6)[TAG_LOWER_REJECTION_WICK] is True


def test_another_symbol_is_not_context() -> None:
    cross = bullish_cross()
    other = detection(agent="wick", event_key="lower_rejection", symbol="EURUSD")
    assert derive(cross, [cross, other])[TAG_LOWER_REJECTION_WICK] is False


def test_another_timeframe_is_not_context() -> None:
    """An M15 wick is a different observation, not the same one seen twice."""
    cross = bullish_cross()
    other = detection(
        agent="wick", event_key="lower_rejection", timeframe=Timeframe.M15
    )
    assert derive(cross, [cross, other])[TAG_LOWER_REJECTION_WICK] is False


def test_a_detection_is_not_its_own_context() -> None:
    cross = bullish_cross()
    assert window_for(cross, [cross]).prior == ()


# ── What each tag means ───────────────────────────────────────────────────────


def test_the_two_wick_tags_are_independent() -> None:
    cross = bullish_cross()
    tags = derive(cross, [cross, upper_wick(candles=-1)])
    assert tags[TAG_UPPER_REJECTION_WICK] is True
    assert tags[TAG_LOWER_REJECTION_WICK] is False


def test_a_sweep_counts_only_when_it_points_the_same_way() -> None:
    """Same implied direction, which is the tradeable side of a sweep."""
    cross = bullish_cross()
    assert derive(cross, [cross, sweep(-1, Direction.BUY)])[TAG_SWEEP_SAME_DIRECTION] is True
    assert derive(cross, [cross, sweep(-1, Direction.SELL)])[TAG_SWEEP_SAME_DIRECTION] is False


def test_a_bearish_cross_aligns_with_a_bearish_sweep() -> None:
    cross = bearish_cross()
    assert derive(cross, [cross, sweep(-1, Direction.SELL)])[TAG_SWEEP_SAME_DIRECTION] is True
    assert derive(cross, [cross, sweep(-1, Direction.BUY)])[TAG_SWEEP_SAME_DIRECTION] is False


def test_session_trend_alignment_reads_the_trend_from_the_event_key() -> None:
    cross = bullish_cross()
    assert derive(cross, [cross, session_trend(-1, "up")])[TAG_SESSION_TREND_ALIGNED] is True
    assert derive(cross, [cross, session_trend(-1, "down")])[TAG_SESSION_TREND_ALIGNED] is False


def test_a_flat_session_is_never_alignment() -> None:
    """A flat session agrees with nothing.

    Calling it aligned would put every flat session in the "aligned" bucket for BOTH
    directions, which is how a tag ends up correlating with nothing at all.
    """
    for cross in (bullish_cross(), bearish_cross()):
        tags = derive(cross, [cross, session_trend(-1, "flat")])
        assert tags[TAG_SESSION_TREND_ALIGNED] is False


def test_a_context_only_subject_gets_no_directional_tags() -> None:
    """With no direction there is nothing for a sweep or trend to agree with."""
    subject = detection(agent="rsi", event_key="oversold_entry")
    tags = derive(subject, [subject, sweep(-1, Direction.BUY), session_trend(-1, "up")])
    assert tags[TAG_SWEEP_SAME_DIRECTION] is False
    assert tags[TAG_SESSION_TREND_ALIGNED] is False


# ── Shape of the result ───────────────────────────────────────────────────────


def test_every_tag_is_always_present_including_the_false_ones() -> None:
    """"We looked and it was not there" is a different claim from "we did not look".

    A review grouping by tag needs the first; a missing key gives it the second.
    """
    tags = derive(bullish_cross(), [bullish_cross()])
    assert set(tags) == set(CONTEXT_TAGS)
    assert all(value is False for value in tags.values())


def test_derive_all_tags_only_evaluable_subjects() -> None:
    """A context-only detection has no outcome to correlate the tags with."""
    cross = bullish_cross()
    wick = lower_wick(candles=-1)
    tags = derive_all([cross, wick], only_agents=frozenset({"ema_cross", "wick"}))

    assert cross.detection_id in tags
    assert wick.detection_id not in tags, "wick has direction=None and is not evaluable"


def test_derive_all_still_uses_untagged_detections_as_context() -> None:
    """The subject filter must not shrink the context.

    ``wick`` is never a subject but is the whole point of the wick tags, so filtering
    it out of the context too would make those tags permanently False.
    """
    cross = bullish_cross()
    tags = derive_all([cross, lower_wick(candles=-1)], only_agents=frozenset({"ema_cross"}))
    assert tags[cross.detection_id][TAG_LOWER_REJECTION_WICK] is True


def test_tags_are_deterministic_and_order_independent() -> None:
    cross = bullish_cross()
    context = [lower_wick(candles=-1), sweep(-2, Direction.BUY), session_trend(-3, "up")]
    first = derive(cross, [cross, *context])
    second = derive(cross, [*reversed(context), cross])
    assert first == second
