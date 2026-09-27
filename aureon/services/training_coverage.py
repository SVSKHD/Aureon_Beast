"""Deterministic training-coverage report and out-of-distribution warning (V1).

Two questions, both answered from counts rather than from a model:

1. **What does the training memory contain?** ``coverage_report`` breaks canonical examples
   down by direction, session, daily bias, session bias, regime, volatility, HTF
   alignment, wick, liquidity and breakout state. Every bucket carries its
   ``sample_count`` beside any rate, and a bucket below ``min_samples`` is flagged
   ``small_sample`` so a 3-of-4 "75%" cannot masquerade as evidence.

2. **How much history looks like *now*?** ``assess_coverage`` counts training examples
   sharing the current context key and reports NORMAL / LOW_TRAINING_COVERAGE /
   OUT_OF_DISTRIBUTION. A categorical value never seen in training is an OOD signal on
   its own. Nothing here is a probability and there is no second model.

The report is computed at training time and stored on the model's
``validation_metrics["training_coverage"]``, so the Champion carries the coverage of the
data that produced it and the prediction service needs no second query.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from aureon.models.learning_v1 import (
    CoverageAssessment,
    CoverageBucket,
    CoverageStatus,
    FeatureSnapshotV1,
)

COVERAGE_SCHEMA = "AUREON_TRAINING_COVERAGE_V1"

#: Dimensions reported, and how each is read off a frozen snapshot.
DIMENSIONS: tuple[str, ...] = (
    "direction",
    "session",
    "daily_bias",
    "session_bias",
    "market_regime",
    "regime_family",
    "volatility_regime",
    "volatility_band",
    "htf_alignment",
    "wick_state",
    "liquidity_state",
    "breakout_state",
    "trend_quality",
    "reversal_risk",
)

#: The joint key used for "how many examples look like this one".
CONTEXT_KEY_DIMENSIONS: tuple[str, ...] = (
    "direction",
    "session",
    "daily_bias",
    "volatility_band",
    "htf_alignment",
)


def _norm(value: Any) -> str:
    text = str(getattr(value, "value", value) or "unknown").strip().lower()
    return text or "unknown"


def regime_family(value: Any) -> str:
    """Collapse the regime agent's states into trend / range / compression / expansion / mixed."""
    text = _norm(value)
    if text in {"trending", "trend_expansion", "trend"}:
        return "trend"
    if text in {"ranging", "range"}:
        return "range"
    if text in {"range_compression", "compressed", "compression"}:
        return "compression"
    if text in {"expanding", "expansion", "expanded"}:
        return "expansion"
    if text in {"volatile_chop", "structurally_messy", "transition", "mixed", "chop", "reversal"}:
        return "mixed_reversal"
    return "unknown"


def volatility_band(snapshot: FeatureSnapshotV1) -> str:
    """low / normal / high from whatever volatility label was frozen."""
    text = _norm(snapshot.volatility_regime)
    if text in {"low", "compressed", "compressing", "quiet"}:
        return "low"
    if text in {"high", "expanded", "expanding", "volatile"}:
        return "high"
    if text in {"normal", "medium"}:
        return "normal"
    state = _norm(snapshot.volatility_state_at_entry)
    if state == "compressing":
        return "low"
    if state == "expanding":
        return "high"
    if state == "normal":
        return "normal"
    return "unknown"


def htf_alignment_of(snapshot: FeatureSnapshotV1) -> str:
    """aligned / opposed / neutral, relative to the setup's own direction."""
    text = _norm(snapshot.htf_alignment)
    if text in {"aligned", "supporting"}:
        return "aligned"
    if text in {"opposed", "opposing"}:
        return "opposed"
    trend = _norm(snapshot.htf_trend)
    if trend in {"bullish", "bearish"}:
        wants = "bullish" if snapshot.direction.value == "buy" else "bearish"
        return "aligned" if trend == wants else "opposed"
    if text in {"bullish", "bearish"}:
        wants = "bullish" if snapshot.direction.value == "buy" else "bearish"
        return "aligned" if text == wants else "opposed"
    if text in {"neutral", "sideways", "mixed"}:
        return "neutral"
    return "unknown"


def dimension_values(snapshot: FeatureSnapshotV1) -> dict[str, str]:
    return {
        "direction": _norm(snapshot.direction),
        "session": _norm(snapshot.current_session or snapshot.session),
        "daily_bias": _norm(snapshot.daily_bias_at_entry),
        "session_bias": _norm(snapshot.session_bias_at_entry),
        "market_regime": _norm(snapshot.market_regime),
        "regime_family": regime_family(snapshot.market_regime),
        "volatility_regime": _norm(snapshot.volatility_regime),
        "volatility_band": volatility_band(snapshot),
        "htf_alignment": htf_alignment_of(snapshot),
        "wick_state": _norm(snapshot.wick_state),
        "liquidity_state": _norm(snapshot.liquidity_state),
        "breakout_state": _norm(snapshot.breakout_state),
        "trend_quality": _norm(snapshot.trend_quality_at_entry),
        "reversal_risk": _norm(snapshot.reversal_risk_at_entry),
    }


def context_key(values: dict[str, str]) -> str:
    return "|".join(f"{name}={values.get(name, 'unknown')}" for name in CONTEXT_KEY_DIMENSIONS)


@dataclass
class _Cell:
    samples: int = 0
    clean: int = 0
    mae_total: float = 0.0
    mfe_total: float = 0.0
    reach: Counter = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.reach = Counter()


def coverage_report(
    examples: Iterable[Any],
    *,
    min_samples: int = 30,
    low_threshold: int = 50,
    ood_threshold: int = 10,
) -> dict[str, Any]:
    """Build the deterministic coverage report as a JSON-safe dict."""
    cells: dict[tuple[str, str], _Cell] = defaultdict(_Cell)
    joint: Counter = Counter()
    seen_values: dict[str, set[str]] = defaultdict(set)
    total = 0
    first_date: str | None = None
    last_date: str | None = None

    for example in examples:
        features = example.features
        outcome = example.outcome
        values = dimension_values(features)
        total += 1
        market_date = getattr(example, "market_date", None)
        if market_date:
            first_date = market_date if first_date is None else min(first_date, market_date)
            last_date = market_date if last_date is None else max(last_date, market_date)
        joint[context_key(values)] += 1
        for dimension, value in values.items():
            seen_values[dimension].add(value)
            cell = cells[(dimension, value)]
            cell.samples += 1
            cell.clean += 1 if outcome.clean_10 else 0
            cell.mae_total += float(outcome.max_adverse_move)
            cell.mfe_total += float(outcome.max_favourable_move)
            for target in (5, 10, 20, 30, 40):
                if getattr(outcome, f"reached_{target}"):
                    cell.reach[target] += 1

    buckets: list[dict[str, Any]] = []
    for (dimension, value), cell in sorted(cells.items()):
        n = cell.samples
        bucket = CoverageBucket(
            dimension=dimension,
            value=value,
            sample_count=n,
            clean_10_count=cell.clean,
            clean_10_rate=(cell.clean / n) if n else None,
            average_mae=(cell.mae_total / n) if n else None,
            average_mfe=(cell.mfe_total / n) if n else None,
            reach_5_rate=(cell.reach[5] / n) if n else None,
            reach_10_rate=(cell.reach[10] / n) if n else None,
            reach_20_rate=(cell.reach[20] / n) if n else None,
            reach_30_rate=(cell.reach[30] / n) if n else None,
            reach_40_rate=(cell.reach[40] / n) if n else None,
            small_sample=n < min_samples,
        )
        buckets.append(bucket.model_dump(mode="json"))

    return {
        "schema": COVERAGE_SCHEMA,
        "total_samples": total,
        "training_from": first_date,
        "training_through": last_date,
        "min_samples": min_samples,
        "low_threshold": low_threshold,
        "ood_threshold": ood_threshold,
        "dimensions": list(DIMENSIONS),
        "context_key_dimensions": list(CONTEXT_KEY_DIMENSIONS),
        "buckets": buckets,
        "context_counts": dict(joint),
        "seen_values": {name: sorted(values) for name, values in seen_values.items()},
    }


def assess_coverage(
    report: dict[str, Any] | None,
    snapshot: FeatureSnapshotV1,
) -> CoverageAssessment:
    """Classify the snapshot's context against a stored coverage report."""
    if not report or report.get("schema") != COVERAGE_SCHEMA:
        return CoverageAssessment(
            status=CoverageStatus.UNKNOWN,
            reason="the Champion carries no training-coverage report",
        )
    values = dimension_values(snapshot)
    key = context_key(values)
    counts = report.get("context_counts") or {}
    similar = int(counts.get(key, 0))
    low_threshold = int(report.get("low_threshold", 50))
    ood_threshold = int(report.get("ood_threshold", 10))
    seen = report.get("seen_values") or {}
    unseen: dict[str, str] = {}
    for dimension in CONTEXT_KEY_DIMENSIONS:
        value = values.get(dimension, "unknown")
        known = seen.get(dimension)
        if known is not None and value not in known and value != "unknown":
            unseen[dimension] = value

    if unseen or similar < ood_threshold:
        status = CoverageStatus.OUT_OF_DISTRIBUTION
        reason = (
            f"{similar} similar training examples (< {ood_threshold})"
            if not unseen
            else "never-seen context values: " + ", ".join(f"{k}={v}" for k, v in unseen.items())
        )
    elif similar < low_threshold:
        status = CoverageStatus.LOW_TRAINING_COVERAGE
        reason = f"{similar} similar training examples (< {low_threshold})"
    else:
        status = CoverageStatus.NORMAL_COVERAGE
        reason = f"{similar} similar training examples"
    return CoverageAssessment(
        status=status,
        similar_samples=similar,
        context_key=key,
        matched_dimensions=tuple(CONTEXT_KEY_DIMENSIONS),
        unseen_values=unseen,
        low_threshold=low_threshold,
        ood_threshold=ood_threshold,
        training_samples=int(report.get("total_samples", 0)),
        reason=reason,
    )


def render_coverage(report: dict[str, Any], *, dimensions: Iterable[str] | None = None) -> str:
    """Plain-text table for the CLI and the health report."""
    wanted = set(dimensions or DIMENSIONS)
    lines = [
        f"training coverage: {report.get('total_samples', 0)} examples "
        f"{report.get('training_from') or '?'}..{report.get('training_through') or '?'}",
        f"{'dimension':<18}{'value':<26}{'n':>6}{'clean10':>9}{'rate':>7}"
        f"{'mae':>7}{'mfe':>7}{'r5':>6}{'r10':>6}{'r20':>6}{'r30':>6}{'r40':>6}",
    ]

    def pct(value: float | None) -> str:
        return "  -  " if value is None else f"{value * 100:4.0f}%"

    for bucket in report.get("buckets", []):
        if bucket["dimension"] not in wanted:
            continue
        flag = "*" if bucket.get("small_sample") else " "
        lines.append(
            f"{bucket['dimension']:<18}{bucket['value']:<26}{bucket['sample_count']:>6}"
            f"{bucket['clean_10_count']:>9}{pct(bucket['clean_10_rate']):>7}"
            f"{(bucket['average_mae'] or 0):>7.1f}{(bucket['average_mfe'] or 0):>7.1f}"
            f"{pct(bucket['reach_5_rate']):>6}{pct(bucket['reach_10_rate']):>6}"
            f"{pct(bucket['reach_20_rate']):>6}{pct(bucket['reach_30_rate']):>6}"
            f"{pct(bucket['reach_40_rate']):>6}{flag}"
        )
    lines.append("* = sample_count below min_samples; the rate is not evidence")
    return "\n".join(lines)
