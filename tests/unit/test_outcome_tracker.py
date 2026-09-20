"""Outcome tracking on paths whose answer is known by construction (§22, §23).

Synthetic candles, so every expected value is arithmetic rather than a golden number
copied from a previous run. ``point=1.0`` throughout, so one price unit is one point
and "+12" in the test reads as "+12 points" in the assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

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
            candle_close=origin.close_time,
            agent_name="ema_cross",
            agent_version="1.0.0",
            event_key="bullish" if direction is Direction.BUY else "bearish",
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


# ── Price-unit thresholds (XAU_OUTCOME_V2, §21) ───────────────────────────────


def v2_tracker(point: float = 0.01) -> OutcomeTracker:
    from aureon.evaluation.rules import XAU_OUTCOME_V2

    return OutcomeTracker(XAU_OUTCOME_V2, market_tz=TZ, point=point)


def test_a_four_dollar_move_reaches_three_but_not_five() -> None:
    """The acceptance case for V2: thresholds mean DOLLARS, not points.

    +$4 must satisfy the $3 threshold and fail the $5 one. Under V1's point-scale the
    same move is +400 points and would have satisfied every threshold up to $0.20 --
    which is exactly the saturation V2 exists to fix.
    """
    tracker = v2_tracker()
    detection = make_detection(Direction.BUY, index=0)
    tracker.track(detection)

    # Reference is next_open = 100.0. Run to 104.0: a $4 favourable excursion.
    tracker.on_closed_candle(flat_candle(1, 100.0))
    tracker.on_closed_candle(candle(2, open_=100.0, high=104.0, low=100.0, close=104.0))
    for index in range(3, 12):
        tracker.on_closed_candle(flat_candle(index, 104.0))

    evaluation = tracker.evaluation_for(detection.detection_id)
    assert evaluation is not None
    horizon = next(
        h for h in evaluation.complete_horizons if h.horizon_id == HORIZON_5_CANDLES
    )
    assert horizon.reached["3"] is True, "$4 should reach the $3 threshold"
    assert horizon.reached["5"] is not True, "$4 must not reach the $5 threshold"
    assert horizon.reached["10"] is not True


def test_the_same_move_is_measured_against_the_symbols_own_point() -> None:
    """A $4 move is $4 whatever the tick size is.

    Run twice with different ``point`` values and the SAME prices. If the conversion
    were hard-coded rather than derived, one of these two would come out wrong.
    """
    outcomes = []
    for point in (0.01, 0.1):
        tracker = v2_tracker(point)
        detection = make_detection(Direction.BUY, index=0)
        tracker.track(detection)
        tracker.on_closed_candle(flat_candle(1, 100.0))
        tracker.on_closed_candle(
            candle(2, open_=100.0, high=104.0, low=100.0, close=104.0)
        )
        for index in range(3, 12):
            tracker.on_closed_candle(flat_candle(index, 104.0))
        evaluation = tracker.evaluation_for(detection.detection_id)
        assert evaluation is not None
        horizon = next(
            h for h in evaluation.complete_horizons if h.horizon_id == HORIZON_5_CANDLES
        )
        outcomes.append((horizon.reached.get("3"), horizon.reached.get("5")))

    assert outcomes[0] == outcomes[1] == (True, None) or outcomes[0] == outcomes[1], (
        f"the same $4 move gave different answers at different tick sizes: {outcomes}"
    )
    assert outcomes[0][0] is True
    assert outcomes[0][1] is not True


def test_a_points_rule_is_unaffected_by_the_conversion() -> None:
    """V1 keeps behaving exactly as it did: 3 points is $0.03 at point=0.01.

    A $4 move is 400 points, so under V1 every threshold up to 20 points is reached --
    the saturation the Phase 2 baseline reported as a finding.
    """
    tracker = OutcomeTracker(EMA_OUTCOME_V1, market_tz=TZ, point=0.01)
    detection = make_detection(Direction.BUY, index=0)
    tracker.track(detection)
    tracker.on_closed_candle(flat_candle(1, 100.0))
    tracker.on_closed_candle(candle(2, open_=100.0, high=104.0, low=100.0, close=104.0))
    for index in range(3, 12):
        tracker.on_closed_candle(flat_candle(index, 104.0))

    evaluation = tracker.evaluation_for(detection.detection_id)
    assert evaluation is not None
    horizon = next(
        h for h in evaluation.complete_horizons if h.horizon_id == HORIZON_5_CANDLES
    )
    assert all(horizon.reached.get(key) for key in ("3", "5", "10", "15", "20"))


def test_a_sell_detection_measures_the_downside_in_price() -> None:
    """Direction-agnostic: -$4 on a SELL is the same $4 favourable move."""
    tracker = v2_tracker()
    detection = make_detection(Direction.SELL, index=0)
    tracker.track(detection)
    tracker.on_closed_candle(flat_candle(1, 100.0))
    tracker.on_closed_candle(candle(2, open_=100.0, high=100.0, low=96.0, close=96.0))
    for index in range(3, 12):
        tracker.on_closed_candle(flat_candle(index, 96.0))

    evaluation = tracker.evaluation_for(detection.detection_id)
    assert evaluation is not None
    horizon = next(
        h for h in evaluation.complete_horizons if h.horizon_id == HORIZON_5_CANDLES
    )
    assert horizon.reached["3"] is True
    assert horizon.reached["5"] is not True


# ── Which agents get evaluated (D-4, §22) ─────────────────────────────────────


def test_every_directional_agent_is_evaluated_and_context_only_agents_are_not() -> None:
    """Outcomes follow DIRECTION, not agent name.

    The tracker has no agent allowlist, deliberately: "does this work?" is a question
    about any detection with a favourable side, and an allowlist would silently answer it
    for `ema_cross` alone while `liquidity` and `breakout` accumulated unevaluated. That
    is a gap nothing in the reviews would reveal -- a sweep with no evaluation simply does
    not appear in a reached-N table, so the table looks complete.

    The converse matters just as much: `wick`, `rsi` and `session_trend` emit
    `direction=None`, so there is no favourable side to measure. Evaluating them would
    require inventing a direction, and a reached-N figure built on an invented direction
    describes the invention.

    Driven through a real fixture replay with the whole roster, because the property is
    about what the roster produces, not about what one hand-built detection does.
    """
    from collections import Counter

    from aureon.agents.breakout_agent import BreakoutAgent
    from aureon.agents.ema_cross_agent import EmaCrossAgent
    from aureon.agents.liquidity_agent import LiquidityAgent
    from aureon.agents.rsi_agent import RsiAgent
    from aureon.agents.session_trend_agent import SessionTrendAgent
    from aureon.agents.wick_agent import WickAgent
    from aureon.data.historical_provider import HistoricalDataProvider
    from aureon.engine.levels import LevelTracker
    from aureon.evaluation.backfill import run_backfill
    from aureon.evaluation.rules import XAU_OUTCOME_V2

    # The REAL instrument tick, not this module's POINT. The synthetic candles above sit
    # around price 100 with POINT = 1.0; XAUUSD's tick is 0.01, and feeding 1.0 makes the
    # level agents' thresholds a hundred times too wide, so liquidity emits nothing and
    # the test passes vacuously on an empty population.
    fixture_point = 0.01
    fixture = (
        Path(__file__).resolve().parents[2]
        / "aureon"
        / "data"
        / "fixtures"
        / "XAUUSD_M5.csv"
    )
    candles = HistoricalDataProvider(fixture, market_tz=TZ).candles[:900]

    levels = LevelTracker()  # shared by liquidity and breakout (§15, §17)
    result = run_backfill(
        candles,
        [
            EmaCrossAgent(fast_period=20, slow_period=50),
            RsiAgent(),
            SessionTrendAgent(point=fixture_point),
            WickAgent(point=fixture_point),
            LiquidityAgent(point=fixture_point, level_tracker=levels),
            BreakoutAgent(point=fixture_point, level_tracker=levels),
        ],
        XAU_OUTCOME_V2,
        account_scope="primary",
        market_tz=TZ,
        point=fixture_point,
    )

    agent_of = {d.detection_id: d.agent_name for d in result.detections}
    produced = Counter(agent_of.values())
    evaluated = Counter(
        agent_of[e.detection_id] for e in result.evaluations if e.detection_id in agent_of
    )

    # The fixture must actually exercise every agent, or this proves nothing.
    for agent in ("ema_cross", "liquidity", "breakout", "wick"):
        assert produced[agent] > 0, f"the fixture produced no {agent} detections"

    # Directional agents: every detection evaluated, none missed.
    for agent in ("ema_cross", "liquidity", "breakout"):
        assert evaluated[agent] == produced[agent], (
            f"{agent}: {produced[agent]} detections but {evaluated[agent]} evaluations"
        )

    # Context-only agents: none evaluated at all.
    for agent in ("wick", "rsi", "session_trend"):
        assert evaluated[agent] == 0, (
            f"{agent} emits direction=None and must not be evaluated; "
            f"got {evaluated[agent]} evaluations"
        )


def test_the_filter_is_direction_not_an_agent_allowlist() -> None:
    """A directional detection from an unheard-of agent is still evaluated.

    Pinned separately because the fixture test above would keep passing if someone added
    an allowlist that happened to name the three agents the fixture uses. This one fails
    the moment the rule stops being "has a direction".
    """
    tracker = OutcomeTracker(EMA_OUTCOME_V1, market_tz=TZ, point=POINT)
    invented = make_detection(Direction.BUY, index=0).model_copy(
        update={"agent_name": "an_agent_nobody_has_written_yet"}
    )

    assert tracker.track(invented) is not None, (
        "an evaluation was refused on the basis of the agent's name"
    )


def test_a_context_only_detection_is_refused_whatever_its_agent() -> None:
    tracker = OutcomeTracker(EMA_OUTCOME_V1, market_tz=TZ, point=POINT)
    context_only = make_detection(Direction.BUY, index=0).model_copy(
        update={"direction": None, "agent_name": "ema_cross"}
    )

    assert tracker.track(context_only) is None
