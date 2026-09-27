"""Phase-1 foundation acceptance and bootstrap Champion helpers.

This module does not train or mutate models by itself. It evaluates whether the
historical foundation satisfies the V1 Phase-1 completion contract and produces a
stable, JSON-safe report that can be archived as evidence.

Bootstrap promotion is intentionally narrow: it is only permitted when there is no
existing Champion, the model is already SHADOW, its completed walk-forward meets the
same clean_10 gates used by EvolutionAgent, and its training coverage proves the
requested foundation period. This is for the first historical Champion only; later
generations must earn promotion through reconciled shadow evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aureon.models.learning_v1 import ModelLifecycleStatus
from aureon.services.evolution_agent import EvolutionAgent

PHASE1_REPORT_SCHEMA = "AUREON_PHASE1_FOUNDATION_REPORT_V1"


@dataclass(frozen=True)
class Phase1Acceptance:
    passed: bool
    checks: dict[str, dict[str, Any]]
    report: dict[str, Any]


def _metric_dict(metric: Any | None) -> dict[str, Any]:
    if metric is None:
        return {}
    dump = getattr(metric, "model_dump", None)
    return dump(mode="json") if callable(dump) else dict(metric)


def build_phase1_acceptance(
    *,
    storage: Any,
    symbol: str,
    expected_from: str = "2023-01-01",
    expected_through: str = "2026-01-31",
    min_samples: int = 30,
) -> Phase1Acceptance:
    symbol = symbol.upper()
    champion = storage.models.champion(symbol)
    latest = storage.models.latest_model(symbol)
    model = champion or latest
    examples = list(
        storage.training_memory.canonical_between(
            symbol, expected_from, expected_through
        )
    )

    coverage = None
    if model is not None:
        coverage = (model.validation_metrics or {}).get("training_coverage")
    if coverage is None:
        from aureon.services.training_coverage import coverage_report

        coverage = coverage_report(examples)

    backtest = (
        storage.models.latest_backtest_for_model(model.model_id)
        if model is not None
        else None
    )
    clean = None if backtest is None else backtest.aggregate_metrics.get("clean_10")
    validation_clean = None if model is None else model.target_metrics.get("clean_10")

    checks: dict[str, dict[str, Any]] = {}

    def add(name: str, ok: bool, **detail: Any) -> None:
        checks[name] = {"passed": bool(ok), **detail}

    first = coverage.get("training_from") if coverage else None
    last = coverage.get("training_through") if coverage else None
    total = int((coverage or {}).get("total_samples", 0))

    add(
        "canonical_training_memory",
        bool(examples) and total >= min_samples,
        samples=total,
        min_samples=min_samples,
        training_from=first,
        training_through=last,
    )
    add(
        "foundation_range",
        first is not None
        and last is not None
        and first <= expected_from
        and last >= expected_through,
        expected_from=expected_from,
        expected_through=expected_through,
        observed_from=first,
        observed_through=last,
    )
    add(
        "chronological_validation",
        validation_clean is not None and int(validation_clean.samples or 0) >= 20,
        metrics=_metric_dict(validation_clean),
    )
    add(
        "walk_forward",
        backtest is not None
        and backtest.status == "complete"
        and clean is not None
        and int(clean.samples or 0) >= 20,
        backtest_id=None if backtest is None else backtest.backtest_id,
        status=None if backtest is None else backtest.status,
        folds=0 if backtest is None else len(backtest.folds),
        out_of_sample_predictions=(
            0 if backtest is None else backtest.out_of_sample_predictions
        ),
        clean_10=_metric_dict(clean),
    )
    add(
        "training_coverage",
        bool(coverage)
        and total >= min_samples
        and bool((coverage or {}).get("buckets")),
        dimensions=list((coverage or {}).get("dimensions", [])),
        bucket_count=len((coverage or {}).get("buckets", [])),
        context_count=len((coverage or {}).get("context_counts", {})),
    )
    add(
        "champion_frozen",
        champion is not None
        and champion.status == ModelLifecycleStatus.CHAMPION.value
        and champion.trained_through <= expected_through,
        champion_model_id=None if champion is None else champion.model_id,
        trained_through=None if champion is None else champion.trained_through,
        generation=None if champion is None else champion.generation,
    )

    passed = all(item["passed"] for item in checks.values())
    report = {
        "schema": PHASE1_REPORT_SCHEMA,
        "symbol": symbol,
        "expected_training_period": {
            "from": expected_from,
            "through": expected_through,
        },
        "passed": passed,
        "checks": checks,
        "selected_model_id": None if model is None else model.model_id,
        "selected_model_status": None if model is None else model.status,
        "champion_model_id": None if champion is None else champion.model_id,
        "training_samples": total,
        "clean_10_validation": _metric_dict(validation_clean),
        "walk_forward_clean_10": _metric_dict(clean),
        "coverage": coverage,
    }
    return Phase1Acceptance(passed=passed, checks=checks, report=report)


def bootstrap_champion_from_historical_shadow(
    *,
    storage: Any,
    model_id: str,
    expected_from: str = "2023-01-01",
    expected_through: str = "2026-01-31",
) -> Any:
    """Promote the first Champion only after historical evidence clears V1 gates."""
    model = storage.models.get_model(model_id)
    if model is None:
        raise LookupError(f"no model {model_id}")
    if storage.models.champion(model.symbol) is not None:
        raise ValueError(
            "bootstrap promotion is only allowed when the symbol has no Champion"
        )
    if model.status != ModelLifecycleStatus.SHADOW.value:
        raise ValueError(
            f"bootstrap promotion requires a shadow model; {model_id} is {model.status}"
        )
    if model.trained_from > expected_from or model.trained_through < expected_through:
        raise ValueError(
            f"shadow training period {model.trained_from}..{model.trained_through} "
            f"does not cover required foundation {expected_from}..{expected_through}"
        )

    backtest = storage.models.latest_backtest_for_model(model.model_id)
    if backtest is None or backtest.status != "complete":
        raise ValueError("bootstrap promotion requires a completed walk-forward backtest")
    clean = backtest.aggregate_metrics.get("clean_10")
    if clean is None:
        raise ValueError("walk-forward backtest has no clean_10 metrics")

    evolution = EvolutionAgent(storage.models)
    failures = evolution._validation_failures(clean)
    if failures:
        raise ValueError("walk-forward rejection: " + "; ".join(failures))

    coverage = (model.validation_metrics or {}).get("training_coverage")
    if not coverage:
        raise ValueError("shadow carries no training_coverage evidence")
    if (coverage.get("training_from") or "9999-12-31") > expected_from:
        raise ValueError("training coverage does not start early enough")
    if (coverage.get("training_through") or "0001-01-01") < expected_through:
        raise ValueError("training coverage does not reach the January cutoff")

    promoted = storage.models.promote_champion(
        model.model_id,
        at=evolution._now(),
        reason=(
            "bootstrap Champion: historical foundation + chronological validation + "
            "walk-forward evidence passed Phase-1 gates"
        ),
    )
    evolution._record(
        promoted,
        action="bootstrap_promoted_champion",
        reason=promoted.promotion_reason or "historical bootstrap passed",
        metrics=evolution._clean_metrics(clean),
    )
    return promoted
