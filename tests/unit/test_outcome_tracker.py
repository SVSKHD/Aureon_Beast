"""Outcome tracking on paths whose answer is known by construction (§22, §23).

Synthetic candles, so every expected value is arithmetic rather than a golden number
copied from a previous run. ``point=1.0`` throughout, so one price unit is one point
and "+12" in the test reads as "+12 points" in the assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aureon.evaluation.outcome_tracker import OutcomeTracker
from aureon.evaluation.rules import (
    EMA_OUTCOME_V1,
    HORIZON_5_CANDLES,
    HORIZON_20_CANDLES,
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
from aureon.models.identity import detection_id
from aureon.models.market import Candle

TZ = "Europe/Athens"
START = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)  # a Wednesday, London session
POINT = 1.0


def candle(
    index: int, *, high: float, low: float, open_: float = 100.0, close: float = 100.0
) -> Candle:
    opened = START + timedelta(minutes=5 * index)
    return Candle(
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        open_time=MarketTime.from_utc(opened, TZ),
        open=open_,
        high=high,
        low=low,
        close=close,
    )


def flat_candle(index: int, price: float) -> Candle:
    return candle(index, high=price, low=price, open_=price, close=price)


def make_detection(direction: Direction = Direction.BUY, *, index: int = 0) -> Detection:
    origin = flat_candle(index, 100.0)
    return Detection(
        detection_id=detection_id(
            account_scope="primary",
            symbol="XAUUSD",
            timeframe="M5",
            agent_name="ema_cross",
            event_key="bullish" if direction is Direction.BUY else "bearish",
            candle_time=origin.open_time.utc,
        ),
        account_scope="primary",
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        agent_name="ema_cross",
        agent_version="1.0.0",
        event_key="bullish" if direction is Direction.BUY else "bearish",
        direction=direction,
        detected_at=MarketTime.from_utc(origin.close_time, TZ),
        candle_open_time=origin.open_time,
        price=100.0,
        session=SessionContext(session=SessionName.LONDON, session_config_version=1),
        sequence_today=1,
        sequence_session=1,
    )


def tracker() -> OutcomeTracker:
    return OutcomeTracker(EMA_OUTCOME_V1, market_tz=TZ, point=POINT)


def horizon(track: OutcomeTracker, detection: Detection, horizon_id: str):
    evaluation = track.evaluation_for(detection.detection_id)
    assert evaluation is not None
    return next(h for h in evaluation.horizons if h.horizon_id == horizon_id)


# ── Straight up: +12, reaching 3/5/10 but not 15/20 ───────────────────────────


def test_a_straight_run_up_reaches_the_lower_thresholds() -> None:
    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)

    # Reference is the NEXT open = 100. Then price climbs to +12 and stops.
    for i, high in enumerate([102.0, 105.0, 108.0, 111.0, 112.0], start=1):
        track.on_closed_candle(candle(i, high=high, low=100.0, open_=100.0, close=high))

    result = horizon(track, detection, HORIZON_5_CANDLES)
    assert result.status is HorizonStatus.COMPLETE
    assert result.mfe == pytest.approx(12.0)
    assert result.reached_threshold(3) is True
    assert result.reached_threshold(5) is True
    assert result.reached_threshold(10) is True
    assert result.reached_threshold(15) is False
    assert result.reached_threshold(20) is False
    assert result.path is PathClassification.MFE_FIRST
    assert result.path_ambiguous is False


def test_time_to_is_measured_from_the_detection_in_seconds() -> None:
    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    for i, high in enumerate([102.0, 105.0, 108.0, 111.0, 112.0], start=1):
        track.on_closed_candle(candle(i, high=high, low=100.0, open_=100.0, close=high))

    result = horizon(track, detection, HORIZON_5_CANDLES)
    # +3 first cleared on the 2nd candle after the detection, known at its close:
    # detection closed at START+5m; that candle closes at START+15m.
    assert result.time_to["3"] == pytest.approx(600.0)
    assert result.time_to["15"] is None, "an unreached threshold has no time"


def test_a_sell_detection_measures_downward_moves_as_favourable() -> None:
    """BUY and SELL must be directly comparable, so excursions are signed by direction."""
    track = tracker()
    detection = make_detection(Direction.SELL)
    track.track(detection)
    for i, low in enumerate([98.0, 95.0, 92.0, 89.0, 88.0], start=1):
        track.on_closed_candle(candle(i, high=100.0, low=low, open_=100.0, close=low))

    result = horizon(track, detection, HORIZON_5_CANDLES)
    assert result.mfe == pytest.approx(12.0)
    assert result.reached_threshold(10) is True
    assert result.mfe_price == pytest.approx(88.0), "a SELL's favourable extreme is the low"


# ── Dip then rally: MAE first ─────────────────────────────────────────────────


def test_a_dip_before_the_rally_is_classified_mae_first() -> None:
    """-4 then +12. The trade worked, but only after going against the holder first.

    This is the distinction that matters: a detection a human could have held versus
    one that would have stopped them out on the way.
    """
    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)

    track.on_closed_candle(candle(1, high=100.0, low=96.0, open_=100.0, close=96.0))
    for i, high in enumerate([104.0, 108.0, 111.0, 112.0], start=2):
        track.on_closed_candle(candle(i, high=high, low=96.0, open_=96.0, close=high))

    result = horizon(track, detection, HORIZON_5_CANDLES)
    assert result.status is HorizonStatus.COMPLETE
    assert result.mae == pytest.approx(-4.0)
    assert result.mfe == pytest.approx(12.0)
    assert result.reached_threshold(10) is True
    assert result.path is PathClassification.MAE_FIRST


def test_a_same_candle_crossing_is_flagged_ambiguous() -> None:
    """When both thresholds fall in one candle, the order is unobservable.

    §23's wording puts it in MFE_FIRST -- the optimistic side -- so the flag records
    that the order was never actually seen and a review can exclude it.
    """
    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    # One candle spanning both +3 and -3 relative to the 100.0 reference.
    track.on_closed_candle(candle(1, high=110.0, low=90.0, open_=100.0, close=100.0))

    result = horizon(track, detection, HORIZON_5_CANDLES)
    assert result.path is PathClassification.MFE_FIRST
    assert result.path_ambiguous is True


# ── Flat: complete, and nothing reached ───────────────────────────────────────


def test_a_flat_path_completes_with_every_threshold_false() -> None:
    """COMPLETE-and-false is a real answer, and must not look like PENDING."""
    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    for i in range(1, 6):
        track.on_closed_candle(candle(i, high=100.5, low=99.5, open_=100.0, close=100.0))

    result = horizon(track, detection, HORIZON_5_CANDLES)
    assert result.status is HorizonStatus.COMPLETE
    assert all(v is False for v in result.reached.values())
    assert all(v is None for v in result.time_to.values())
    assert result.path is PathClassification.NONE
    # And it IS an answer: reached_threshold returns False, not None.
    assert result.reached_threshold(3) is False


# ── Truncated stream: still unknown ───────────────────────────────────────────


def test_a_truncated_stream_leaves_the_long_horizon_pending() -> None:
    """The heart of the phase: unknown must stay unknown.

    Five candles answer the 5-candle horizon but say nothing about the 20-candle one,
    which must remain PENDING rather than be closed as a miss.
    """
    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    for i in range(1, 6):
        track.on_closed_candle(candle(i, high=101.0, low=99.0, open_=100.0, close=100.0))

    evaluation = track.evaluation_for(detection.detection_id)
    assert evaluation is not None

    short = horizon(track, detection, HORIZON_5_CANDLES)
    long = horizon(track, detection, HORIZON_20_CANDLES)
    assert short.status is HorizonStatus.COMPLETE
    assert long.status is HorizonStatus.PENDING
    # PENDING reads as "unknown", never as "no".
    assert long.reached_threshold(3) is None
    assert short in evaluation.complete_horizons
    assert long in evaluation.pending_horizons
    assert evaluation.is_fully_evaluated is False


# ── Idempotence and freezing ──────────────────────────────────────────────────


def test_a_complete_horizon_never_changes_on_further_candles() -> None:
    """Re-running must be a no-op, or the backfill could rewrite history."""
    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    for i in range(1, 6):
        track.on_closed_candle(candle(i, high=101.0, low=99.0, open_=100.0, close=100.0))

    before = horizon(track, detection, HORIZON_5_CANDLES).model_dump(mode="json")
    # A huge move AFTER the horizon closed must not touch it.
    for i in range(6, 12):
        track.on_closed_candle(candle(i, high=500.0, low=99.0, open_=100.0, close=500.0))
    after = horizon(track, detection, HORIZON_5_CANDLES).model_dump(mode="json")

    assert before == after


def test_re_tracking_a_detection_does_not_restart_it() -> None:
    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    for i in range(1, 6):
        track.on_closed_candle(candle(i, high=112.0, low=100.0, open_=100.0, close=112.0))

    before = track.evaluation_for(detection.detection_id)
    track.track(detection)
    after = track.evaluation_for(detection.detection_id)
    assert before is not None and after is not None
    assert [h.model_dump(mode="json") for h in before.horizons] == [
        h.model_dump(mode="json") for h in after.horizons
    ]


# ── Gaps ──────────────────────────────────────────────────────────────────────


def test_a_large_gap_invalidates_rather_than_completes() -> None:
    """§22. Price moved while nothing was observed, so an excursion figure would be a
    lower bound presented as a measurement."""
    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    track.on_closed_candle(candle(1, high=101.0, low=99.0, open_=100.0, close=100.0))
    # Jump far past the 2-timeframe tolerance.
    track.on_closed_candle(candle(50, high=101.0, low=99.0, open_=100.0, close=100.0))

    result = horizon(track, detection, HORIZON_5_CANDLES)
    assert result.status is HorizonStatus.INVALID
    assert result.invalid_reason
    assert result.reached_threshold(3) is None, "INVALID is not an answer either"


def test_a_small_gap_is_tolerated() -> None:
    """One missing candle is normal thin-market behaviour, not a data failure."""
    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    track.on_closed_candle(candle(1, high=101.0, low=99.0, open_=100.0, close=100.0))
    track.on_closed_candle(candle(3, high=101.0, low=99.0, open_=100.0, close=100.0))

    assert horizon(track, detection, HORIZON_5_CANDLES).status is HorizonStatus.PENDING


# ── Scope ─────────────────────────────────────────────────────────────────────


def test_context_only_detections_are_not_evaluated() -> None:
    """Without a direction there is no favourable side, so "did it work?" has no
    meaning (decision 48)."""
    track = tracker()
    context = make_detection(Direction.BUY).model_copy(update={"direction": None})
    assert track.track(context) is None
    assert track.tracked_count == 0


def test_the_detections_own_candle_is_not_measured() -> None:
    """That bar is already history when the detection becomes known."""
    track = tracker()
    detection = make_detection(Direction.BUY, index=0)
    track.track(detection)
    # Re-feeding the detection's own candle must change nothing.
    track.on_closed_candle(flat_candle(0, 100.0))
    assert horizon(track, detection, HORIZON_5_CANDLES).candles_seen == 0


def test_the_reference_is_the_next_open_not_the_detection_price() -> None:
    """A human cannot transact at the close that produced the detection."""
    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    # Next candle opens with a gap up to 105; +10 from THERE is 115, not 110.
    track.on_closed_candle(candle(1, high=115.0, low=105.0, open_=105.0, close=115.0))

    evaluation = track.evaluation_for(detection.detection_id)
    assert evaluation is not None
    assert evaluation.reference_value == pytest.approx(105.0)
    assert horizon(track, detection, HORIZON_5_CANDLES).mfe == pytest.approx(10.0)


def test_another_symbol_does_not_advance_an_evaluation() -> None:
    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    other = candle(1, high=200.0, low=100.0).model_copy(update={"symbol": "EURUSD"})
    track.on_closed_candle(other)
    assert horizon(track, detection, HORIZON_5_CANDLES).candles_seen == 0


def test_memory_is_released_once_everything_resolves() -> None:
    """The live observer runs indefinitely; finished evaluations must not accumulate."""
    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    assert track.tracked_count == 1
    assert track.release_closed() == 0, "still open; nothing to release"


# ── Event-driven horizons ─────────────────────────────────────────────────────


def test_the_minute_horizon_closes_on_elapsed_time() -> None:
    from aureon.evaluation.rules import HORIZON_60_MINUTES

    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    # 11 M5 candles = 55 minutes from the detection's close; the 12th reaches 60.
    for i in range(1, 12):
        track.on_closed_candle(candle(i, high=101.0, low=99.0, open_=100.0, close=100.0))
    assert horizon(track, detection, HORIZON_60_MINUTES).status is HorizonStatus.PENDING

    track.on_closed_candle(candle(12, high=101.0, low=99.0, open_=100.0, close=100.0))
    assert horizon(track, detection, HORIZON_60_MINUTES).status is HorizonStatus.COMPLETE


def test_the_session_horizon_closes_when_the_session_changes() -> None:
    from aureon.evaluation.rules import HORIZON_SESSION_CLOSE

    track = tracker()
    detection = make_detection(Direction.BUY)  # 13:00 Athens: London
    track.track(detection)

    # Contiguous candles, because jumping ahead would (correctly) trip the gap rule and
    # mark the horizon INVALID rather than COMPLETE. London runs to 18:00 Athens =
    # 15:00 UTC, which is candle index 60 from a 10:00 UTC start.
    for i in range(1, 60):
        track.on_closed_candle(candle(i, high=101.0, low=99.0, open_=100.0, close=100.0))
    assert horizon(track, detection, HORIZON_SESSION_CLOSE).status is HorizonStatus.PENDING

    track.on_closed_candle(candle(60, high=101.0, low=99.0, open_=100.0, close=100.0))
    assert horizon(track, detection, HORIZON_SESSION_CLOSE).status is HorizonStatus.COMPLETE


def test_the_day_horizon_closes_on_a_new_broker_day() -> None:
    from aureon.evaluation.rules import HORIZON_DAY_CLOSE

    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)

    # The broker day ends at midnight Athens = 21:00 UTC in summer, which is candle
    # index 132 from a 10:00 UTC start. Contiguous, to avoid the gap rule.
    for i in range(1, 132):
        track.on_closed_candle(candle(i, high=101.0, low=99.0, open_=100.0, close=100.0))
    assert horizon(track, detection, HORIZON_DAY_CLOSE).status is HorizonStatus.PENDING

    track.on_closed_candle(candle(132, high=101.0, low=99.0, open_=100.0, close=100.0))
    result = horizon(track, detection, HORIZON_DAY_CLOSE)
    assert result.status is HorizonStatus.COMPLETE


def test_the_opposite_cross_horizon_closes_on_a_reversing_detection() -> None:
    from aureon.evaluation.rules import HORIZON_OPPOSITE_CROSS

    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    track.on_closed_candle(candle(1, high=101.0, low=99.0, open_=100.0, close=100.0))
    assert horizon(track, detection, HORIZON_OPPOSITE_CROSS).status is HorizonStatus.PENDING

    # Same direction: not an opposite cross.
    same = make_detection(Direction.BUY, index=5)
    track.on_detection(same)
    assert horizon(track, detection, HORIZON_OPPOSITE_CROSS).status is HorizonStatus.PENDING

    opposite = make_detection(Direction.SELL, index=6)
    track.on_detection(opposite)
    assert horizon(track, detection, HORIZON_OPPOSITE_CROSS).status is HorizonStatus.COMPLETE


def test_an_opposite_detection_from_another_agent_is_ignored() -> None:
    """The horizon waits for this agent reversing, not for any bearish opinion."""
    from aureon.evaluation.rules import HORIZON_OPPOSITE_CROSS

    track = tracker()
    detection = make_detection(Direction.BUY)
    track.track(detection)
    track.on_closed_candle(candle(1, high=101.0, low=99.0, open_=100.0, close=100.0))

    foreign = make_detection(Direction.SELL, index=6).model_copy(
        update={"agent_name": "liquidity"}
    )
    track.on_detection(foreign)
    assert horizon(track, detection, HORIZON_OPPOSITE_CROSS).status is HorizonStatus.PENDING


# ── The rule stays frozen ─────────────────────────────────────────────────────


def test_a_shipped_rule_cannot_be_retuned() -> None:
    """§21. Editing thresholds in place would silently redefine every stored result."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EMA_OUTCOME_V1.thresholds = (1.0, 2.0)


def test_the_registry_refuses_a_redefinition() -> None:
    from aureon.evaluation.rules import RuleFrozenError, register
    from aureon.models.evaluation import EvaluationRule

    changed = EvaluationRule(
        rule_id=EMA_OUTCOME_V1.rule_id,
        reference_price=EMA_OUTCOME_V1.reference_price,
        horizons=EMA_OUTCOME_V1.horizons,
        thresholds=(1.0, 2.0, 3.0),
    )
    with pytest.raises(RuleFrozenError, match="frozen"):
        register(changed)


def test_re_registering_the_identical_rule_is_a_no_op() -> None:
    from aureon.evaluation.rules import register

    assert register(EMA_OUTCOME_V1) is EMA_OUTCOME_V1
