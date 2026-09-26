"""Adaptive multi-target historical learning for Aureon.

The five user-facing outputs are probabilities of reaching +5/+10/+20/+30/+40
price moves from an Agent 20 setup.  Features are frozen at setup time.  ATR
normalisation helps the same model reason across quiet and volatile gold regimes.

This module is historical/shadow research only.  It grants no execution authority.
"""

from __future__ import annotations

from bisect import bisect_left
from statistics import median
from typing import Any

from aureon.ml.boosted_stumps import BoostedStumpModel, fit_boosted_stumps
from aureon.ml.logistic import LogisticModel, binary_metrics, fit_logistic
from aureon.models.enums import Direction
from aureon.services.decision_backtest import DecisionReplayRow

TARGETS: tuple[float, ...] = (5.0, 10.0, 20.0, 30.0, 40.0)
QUALITY_STOPS: tuple[float, ...] = (5.0, 8.0, 10.0, 15.0)

FEATURE_NAMES: tuple[str, ...] = (
    "direction_sign",
    "rsi",
    "ema_gap_atr",
    "ema_gap_change_atr",
    "target10_atr_ratio",
    "atr14",
    "director_supporting",
    "director_opposing",
    "htf_bullish",
    "htf_bearish",
    "director_ready",
    "regime_expanding",
    "regime_ranging",
    "regime_compression",
    "regime_chop_or_messy",
    "participation_expanding",
    "participation_contracting",
    "has_liquidity",
    "has_wick",
    "has_breakout",
)


def _atr14(candles: list[Any], index: int) -> float:
    start = max(1, index - 14)
    ranges: list[float] = []
    for i in range(start, index):
        bar = candles[i]
        previous = candles[i - 1]
        ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous.close),
                abs(bar.low - previous.close),
            )
        )
    if not ranges:
        return 1.0
    return max(sum(ranges) / len(ranges), 1e-9)


def _feature_vector(row: DecisionReplayRow, atr14: float) -> list[float]:
    regime = (row.regime or "").lower()
    participation = (row.participation or "").lower()
    agents = set(row.same_candle_agents)
    return [
        1.0 if row.direction == "buy" else -1.0,
        float(row.rsi if row.rsi is not None else 50.0),
        float(row.ema_gap or 0.0) / atr14,
        float(row.ema_gap_change or 0.0) / atr14,
        10.0 / atr14,
        atr14,
        float(row.director_supporting),
        float(row.director_opposing),
        1.0 if row.htf_state == "bullish" else 0.0,
        1.0 if row.htf_state == "bearish" else 0.0,
        1.0 if row.director_state == "ready" else 0.0,
        1.0 if regime == "expanding" else 0.0,
        1.0 if regime == "ranging" else 0.0,
        1.0 if "compression" in regime else 0.0,
        1.0 if ("chop" in regime or "messy" in regime) else 0.0,
        1.0 if participation == "expanding" else 0.0,
        1.0 if participation == "contracting" else 0.0,
        1.0 if "liquidity" in agents else 0.0,
        1.0 if "wick" in agents else 0.0,
        1.0 if "breakout" in agents else 0.0,
    ]


def _favourable_adverse(bar: Any, entry: float, direction: Direction) -> tuple[float, float]:
    if direction is Direction.BUY:
        return max(0.0, bar.high - entry), max(0.0, entry - bar.low)
    return max(0.0, entry - bar.low), max(0.0, bar.high - entry)


def build_adaptive_examples(
    rows: list[DecisionReplayRow],
    candles: list[Any],
    *,
    horizon_bars: int = 864,
) -> list[dict[str, Any]]:
    """Create frozen setup features plus multi-target and drawdown labels."""
    if horizon_bars < 12:
        raise ValueError("horizon_bars must be >= 12")

    eligible = [row for row in rows if row.eligible]
    opens = [candle.open_time.utc for candle in candles]
    examples: list[dict[str, Any]] = []

    for row in eligible:
        moment = __import__("datetime").datetime.fromisoformat(row.at)
        start = bisect_left(opens, moment)
        if start >= len(candles):
            continue
        future = candles[start : min(len(candles), start + horizon_bars)]
        if not future:
            continue

        direction = Direction.BUY if row.direction == "buy" else Direction.SELL
        atr14 = _atr14(candles, start)
        target_hits = {str(int(target)): False for target in TARGETS}
        bars_to_target: dict[str, int | None] = {str(int(target)): None for target in TARGETS}
        mae_before_target: dict[str, float | None] = {str(int(target)): None for target in TARGETS}
        running_mae = 0.0
        max_favourable = 0.0
        max_adverse = 0.0

        for offset, bar in enumerate(future, start=1):
            favourable, adverse = _favourable_adverse(bar, row.entry_price, direction)
            running_mae = max(running_mae, adverse)
            max_favourable = max(max_favourable, favourable)
            max_adverse = max(max_adverse, adverse)
            for target in TARGETS:
                key = str(int(target))
                if not target_hits[key] and favourable >= target:
                    target_hits[key] = True
                    bars_to_target[key] = offset
                    mae_before_target[key] = running_mae

        first_touch_quality: dict[str, str] = {}
        for stop in QUALITY_STOPS:
            key = str(int(stop))
            result = "timeout"
            for bar in future:
                favourable, adverse = _favourable_adverse(bar, row.entry_price, direction)
                hit_target = favourable >= 10.0
                hit_stop = adverse >= stop
                if hit_target and hit_stop:
                    result = "ambiguous"
                    break
                if hit_target:
                    result = "target_first"
                    break
                if hit_stop:
                    result = "stop_first"
                    break
            first_touch_quality[key] = result

        examples.append(
            {
                "at": row.at,
                "direction": row.direction,
                "atr14": atr14,
                "features": _feature_vector(row, atr14),
                "target_hits": target_hits,
                "bars_to_target": bars_to_target,
                "mae_before_target": mae_before_target,
                "max_favourable_move": max_favourable,
                "max_adverse_move": max_adverse,
                "first_touch_10_vs_stop": first_touch_quality,
            }
        )
    return examples


def _normalisation(vectors: list[list[float]]) -> tuple[list[float], list[float]]:
    means = [sum(v[i] for v in vectors) / len(vectors) for i in range(len(FEATURE_NAMES))]
    scales: list[float] = []
    for i, mean in enumerate(means):
        variance = sum((v[i] - mean) ** 2 for v in vectors) / len(vectors)
        scales.append(max(variance ** 0.5, 1e-9))
    return means, scales


def _norm(vector: list[float], means: list[float], scales: list[float]) -> list[float]:
    return [
        (vector[i] - means[i]) / max(scales[i], 1e-9)
        for i in range(len(FEATURE_NAMES))
    ]


def train_adaptive_reference(
    rows: list[DecisionReplayRow],
    candles: list[Any],
    *,
    horizon_bars: int = 864,
    train_fraction: float = 0.7,
) -> dict[str, Any]:
    examples = build_adaptive_examples(rows, candles, horizon_bars=horizon_bars)
    if len(examples) < 30:
        return {
            "status": "insufficient_data",
            "schema": "AUREON_ADAPTIVE_MULTI_TARGET_V2",
            "samples": len(examples),
            "targets": list(TARGETS),
        }

    split = max(20, min(len(examples) - 8, int(len(examples) * train_fraction)))
    train = examples[:split]
    test = examples[split:]
    raw_train = [item["features"] for item in train]
    means, scales = _normalisation(raw_train)
    norm_train = [_norm(vector, means, scales) for vector in raw_train]

    target_models: dict[str, Any] = {}
    for target in TARGETS:
        key = str(int(target))
        labels = [1 if item["target_hits"][key] else 0 for item in train]
        if not any(labels) or all(labels):
            target_models[key] = {
                "status": "insufficient_classes",
                "positives": sum(labels),
                "samples": len(labels),
            }
            continue

        logistic = fit_logistic(norm_train, labels)
        boosted = fit_boosted_stumps(norm_train, labels)
        test_vectors = [_norm(item["features"], means, scales) for item in test]
        test_labels = [1 if item["target_hits"][key] else 0 for item in test]
        logistic_probs = [logistic.probability(vector) for vector in test_vectors]
        boosted_probs = [boosted.probability(vector) for vector in test_vectors]
        logistic_metrics = binary_metrics(test_labels, logistic_probs)
        boosted_metrics = binary_metrics(test_labels, boosted_probs)
        logistic_brier = float(logistic_metrics.get("brier") or 1.0)
        boosted_brier = float(boosted_metrics.get("brier") or 1.0)
        chosen = "boosted_stumps" if boosted_brier < logistic_brier else "logistic"
        target_models[key] = {
            "status": "trained",
            "chosen_model": chosen,
            "logistic": logistic.to_dict(),
            "boosted_stumps": boosted.to_dict(),
            "validation": {
                "logistic": logistic_metrics,
                "boosted_stumps": boosted_metrics,
            },
            "train_positive_rate": sum(labels) / len(labels),
        }

    quality = {}
    for stop in QUALITY_STOPS:
        key = str(int(stop))
        outcomes = [item["first_touch_10_vs_stop"][key] for item in examples]
        resolved = [value for value in outcomes if value in {"target_first", "stop_first"}]
        quality[key] = {
            "target_first": sum(value == "target_first" for value in resolved),
            "stop_first": sum(value == "stop_first" for value in resolved),
            "ambiguous": sum(value == "ambiguous" for value in outcomes),
            "timeout": sum(value == "timeout" for value in outcomes),
            "target_first_rate": (
                sum(value == "target_first" for value in resolved) / len(resolved)
                if resolved else None
            ),
        }

    winner_mae10 = [
        float(item["mae_before_target"]["10"])
        for item in examples
        if item["mae_before_target"]["10"] is not None
    ]

    return {
        "status": "adaptive_reference_only",
        "schema": "AUREON_ADAPTIVE_MULTI_TARGET_V2",
        "historical_reference_only": True,
        "five_outputs": [5, 10, 20, 30, 40],
        "horizon_bars": horizon_bars,
        "horizon_hours": horizon_bars * 5 / 60,
        "samples": len(examples),
        "train_samples": len(train),
        "test_samples": len(test),
        "train_from": train[0]["at"],
        "train_through": train[-1]["at"],
        "test_from": test[0]["at"],
        "test_through": test[-1]["at"],
        "feature_names": list(FEATURE_NAMES),
        "means": means,
        "scales": scales,
        "target_models": target_models,
        "quality_10_first_touch": quality,
        "winner_mae_before_10": {
            "samples": len(winner_mae10),
            "median": median(winner_mae10) if winner_mae10 else None,
            "max": max(winner_mae10) if winner_mae10 else None,
        },
        "note": (
            "Five target probabilities are historical reference outputs. "
            "The nonlinear challenger is selected per target only when its chronological "
            "validation Brier score beats the logistic baseline."
        ),
    }


def _load_probability(model_block: dict[str, Any], vector: list[float]) -> float | None:
    if model_block.get("status") != "trained":
        return None
    chosen = model_block.get("chosen_model")
    if chosen == "boosted_stumps":
        model = BoostedStumpModel.from_dict(model_block["boosted_stumps"])
        return model.probability(vector)
    model = LogisticModel.from_dict(model_block["logistic"])
    return model.probability(vector)


def test_adaptive_reference(
    rows: list[DecisionReplayRow],
    candles: list[Any],
    artifact: dict[str, Any],
    *,
    threshold: float = 0.50,
) -> dict[str, Any]:
    if artifact.get("status") != "adaptive_reference_only":
        raise ValueError("saved artifact has no completed adaptive_reference_only model")
    if tuple(artifact.get("feature_names") or ()) != FEATURE_NAMES:
        raise ValueError("adaptive model feature contract mismatch")

    horizon_bars = int(artifact.get("horizon_bars") or 864)
    examples = build_adaptive_examples(rows, candles, horizon_bars=horizon_bars)
    means = [float(value) for value in artifact["means"]]
    scales = [float(value) for value in artifact["scales"]]
    models = artifact["target_models"]

    scored: list[dict[str, Any]] = []
    labels_by_target: dict[str, list[int]] = {str(int(t)): [] for t in TARGETS}
    probs_by_target: dict[str, list[float]] = {str(int(t)): [] for t in TARGETS}

    for item in examples:
        vector = _norm(item["features"], means, scales)
        raw: dict[str, float | None] = {}
        previous = 1.0
        for target in TARGETS:
            key = str(int(target))
            probability = _load_probability(models.get(key, {}), vector)
            if probability is not None:
                probability = min(previous, probability)
                previous = probability
                labels_by_target[key].append(1 if item["target_hits"][key] else 0)
                probs_by_target[key].append(probability)
            raw[key] = probability

        recommended_target = 0
        for target in TARGETS:
            probability = raw[str(int(target))]
            if probability is not None and probability >= threshold:
                recommended_target = int(target)

        runner_bias = "EXIT_OR_TIGHTEN"
        if recommended_target >= 40:
            runner_bias = "STRONG_RUNNER"
        elif recommended_target >= 30:
            runner_bias = "HOLD_RUNNER"
        elif recommended_target >= 20:
            runner_bias = "RUNNER_CANDIDATE"
        elif recommended_target >= 10:
            runner_bias = "TARGET_10"
        elif recommended_target >= 5:
            runner_bias = "SECURE_5"

        scored.append(
            {
                "at": item["at"],
                "direction": item["direction"],
                "probabilities": raw,
                "recommended_target": recommended_target,
                "runner_bias": runner_bias,
                "actual_target_hits": item["target_hits"],
                "bars_to_target": item["bars_to_target"],
                "mae_before_target": item["mae_before_target"],
                "atr14": item["atr14"],
                "max_favourable_move": item["max_favourable_move"],
                "max_adverse_move": item["max_adverse_move"],
            }
        )

    metrics = {}
    for target in TARGETS:
        key = str(int(target))
        if labels_by_target[key]:
            metrics[key] = binary_metrics(labels_by_target[key], probs_by_target[key])
        else:
            metrics[key] = None

    selected_5 = [item for item in scored if item["recommended_target"] >= 5]
    selected_10 = [item for item in scored if item["recommended_target"] >= 10]
    runner_candidates = [item for item in scored if item["recommended_target"] >= 20]

    return {
        "status": "tested_adaptive_reference",
        "schema": "AUREON_ADAPTIVE_MULTI_TARGET_V2",
        "historical_reference_only": True,
        "samples": len(examples),
        "threshold": threshold,
        "five_outputs": [5, 10, 20, 30, 40],
        "metrics_by_target": metrics,
        "selected_for_5": len(selected_5),
        "selected_for_10": len(selected_10),
        "runner_candidates_20_plus": len(runner_candidates),
        "scored_rows": scored,
    }
