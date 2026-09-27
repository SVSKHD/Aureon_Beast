"""GAP 4/5/9: the Champion's decision contract, one-position HOLD, and honest experiences."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from aureon.models.enums import DirectionContext, Timeframe
from aureon.models.learning_v1 import CoverageStatus, EntryDecision
from aureon.services.prediction_service import PredictionService
from aureon.services.training_coverage import coverage_report
from aureon.storage.postgres.repositories.models import ModelRepository, classify_outcome
from tests.unit.v1_fixtures import constant_model, examples


def _setup_event(setup_id="setup-x", session="london"):
    at = datetime(2026, 2, 2, 12, 0, tzinfo=UTC)
    setup = SimpleNamespace(
        setup_id=setup_id,
        symbol="XAUUSD",
        timeframe=Timeframe.M5,
        direction_context=DirectionContext.BULLISH,
        anchor=SimpleNamespace(price=2400.0),
        context_summary=SimpleNamespace(
            volatility_regime="normal", session=None, mtf_alignment=SimpleNamespace(value="aligned")
        ),
    )
    event = SimpleNamespace(
        event_id=f"event-{setup_id}",
        market_time=SimpleNamespace(utc=at),
        context_snapshot={
            "reference_price": "2400.0",
            "rsi": "56",
            "atr": "8",
            "session": session,
            "daily_bias_at_entry": "bullish",
            "daily_bias_strength_at_entry": 0.6,
            "agent_states": {"wick": {"alignment": "aligned"}, "rsi": {"alignment": "opposed"}},
        },
    )
    return setup, event


def test_decision_contract_enter_wait_reject(local_store) -> None:
    models = ModelRepository(local_store)
    for clean, expected in (
        (0.72, EntryDecision.ENTER),
        (0.50, EntryDecision.WAIT),
        (0.20, EntryDecision.REJECT),
    ):
        models.write_model(constant_model(f"m-{clean}", clean=clean))
        setup, event = _setup_event(setup_id=f"s-{clean}")
        result = PredictionService(models).predict_champion(setup, event)
        assert result.decision is expected, clean
        assert result.probability_clean_10 == clean
        assert result.probability_reach_20 == 0.5 and result.recommended_target == 20
        assert result.supporting_agents == ("wick",) and result.opposing_agents == ("rsi",)
        assert result.daily_bias == "bullish" and result.session == "london"
        assert result.generation == 0
        stored = models.prediction_for(f"m-{clean}", f"s-{clean}")
        assert stored.decision == expected.value
        models.set_status(f"m-{clean}", "retired")


def test_hold_existing_position_still_analyses_and_flags_addon_for_research(local_store) -> None:
    models = ModelRepository(local_store)
    models.write_model(constant_model("champ", clean=0.8))
    holding = {"value": True}
    service = PredictionService(models, position_provider=lambda symbol: holding["value"])
    setup, event = _setup_event(setup_id="s-hold")
    result = service.predict_champion(setup, event)
    assert result.decision is EntryDecision.HOLD_EXISTING_POSITION
    assert result.addon_opportunity is True and result.would_enter is True
    assert models.prediction_for("champ", "s-hold").decision == "HOLD_EXISTING_POSITION"
    holding["value"] = False
    setup2, event2 = _setup_event(setup_id="s-free")
    assert service.predict_champion(setup2, event2).decision is EntryDecision.ENTER


def test_out_of_distribution_context_downgrades_enter_to_wait(local_store) -> None:
    models = ModelRepository(local_store)
    report = coverage_report(examples(120, session="london"), low_threshold=50, ood_threshold=10)
    models.write_model(
        constant_model("champ", clean=0.8, validation_metrics={"training_coverage": report})
    )
    service = PredictionService(models)
    setup, event = _setup_event(setup_id="s-ood", session="new_york")
    result = service.predict_champion(setup, event)
    assert result.training_coverage.status is CoverageStatus.OUT_OF_DISTRIBUTION
    assert result.decision is EntryDecision.WAIT
    setup2, event2 = _setup_event(setup_id="s-in", session="london")
    normal = service.predict_champion(setup2, event2)
    assert normal.training_coverage.status is CoverageStatus.NORMAL_COVERAGE
    assert normal.decision is EntryDecision.ENTER


def test_experiences_are_scored_after_the_fact_and_failures_kept(local_store) -> None:
    models = ModelRepository(local_store)
    models.write_model(constant_model("champ", clean=0.8))
    service = PredictionService(models)
    setup, event = _setup_event(setup_id="s-fp")
    service.predict_champion(setup, event)
    at = datetime(2026, 2, 3, tzinfo=UTC)
    models.reconcile_prediction("champ", "s-fp", outcomes={"clean_10": False}, at=at)
    stored = models.prediction_for("champ", "s-fp")
    assert stored.decision == "ENTER" and stored.outcome_class == "false_positive"
    assert stored.probabilities["clean_10"] == 0.8  # the prediction is never rewritten
    # A second outcome cannot rewrite the score.
    models.reconcile_prediction("champ", "s-fp", outcomes={"clean_10": True}, at=at)
    assert models.prediction_for("champ", "s-fp").outcome_class == "false_positive"
    assert models.experience_summary("champ")["false_positive"] == 1
    assert classify_outcome("REJECT", True) == "false_negative"
    assert classify_outcome("WAIT", False) == "true_negative"
    assert classify_outcome("ENTER", None) is None
