"""GAP 8: month-to-month generation comparison is paired, bucketed and non-predictive."""

from __future__ import annotations

from aureon.services.evolution_agent import EvolutionAgent
from aureon.storage.postgres.repositories.models import ModelRepository
from tests.unit.v1_fixtures import constant_model, examples


def test_generation_report_compares_both_models_on_the_same_examples(local_store) -> None:
    models = ModelRepository(local_store)
    models.write_model(constant_model("august", clean=0.3, trained_through="2026-01-20"))
    models.write_model(
        constant_model(
            "september", status="shadow", clean=0.8, trained_through="2026-02-03", generation=1
        )
    )
    rows = examples(60, days=30, per_day=2, session="london") + examples(
        40, days=20, per_day=2, session="asia", clean_every=4
    )
    report = EvolutionAgent(models).generation_report(
        challenger_model_id="september",
        previous_model_id="august",
        examples=rows,
        min_bucket_samples=10,
    )
    assert report.previous_model_id == "august" and report.challenger_model_id == "september"
    assert report.previous_training_period == ("2026-01-01", "2026-01-20")
    assert report.samples_before == 50
    assert report.samples_added == sum(1 for r in rows if r.market_date > "2026-01-20")
    assert report.comparison_samples == report.samples_added
    for target in ("clean_10", "reach_5", "reach_10", "reach_20", "reach_30", "reach_40"):
        assert target in report.challenger_metrics and target in report.previous_metrics
    clean = report.challenger_metrics["clean_10"]
    assert {
        "precision",
        "recall",
        "false_positive_rate",
        "brier",
        "log_loss",
        "average_mae",
        "average_mfe",
    } <= set(clean)
    # Same examples, so bucket sample counts are identical for both generations.
    for key, bucket in report.challenger_buckets.items():
        assert report.previous_buckets[key]["samples"] == bucket["samples"]
    assert any(key.startswith("session:") for key in report.challenger_buckets)
    assert any(key.startswith("daily_bias:") for key in report.challenger_buckets)
    # A constant 0.8 says ENTER everywhere: relative to a constant 0.3 that abstains, the
    # false-positive rate rises in every bucket with negatives -- a new failure pattern.
    assert report.new_failure_patterns
    assert "not a prediction" in report.disclaimer
    decisions = models.evolution_for_symbol("XAUUSD")
    assert decisions and decisions[0].action == "generation_report"
    assert decisions[0].metrics["challenger_model_id"] == "september"


def test_generation_report_without_a_previous_model_still_describes_the_challenger(
    local_store,
) -> None:
    models = ModelRepository(local_store)
    models.write_model(constant_model("first", status="candidate", clean=0.6))
    report = EvolutionAgent(models).generation_report(
        challenger_model_id="first", examples=examples(30), record=False
    )
    assert report.previous_model_id is None
    assert report.previous_metrics == {} and report.challenger_metrics
    assert report.regimes_improved == () and report.regimes_degraded == ()
