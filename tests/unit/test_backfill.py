"""Backfill and reporting over the committed fixture (Phase 3 "done when").

The unit tests in ``test_outcome_tracker`` pin the rules on synthetic paths. These
check the properties that only show up over real data: idempotence, the weekend gap
producing INVALID horizons, and -- most importantly -- that no statistic ever counts an
unknown as a miss.
"""

from __future__ import annotations

import pytest

from aureon.evaluation.backfill import build_report, format_report, run_backfill
from aureon.evaluation.rules import EMA_OUTCOME_V1, HORIZON_5_CANDLES
from aureon.models.enums import HorizonStatus
from aureon.models.market import Candle
from tests.conftest import ACCOUNT_SCOPE, MARKET_TZ, InMemoryFirestore, cross_agent


def backfill(candles: list[Candle]):
    return run_backfill(
        candles,
        [cross_agent()],
        EMA_OUTCOME_V1,
        account_scope=ACCOUNT_SCOPE,
        market_tz=MARKET_TZ,
        point=0.01,
    )


@pytest.fixture(scope="module")
def result(request):
    from aureon.data.historical_provider import HistoricalDataProvider
    from tests.conftest import FIXTURE_CSV

    provider = HistoricalDataProvider(FIXTURE_CSV, market_tz=MARKET_TZ)
    return backfill(provider.candles)


def test_the_backfill_evaluates_every_directional_detection(result) -> None:
    assert result.detections
    directional = [d for d in result.detections if d.direction is not None]
    assert result.evaluated == len(directional)
    assert result.evaluated > 20, "too few evaluations for the checks below to mean much"


def test_every_evaluation_covers_every_horizon(result) -> None:
    for evaluation in result.evaluations:
        assert {h.horizon_id for h in evaluation.horizons} == {
            h.id for h in EMA_OUTCOME_V1.horizons
        }


def test_the_backfill_is_idempotent(candles: list[Candle]) -> None:
    """Re-running must not change a single stored value.

    The whole research loop depends on it: if a second run produced different
    numbers, no recorded result could be trusted.
    """
    first = backfill(candles[:900])
    second = backfill(candles[:900])

    def comparable(evaluations):
        # updated_at is a wall-clock stamp and is expected to differ between runs;
        # everything else must be identical.
        return [
            e.model_dump(mode="json") | {"updated_at": None}
            for e in sorted(evaluations, key=lambda e: e.detection_id)
        ]

    assert comparable(first.evaluations) == comparable(second.evaluations)


def test_a_complete_horizon_carries_its_excursions(result) -> None:
    """Decision 13: a COMPLETE horizon that answered nothing would still be counted."""
    for evaluation in result.evaluations:
        for horizon in evaluation.complete_horizons:
            assert horizon.mfe is not None
            assert horizon.mae is not None
            assert horizon.future_high is not None
            assert horizon.future_low is not None
            assert horizon.completed_at is not None


def test_mae_is_never_above_mfe(result) -> None:
    """Both are signed relative to the detection's direction."""
    for evaluation in result.evaluations:
        for horizon in evaluation.complete_horizons:
            assert horizon.mae <= horizon.mfe


def test_the_weekend_gap_produces_invalid_horizons(result) -> None:
    """§22. Price moved while nothing was observed, so the excursion figures would be
    a lower bound presented as a measurement."""
    invalid = [
        horizon
        for evaluation in result.evaluations
        for horizon in evaluation.horizons
        if horizon.status is HorizonStatus.INVALID
    ]
    assert invalid, "the fixture's 49h weekend gap should invalidate some horizons"
    assert all(h.invalid_reason for h in invalid)


def test_pending_horizons_exist_at_the_end_of_the_stream(result) -> None:
    """Detections near the end of the data cannot have long horizons answered.

    If nothing were PENDING, the tracker would be closing horizons it had no data for.
    """
    pending = [
        horizon
        for evaluation in result.evaluations
        for horizon in evaluation.pending_horizons
    ]
    assert pending


def test_an_unknown_answer_is_never_reported_as_a_miss(result) -> None:
    """The central claim of the phase.

    A threshold's denominator must equal the number of COMPLETE horizons -- never the
    total -- so PENDING and INVALID cannot be silently folded in as failures.
    """
    reports = build_report(result.evaluations, EMA_OUTCOME_V1)
    for horizon in EMA_OUTCOME_V1.horizons:
        report = reports[horizon.id]
        for threshold_report in report.thresholds.values():
            assert threshold_report.evaluated == report.complete
            assert threshold_report.reached <= threshold_report.evaluated
        # And the excluded work is reported rather than dropped.
        assert report.total == len(result.evaluations)


def test_a_threshold_with_no_data_has_no_rate() -> None:
    """None, not 0.0: a rate of zero asserts the threshold was never reached."""
    from aureon.evaluation.backfill import ThresholdReport

    assert ThresholdReport(threshold=3.0).rate is None
    assert ThresholdReport(threshold=3.0, reached=1, evaluated=2).rate == pytest.approx(0.5)


def test_reached_counts_come_only_from_complete_horizons(result) -> None:
    """Recomputed independently here, so a bug in build_report cannot hide itself."""
    reports = build_report(result.evaluations, EMA_OUTCOME_V1)
    expected = sum(
        1
        for evaluation in result.evaluations
        for horizon in evaluation.complete_horizons
        if horizon.horizon_id == HORIZON_5_CANDLES and horizon.reached.get("3")
    )
    assert reports[HORIZON_5_CANDLES].thresholds["3"].reached == expected


def test_the_report_flags_saturated_thresholds(result) -> None:
    """A metric that is ~always true measures nothing.

    At point=0.01 the specified 3-20 point thresholds are far inside a single M5
    candle's range, so they saturate. The report must say so rather than let a wall of
    100% read as a strong result.
    """
    from aureon.evaluation.backfill import diagnose

    reports = build_report(result.evaluations, EMA_OUTCOME_V1)
    warnings = diagnose(reports, EMA_OUTCOME_V1)
    assert any("reached" in w and "measure almost nothing" in w for w in warnings)


def test_the_report_flags_unobservable_path_ordering(result) -> None:
    """Same-candle crossings make MFE_FIRST a convention, not a measurement."""
    from aureon.evaluation.backfill import diagnose

    warnings = diagnose(build_report(result.evaluations, EMA_OUTCOME_V1), EMA_OUTCOME_V1)
    assert any("ambiguous" in w for w in warnings)


def test_the_report_renders(result) -> None:
    text = format_report(build_report(result.evaluations, EMA_OUTCOME_V1), EMA_OUTCOME_V1)
    assert "COMPLETE horizons ONLY" in text
    assert EMA_OUTCOME_V1.rule_id in text
    for horizon in EMA_OUTCOME_V1.horizons:
        assert horizon.id in text


# ── No hindsight ──────────────────────────────────────────────────────────────


def test_an_evaluation_never_measures_before_its_detection(result) -> None:
    """The structural guarantee against hindsight.

    Every excursion timestamp must fall strictly after the detection became known. A
    timestamp at or before it would mean the evaluator had peeked at the bar the
    detection was made on -- or earlier.
    """
    by_id = {d.detection_id: d for d in result.detections}
    for evaluation in result.evaluations:
        detection = by_id[evaluation.detection_id]
        for horizon in evaluation.horizons:
            for stamp in (horizon.mfe_at, horizon.mae_at, horizon.completed_at):
                if stamp is not None:
                    assert stamp > detection.detected_at.utc


def test_time_to_is_never_negative(result) -> None:
    for evaluation in result.evaluations:
        for horizon in evaluation.complete_horizons:
            for seconds in horizon.time_to.values():
                if seconds is not None:
                    assert seconds > 0


def test_a_reached_threshold_always_has_a_time_and_the_reverse(result) -> None:
    for evaluation in result.evaluations:
        for horizon in evaluation.complete_horizons:
            for key, was_reached in horizon.reached.items():
                if was_reached:
                    assert horizon.time_to[key] is not None, key
                else:
                    assert horizon.time_to[key] is None, key


def test_detections_are_not_mutated_by_evaluation(result) -> None:
    """Outcomes live in detection_evaluations; a Detection is immutable."""
    for detection in result.detections:
        assert not any(
            field.startswith(("reached", "outcome", "mfe", "mae"))
            for field in detection.model_dump()
        )


# ── Persistence ───────────────────────────────────────────────────────────────


def test_evaluations_round_trip_through_the_repository(result) -> None:
    from aureon.storage.evaluation_repository import EvaluationRepository

    store = InMemoryFirestore()
    repository = EvaluationRepository(store)
    subset = result.evaluations[:5]
    assert repository.upsert_many(subset) == 5

    for evaluation in subset:
        loaded = repository.get(evaluation.detection_id, evaluation.rule_id)
        assert loaded is not None
        assert loaded.model_dump(mode="json") == evaluation.model_dump(mode="json")


def test_upserting_twice_leaves_one_document(result) -> None:
    from aureon.storage.evaluation_repository import EvaluationRepository

    store = InMemoryFirestore()
    repository = EvaluationRepository(store)
    evaluation = result.evaluations[0]
    repository.upsert(evaluation)
    repository.upsert(evaluation)
    assert len(store.docs) == 1


def test_a_second_rule_gets_its_own_document(result) -> None:
    """So a new rule can be evaluated without touching the first rule's frozen results."""
    from aureon.storage.evaluation_repository import EvaluationRepository

    store = InMemoryFirestore()
    repository = EvaluationRepository(store)
    evaluation = result.evaluations[0]
    repository.upsert(evaluation)
    repository.upsert(evaluation.model_copy(update={"rule_id": "OTHER_RULE_V1"}))
    assert len(store.docs) == 2


# ── 9A: evaluation over either symbol, each under its own rule ────────────────

SYMBOL_CASES = [
    ("XAUUSD", "XAU_OUTCOME_V2", 0.01),
    ("XAGUSD", "XAG_OUTCOME_V1", 0.001),
]


def backfill_for(symbol: str, rule_id: str, point: float, candles: list[Candle]):
    from aureon.config import AureonConfig
    from aureon.evaluation.rules import get_rule
    from main_observer import default_agents

    config = AureonConfig(
        symbols=("XAUUSD", "XAGUSD"),
        evaluation_rules={"XAUUSD": "XAU_OUTCOME_V2", "XAGUSD": "XAG_OUTCOME_V1"},
    )
    return run_backfill(
        candles,
        default_agents(config, symbol=symbol),
        get_rule(rule_id),
        account_scope=ACCOUNT_SCOPE,
        market_tz=MARKET_TZ,
        point=point,
    )


@pytest.mark.parametrize(("symbol", "rule_id", "point"), SYMBOL_CASES)
def test_each_symbol_is_evaluated_under_its_own_rule(
    request: pytest.FixtureRequest, symbol: str, rule_id: str, point: float
) -> None:
    """9A. The evaluator is dimensional, and the rule id is the record of which ruler.

    Not a formality: a price threshold is divided by the symbol's tick at evaluation time
    (decision 138), so the same $-distance is a different number of points on each
    instrument, and the stored ``rule_id`` is what a later reader uses to know which
    thresholds produced the counts.
    """
    candles = request.getfixturevalue(
        "candles" if symbol == "XAUUSD" else "silver_candles"
    )[:900]
    result = backfill_for(symbol, rule_id, point, candles)

    assert result.detections, f"{symbol} produced no detections"
    assert result.evaluations, f"{symbol} produced no evaluations"
    assert {e.rule_id for e in result.evaluations} == {rule_id}
    assert {d.symbol for d in result.detections} == {symbol}
    # Every directional detection is evaluated; context-only ones have no favourable side.
    directional = [d for d in result.detections if d.direction is not None]
    assert len(result.evaluations) == len(directional)


@pytest.mark.parametrize(("symbol", "rule_id", "point"), SYMBOL_CASES)
def test_each_symbols_first_threshold_is_reachable(
    request: pytest.FixtureRequest, symbol: str, rule_id: str, point: float
) -> None:
    """A rule whose smallest threshold nothing ever reaches is measuring the wrong scale.

    Asserted as "some COMPLETE horizon reached rung 1" rather than as a rate: the point is
    that the ladder is dimensionally sane on this instrument, which is exactly what a
    shared rule across two symbols would break (decision 141). The BASELINE document carries
    the rates, including silver's top rungs that nothing reaches.
    """
    from aureon.evaluation.rules import get_rule
    from aureon.models.enums import HorizonStatus as Status
    from aureon.models.evaluation import threshold_key

    candles = request.getfixturevalue(
        "candles" if symbol == "XAUUSD" else "silver_candles"
    )[:900]
    result = backfill_for(symbol, rule_id, point, candles)
    rung_one = threshold_key(get_rule(rule_id).thresholds[0])

    reached = sum(
        1
        for evaluation in result.evaluations
        for horizon in evaluation.horizons
        if horizon.status is Status.COMPLETE and horizon.reached.get(rung_one)
    )
    assert reached > 0, (
        f"nothing reached {rung_one} under {rule_id} on {symbol}: the thresholds are "
        "the wrong scale for this instrument"
    )
