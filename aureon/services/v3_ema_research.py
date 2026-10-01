"""Aureon V3 EMA validation and research reports.

All reports operate on frozen predictions and resolved outcomes. They never alter
historical examples and never infer unavailable candle paths.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from aureon.ml.logistic import binary_metrics
from aureon.models.base import to_utc, utc_now
from aureon.models.ema_journey_v3 import EMAMovementJourney
from aureon.services.v3_ema_learning import (
    CanonicalEMAExampleV3,
    EMAHoldoutDayV3,
    calibration_buckets,
)
from aureon.services.v3_ema_ops import rolling_unseen_summary

TARGETS = (
    ("3", "probability_reach_3", "reached_3"),
    ("5", "probability_reach_5", "reached_5"),
    ("10", "probability_reach_10", "reached_10"),
    ("20", "probability_reach_20", "reached_20"),
    ("30", "probability_reach_30", "reached_30"),
    ("40", "probability_reach_40", "reached_40"),
    ("clean_10", "probability_clean_10", "clean_10"),
)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def score_predictions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare frozen probabilities with actual resolved outcomes."""
    target_metrics: dict[str, Any] = {}
    false_positives = 0
    false_negatives = 0
    signed_errors: list[float] = []

    for label, probability_key, outcome_key in TARGETS:
        probabilities: list[float] = []
        labels: list[int] = []
        for row in rows:
            prediction = dict(row.get("payload") or {})
            actual = dict(row.get("actual_outcome") or {})
            probability = prediction.get(probability_key)
            outcome = actual.get(outcome_key)
            if probability is None or outcome is None:
                continue
            p = float(probability)
            y = 1 if bool(outcome) else 0
            probabilities.append(p)
            labels.append(y)
            signed_errors.append(p - y)
            if label == "clean_10":
                if p >= 0.5 and not y:
                    false_positives += 1
                elif p < 0.5 and y:
                    false_negatives += 1

        metrics = binary_metrics(labels, probabilities)
        target_metrics[label] = {
            **metrics,
            "calibration": calibration_buckets(
                probabilities,
                [bool(value) for value in labels],
            ),
        }

    mfe_errors: list[float] = []
    mae_errors: list[float] = []
    for row in rows:
        prediction = dict(row.get("payload") or {})
        actual = dict(row.get("actual_outcome") or {})
        if prediction.get("expected_mfe") is not None and actual.get("mfe") is not None:
            mfe_errors.append(float(prediction["expected_mfe"]) - float(actual["mfe"]))
        if prediction.get("expected_mae") is not None and actual.get("mae") is not None:
            mae_errors.append(float(prediction["expected_mae"]) - float(actual["mae"]))

    return {
        "samples": len(rows),
        "targets": target_metrics,
        "false_positives_clean_10": false_positives,
        "false_negatives_clean_10": false_negatives,
        "mean_probability_error": _mean(signed_errors),
        "mean_mfe_error": _mean(mfe_errors),
        "mean_mae_error": _mean(mae_errors),
    }


class V3EMAValidationService:
    """Score and release next-day holdouts."""

    def __init__(self, learning: Any, *, now: Any = utc_now) -> None:
        self.learning = learning
        self._now = now

    def score_holdout(self, symbol: str, market_date: str) -> EMAHoldoutDayV3:
        holdout = self.learning.holdout_for(symbol, market_date)
        if holdout is None:
            raise LookupError(f"no V3 holdout for {symbol} {market_date}")
        if holdout.status == "released":
            return holdout
        rows = self.learning.predictions_for_market_date(
            symbol,
            market_date,
            model_id=holdout.frozen_model_id,
            reconciled_only=True,
        )
        if not rows:
            raise ValueError("holdout has no reconciled predictions to score")
        metrics = score_predictions(rows)
        scored = holdout.model_copy(
            update={
                "status": "scored",
                "scored_at": to_utc(self._now()),
                "metrics": metrics,
            }
        )
        return self.learning.update_holdout(scored)

    def rolling_unseen(
        self,
        symbol: str,
        *,
        window_days: int = 15,
    ) -> dict[str, Any]:
        """Aggregate scored/released unseen days over a 10-20 day window."""
        return rolling_unseen_summary(
            self.learning.holdouts_for(symbol),
            window_days=window_days,
            min_days=10,
            max_days=20,
        )

    def release_holdout(self, symbol: str, market_date: str) -> EMAHoldoutDayV3:
        holdout = self.learning.holdout_for(symbol, market_date)
        if holdout is None:
            raise LookupError(f"no V3 holdout for {symbol} {market_date}")
        if holdout.status == "released":
            return holdout
        if holdout.status != "scored" or holdout.scored_at is None:
            raise ValueError("holdout must be scored before release")
        released = holdout.model_copy(update={"status": "released"})
        return self.learning.update_holdout(released)


def _example_summary(
    examples: list[CanonicalEMAExampleV3],
    *,
    min_cell_samples: int = 20,
) -> dict[str, Any]:
    if not examples:
        return {"samples": 0, "insufficient": True}
    if len(examples) < min_cell_samples:
        return {
            "samples": len(examples),
            "journeys": len({example.journey_id for example in examples}),
            "insufficient": True,
            "minimum_required": min_cell_samples,
        }
    return {
        "insufficient": False,
        "samples": len(examples),
        "journeys": len({example.journey_id for example in examples}),
        "reach_3_rate": sum(example.outcome.reached_3 for example in examples) / len(examples),
        "reach_5_rate": sum(example.outcome.reached_5 for example in examples) / len(examples),
        "reach_10_rate": sum(example.outcome.reached_10 for example in examples) / len(examples),
        "reach_20_rate": sum(example.outcome.reached_20 for example in examples) / len(examples),
        "average_mfe": _mean([example.outcome.mfe for example in examples]),
        "average_mae": _mean([example.outcome.mae for example in examples]),
        "average_speed": _mean(
            [
                example.outcome.dollars_per_bar
                for example in examples
                if example.outcome.dollars_per_bar is not None
            ]
        ),
    }


def continuation_report(
    examples: list[CanonicalEMAExampleV3],
    *,
    min_cell_samples: int = 20,
) -> dict[str, Any]:
    """Compare anchor families without publishing unstable small-cell hit rates."""
    anchor_groups: dict[str, list[CanonicalEMAExampleV3]] = defaultdict(list)
    trend_groups: dict[str, list[CanonicalEMAExampleV3]] = defaultdict(list)
    direction_groups: dict[str, list[CanonicalEMAExampleV3]] = defaultdict(list)
    session_groups: dict[str, list[CanonicalEMAExampleV3]] = defaultdict(list)
    confidence_groups: dict[str, list[CanonicalEMAExampleV3]] = defaultdict(list)
    regime_groups: dict[str, list[CanonicalEMAExampleV3]] = defaultdict(list)

    for example in examples:
        anchor_groups[example.anchor_type].append(example)
        trend_groups[example.features.trend_direction].append(example)
        direction_groups[example.direction.value].append(example)
        session_groups[
            f"{example.features.session}:{example.features.session_phase}"
        ].append(example)
        confidence_groups[example.features.agent_confidence.label].append(example)
        regime_groups[example.features.volatility_regime].append(example)

    return {
        "by_anchor": {
            key: _example_summary(rows, min_cell_samples=min_cell_samples)
            for key, rows in anchor_groups.items()
        },
        "by_trend": {
            key: _example_summary(rows, min_cell_samples=min_cell_samples)
            for key, rows in trend_groups.items()
        },
        "by_direction": {
            key: _example_summary(rows, min_cell_samples=min_cell_samples)
            for key, rows in direction_groups.items()
        },
        "by_session": {
            key: _example_summary(rows, min_cell_samples=min_cell_samples)
            for key, rows in session_groups.items()
        },
        "by_agent_confidence": {
            key: _example_summary(rows, min_cell_samples=min_cell_samples)
            for key, rows in confidence_groups.items()
        },
        "by_regime": {
            key: _example_summary(rows, min_cell_samples=min_cell_samples)
            for key, rows in regime_groups.items()
        },
        "minimum_cell_samples": min_cell_samples,
    }


def placement_report(journeys: list[EMAMovementJourney]) -> dict[str, Any]:
    """Compare only placements that actually became observable in each journey."""
    rows: list[dict[str, Any]] = []
    for journey in journeys:
        if journey.status.value == "invalid":
            continue
        for anchor in journey.anchors:
            if not anchor.outcome.completed or not anchor.outcome.valid:
                continue
            rows.append(
                {
                    "journey_id": journey.journey_id,
                    "anchor": anchor.anchor_type.value,
                    "direction": anchor.direction.value,
                    "reference_price": anchor.reference_price,
                    "reference_price_kind": anchor.reference_price_kind,
                    "move_consumed": anchor.movement_from_journey_start,
                    "mfe": anchor.outcome.mfe,
                    "mae": anchor.outcome.mae,
                    "reach_3": bool(
                        anchor.outcome.targets.get("3")
                        and anchor.outcome.targets["3"].reached
                    ),
                    "reach_5": bool(
                        anchor.outcome.targets.get("5")
                        and anchor.outcome.targets["5"].reached
                    ),
                    "reach_10": bool(
                        anchor.outcome.targets.get("10")
                        and anchor.outcome.targets["10"].reached
                    ),
                    "pullback_before_5": (
                        anchor.outcome.targets["5"].adverse_before_reach
                        if anchor.outcome.targets.get("5")
                        else None
                    ),
                }
            )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["anchor"]].append(row)

    return {
        "placements": {
            anchor: {
                "samples": len(group),
                "journeys": len({row["journey_id"] for row in group}),
                "average_mfe": _mean([float(row["mfe"]) for row in group]),
                "average_mae": _mean([float(row["mae"]) for row in group]),
                "reach_3_rate": sum(bool(row["reach_3"]) for row in group) / len(group),
                "reach_5_rate": sum(bool(row["reach_5"]) for row in group) / len(group),
                "reach_10_rate": sum(bool(row["reach_10"]) for row in group) / len(group),
                "average_move_consumed": _mean(
                    [float(row["move_consumed"]) for row in group]
                ),
                "average_pullback_before_5": _mean(
                    [
                        float(row["pullback_before_5"])
                        for row in group
                        if row["pullback_before_5"] is not None
                    ]
                ),
            }
            for anchor, group in grouped.items()
        },
        "note": (
            "Only observed anchors are compared. No hypothetical fill is invented at a "
            "price the market did not actually make available."
        ),
    }
