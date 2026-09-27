"""Phase-2 evidence aggregation tests."""
from __future__ import annotations

from datetime import UTC, datetime

from aureon.models.enums import Direction, Timeframe
from aureon.models.sequence_v1 import (
    CandidateOutcome,
    EMASequenceLabel,
    EMASequenceOutcome,
    EMASequenceRecord,
    EMASequenceSnapshot,
    ObservableCandidateSnapshot,
    TargetLadder,
)
from aureon.services.sequence_hypothesis import EvidenceGate, build_phase2_report, distribution


def _record(direction=Direction.BUY, outcome=EMASequenceOutcome.IMMEDIATE_CONTINUATION):
    now = datetime(2025, 1, 1, tzinfo=UTC)
    snapshot = EMASequenceSnapshot(
        sequence_id=f"{direction.value}-{outcome.value}",
        symbol="XAUUSD", timeframe=Timeframe.M5, timestamp=now,
        direction=direction, cross_price=2000, ema_fast=2001, ema_slow=2000,
        session="london", market_regime="trend", volatility_regime="normal",
        htf_alignment="aligned", daily_market_bias=direction.value,
    )
    counter_direction = Direction.SELL if direction is Direction.BUY else Direction.BUY
    counter = ObservableCandidateSnapshot(
        candidate_id="c", timestamp=now, entry_price=1998,
        direction=counter_direction, candidate_type="counter_move",
        evidence_agents=["liquidity"],
    )
    continuation = ObservableCandidateSnapshot(
        candidate_id="r", timestamp=now, entry_price=1999,
        direction=direction, candidate_type="continuation",
        evidence_agents=["breakout"],
    )
    ladder = TargetLadder(reached_6=True, reached_10=True, bars_to_6=2, bars_to_10=4)
    label = EMASequenceLabel(
        outcome=outcome, horizon_bars=144, pullback_move=4, pullback_bars=3,
        continuation_move=12, max_favourable_move=12, max_adverse_move=4,
        continuation=ladder, counter_move=ladder,
        counter_move_candidate=counter,
        counter_move_outcome=CandidateOutcome(mfe=11, mae=2, targets=ladder, bars_observed=20),
        exhaustion_candidate=continuation,
        continuation_candidate=continuation,
        continuation_candidate_outcome=CandidateOutcome(
            mfe=14, mae=1, targets=ladder, bars_observed=18
        ),
    )
    return EMASequenceRecord(snapshot=snapshot, label=label)


def test_distribution_reports_requested_percentiles():
    result = distribution([1, 2, 3, 4, 10])
    assert result["median"] == 3
    assert result["p25"] == 2
    assert result["p50"] == 3
    assert result["p75"] == 4
    assert result["p90"] > 4


def test_report_covers_directions_targets_evidence_and_breakdowns():
    records = [
        _record(Direction.BUY, EMASequenceOutcome.PULLBACK_THEN_CONTINUATION),
        _record(Direction.SELL, EMASequenceOutcome.DEEP_PULLBACK_THEN_CONTINUATION),
    ]
    report = build_phase2_report(
        records,
        source={"symbol": "XAUUSD", "from": "2023-01-01", "to": "2026-01-31"},
        quality={"chronological": True, "duplicate_open_times": 0, "missing_required_horizon": 0, "full_research_period_covered": True, "source_fingerprint": "abc"},
        gate=EvidenceGate(min_sequences=2, max_ambiguous_fraction=0.5),
    )
    assert report["direction_counts"] == {"buy": 1, "sell": 1}
    assert report["counter_move"]["all"]["targets"]["6"]["hit_rate"] == 1
    assert report["continuation_reentry"]["all"]["targets"]["10"]["hit_rate"] == 1
    assert report["counter_move"]["agent_evidence"]["liquidity"]["reached_6_rate"] == 1
    assert "london" in report["breakdowns"]["session"]
    assert report["evidence_gate"]["status"] == "DATA_QUALITY_PASS"
    assert report["evidence_gate"]["phase3_approved"] is False
    assert report["ml_training_allowed"] is False


def test_gate_stops_on_bad_quality_or_one_sided_sample():
    report = build_phase2_report(
        [_record(Direction.BUY)],
        source={},
        quality={"chronological": False, "duplicate_open_times": 1, "missing_required_horizon": 2, "full_research_period_covered": False, "source_fingerprint": ""},
        gate=EvidenceGate(min_sequences=1),
    )
    assert report["evidence_gate"]["status"] == "STOP"
    assert "both_directions_not_covered" in report["evidence_gate"]["failures"]
    assert "candles_not_strictly_chronological" in report["evidence_gate"]["failures"]
    assert report["evidence_gate"]["phase3_approved"] is False


def test_ambiguous_fraction_can_close_gate():
    records = [
        _record(Direction.BUY, EMASequenceOutcome.AMBIGUOUS_PATH),
        _record(Direction.SELL, EMASequenceOutcome.IMMEDIATE_CONTINUATION),
    ]
    report = build_phase2_report(
        records, source={},
        quality={"chronological": True, "duplicate_open_times": 0, "missing_required_horizon": 0, "full_research_period_covered": True, "source_fingerprint": "abc"},
        gate=EvidenceGate(min_sequences=2, max_ambiguous_fraction=0.25),
    )
    assert report["evidence_gate"]["status"] == "STOP"
    assert "ambiguous_fraction_above_gate" in report["evidence_gate"]["failures"]
