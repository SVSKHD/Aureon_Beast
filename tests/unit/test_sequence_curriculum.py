"""Phase-3 curriculum acceptance tests."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aureon.services.sequence_curriculum import (
    FEATURE_SCHEMA, FORBIDDEN_FEATURE_KEYS, acceptance_report, build_examples, chronological_split,
)


def _candidate(sid, minute, direction="BUY", reached=True):
    return SimpleNamespace(
        candidate_id=f"{sid}-{minute}", timestamp=datetime(2026, 1, 1, tzinfo=UTC)+timedelta(minutes=minute),
        entry_price=100.0, direction=SimpleNamespace(value=direction),
        evidence_agents=["wick"], evidence={"detections": [{"agent": "wick", "direction": direction}]},
        context={"rsi": 50, "future": {"bad": True}},
    )


def _ladder(reached=True):
    values = {}
    for target in (6, 10, 20, 30, 40):
        hit = reached and target <= 10
        values[f"reached_{target}"] = hit
        values[f"bars_to_{target}"] = target if hit else None
    return SimpleNamespace(**values)


def _record(index, outcome="PULLBACK_THEN_CONTINUATION"):
    at = datetime(2026, 1, 1, tzinfo=UTC)+timedelta(hours=index)
    direction = "BUY" if index % 2 == 0 else "SELL"
    opposite = "SELL" if direction == "BUY" else "BUY"
    snapshot = SimpleNamespace(
        sequence_id=f"seq-{index:03d}", timestamp=at, direction=SimpleNamespace(value=direction),
        ema_fast=101.0, ema_slow=100.0, ema_fast_slope=.2, ema_slow_slope=.1,
        ema_separation=1.0, ema_separation_change=.1, rsi=55.0, rsi_change=1.0, atr=3.0,
        htf_alignment="aligned", daily_market_bias="bullish", market_regime={"state": "trend"},
        volatility_regime="normal", session="london", wick_state=None, liquidity_state=None,
        breakout_state=None, market_structure=None, participation=None,
        agent_direction=direction.lower(), agent_confidence=.7,
        agent_states={"wick": "ready"}, context={"session": "london", "future": {"bad": 1}},
    )
    counter = _candidate(snapshot.sequence_id, index*60+5, opposite, True)
    continuation = _candidate(snapshot.sequence_id, index*60+15, direction, True)
    candidate_outcome = SimpleNamespace(mfe=12.0, mae=3.0, targets=_ladder(True))
    label = SimpleNamespace(
        outcome=SimpleNamespace(value=outcome), continuation=_ladder(True),
        max_favourable_move=14.0, max_adverse_move=4.0,
        counter_move_candidate=counter, counter_move_outcome=candidate_outcome,
        exhaustion_candidate=continuation, continuation_candidate=continuation,
        continuation_candidate_outcome=candidate_outcome,
    )
    return SimpleNamespace(snapshot=snapshot, label=label)


def test_future_fields_never_enter_features() -> None:
    rows = build_examples([_record(0)])
    for row in rows:
        serialized = str(row["features"])
        for forbidden in FORBIDDEN_FEATURE_KEYS:
            assert f"'{forbidden}':" not in serialized


def test_sequence_level_split_has_no_overlap_and_is_chronological() -> None:
    records = [_record(i) for i in range(20)]
    split = chronological_split(records)
    assert not set(split.train) & set(split.validation)
    assert not set(split.train) & set(split.test)
    assert not set(split.validation) & set(split.test)
    assert split.train[0] == "seq-000"
    assert split.test[-1] == "seq-019"


def test_curriculum_contains_all_decision_classes_and_both_directions() -> None:
    records = [
        _record(0, "IMMEDIATE_CONTINUATION"),
        _record(1, "FAILED_DIRECTION"),
        _record(2, "PULLBACK_THEN_CONTINUATION"),
        _record(3, "PULLBACK_THEN_CONTINUATION"),
    ]
    rows = build_examples(records)
    report = acceptance_report(records, rows, chronological_split(records))
    assert {"ENTER", "WAIT", "INVALIDATE"} <= set(report["decision_counts"])
    assert {"BUY", "SELL"} <= set(report["direction_counts"])
    assert report["anti_leakage"]["forbidden_feature_occurrences"] == {}
    assert report["acceptance_gate"]["status"] == "PHASE_3_CURRICULUM_PASS"


def test_failed_candidate_is_preserved_as_negative() -> None:
    record = _record(0)
    record.label.counter_move_outcome.targets.reached_6 = False
    rows = build_examples([record])
    counter = next(row for row in rows if row["stage"] == "COUNTER_MOVE")
    assert counter["labels"]["decision"] == "INVALIDATE"
    assert counter["labels"]["setup_type"] is None



def test_feature_schema_is_stable_across_stages() -> None:
    rows = build_examples([_record(0)])
    assert rows
    assert all(set(row["features"]) == set(FEATURE_SCHEMA) for row in rows)


def test_acceptance_gate_rejects_inconsistent_target_ladder() -> None:
    records = [_record(i) for i in range(4)]
    rows = build_examples(records)
    rows[0]["labels"]["reached_10"] = True
    rows[0]["labels"]["reached_6"] = False
    report = acceptance_report(records, rows, chronological_split(records))
    assert report["acceptance_gate"]["status"] == "STOP"
    assert "labels_not_internally_consistent" in report["acceptance_gate"]["failures"]


def test_lowercase_source_directions_are_normalized_for_acceptance() -> None:
    records = [
        _record(0, "IMMEDIATE_CONTINUATION"),
        _record(1, "FAILED_DIRECTION"),
        _record(2, "PULLBACK_THEN_CONTINUATION"),
        _record(3, "PULLBACK_THEN_CONTINUATION"),
    ]
    for record in records:
        record.snapshot.direction.value = record.snapshot.direction.value.lower()
        record.label.counter_move_candidate.direction.value = record.label.counter_move_candidate.direction.value.lower()
        record.label.exhaustion_candidate.direction.value = record.label.exhaustion_candidate.direction.value.lower()
        record.label.continuation_candidate.direction.value = record.label.continuation_candidate.direction.value.lower()

    rows = build_examples(records)
    report = acceptance_report(records, rows, chronological_split(records))

    assert set(report["direction_counts"]) == {"BUY", "SELL"}
    assert report["anti_leakage"]["label_consistency_failures"] == []
    assert report["acceptance_gate"]["status"] == "PHASE_3_CURRICULUM_PASS"
