"""Foundation training and the true-unseen-month workflow (V1, GAP 10/11).

## Foundation
historical candles -> existing agents -> Daily Market Bias Agent -> candidate setups ->
frozen canonical features -> future outcomes -> canonical examples -> training memory ->
candidate model -> chronological validation -> walk-forward -> model registry.

Every step reuses what already exists: ``run_decision_replay`` runs the real agent roster
and Director over closed candles, ``canonical_examples_from_replay`` freezes features and
resolves outcomes, ``V1ModelTrainer`` and ``V1WalkForwardBacktester`` train and validate,
``EvolutionAgent`` governs. Dates, symbol and timeframe are parameters, never constants.

## Unseen month
A ``LearningExam`` marks a period the frozen Champion is examined on. While the exam is
OPEN, V1 training refuses every example inside it (``LeakageError``). ``score_exam``
records the frozen model's predictions on the period's setups, then scores them against
the period's outcomes, preserving false positives and false negatives. Only ``release_exam``
lets the period enter Challenger training. The sequence February -> March is therefore
enforced by state, not by discipline.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aureon.ml.logistic import binary_metrics
from aureon.models.base import to_utc, utc_now
from aureon.models.learning_v1 import (
    CanonicalTrainingExample,
    LearningExam,
    LearningExamStatus,
)
from aureon.models.ml import ModelPrediction
from aureon.services.learning_contract import canonical_examples_from_replay
from aureon.services.v1_model_training import (
    V1_TARGETS,
    assert_canonical_examples,
    predict_v1_artifact,
    target_value,
)

log = logging.getLogger(__name__)


class LeakageError(RuntimeError):
    """Raised when data from an unfinished exam period would enter training."""


@dataclass(frozen=True)
class FoundationBuildResult:
    symbol: str
    timeframe: str
    candles: int
    replay_rows: int
    eligible_rows: int
    examples: int
    written: int
    first_market_date: str | None
    last_market_date: str | None
    clean_10: int


def build_canonical_examples(
    *,
    candles: list[Any],
    engine: Any,
    start: datetime,
    end: datetime,
    horizon_bars: int = 864,
    clean_target: float = 10.0,
    clean_max_mae: float = 7.0,
    target_move: float = 10.0,
    on_progress: Callable[[int, int, int], None] | None = None,
    bias_policy: Any | None = None,
) -> tuple[list[CanonicalTrainingExample], dict[str, int]]:
    """Replay candles through the real agents and freeze canonical examples.

    Candles before ``start`` warm the engine; candles after ``end`` only supply outcomes.
    A setup inside the range whose outcome horizon runs past the supplied candles is
    skipped rather than labelled short.
    """
    from aureon.services.decision_backtest import run_decision_replay

    rows = run_decision_replay(
        candles=candles,
        engine=engine,
        target_move=target_move,
        on_progress=on_progress,
        bias_policy=bias_policy,
    )
    in_range = [row for row in rows if start <= to_utc(datetime.fromisoformat(row.at)) < end]
    examples = canonical_examples_from_replay(
        in_range,
        candles,
        horizon_bars=horizon_bars,
        clean_target=clean_target,
        clean_max_mae=clean_max_mae,
    )
    counts = {
        "replay_rows": len(in_range),
        "eligible_rows": sum(1 for row in in_range if row.eligible),
        "examples": len(examples),
        "clean_10": sum(1 for example in examples if example.outcome.clean_10),
    }
    return examples, counts


def bias_evidence_report(examples: list[Any], *, min_samples: int = 30) -> dict[str, Any]:
    """What the frozen daily/session bias actually preceded (item 2 evidence).

    Buckets canonical examples by (direction, session, daily bias), by daily-bias
    alignment and by session transition, with sample counts beside every rate. Nothing
    here tunes anything; it is the table a human reads before touching a threshold.
    """
    from collections import defaultdict

    cells: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))

    def add(group: str, key: str, example: Any) -> None:
        cells[group][key].append(example)

    for example in examples:
        f = example.features
        direction = f.direction.value
        session = f.current_session or f.session or "unknown"
        daily = f.daily_bias_at_entry or "unknown"
        session_bias = f.session_bias_at_entry or "unknown"
        add("direction|session|daily_bias", f"{direction}|{session}|{daily}", example)
        add("direction|session|session_bias", f"{direction}|{session}|{session_bias}", example)
        aligned = f.entry_aligned_with_daily_bias
        if aligned is None and daily in {"strong_bullish", "bullish", "strong_bearish", "bearish"}:
            aligned = (daily.endswith("bullish")) == (direction == "buy")
        add(
            "daily_bias_alignment",
            "aligned" if aligned is True else "opposed" if aligned is False else "undirected",
            example,
        )
        add("daily_bias", daily, example)
        add("session_bias", session_bias, example)
        add("reversal_risk", f.reversal_risk_at_entry or "unknown", example)
        add("trend_quality", f.trend_quality_at_entry or "unknown", example)
        add("session_transition_state", f.session_transition_state or "unknown", example)

    def stats(rows: list[Any]) -> dict[str, Any]:
        n = len(rows)
        clean = sum(1 for r in rows if r.outcome.clean_10)
        return {
            "sample_count": n,
            "clean_10_count": clean,
            "clean_10_rate": round(clean / n, 4) if n else None,
            "reach_20_rate": _rate(rows, lambda r: r.outcome.reached_20),
            "reach_30_rate": _rate(rows, lambda r: r.outcome.reached_30),
            "reach_40_rate": _rate(rows, lambda r: r.outcome.reached_40),
            "average_mfe": _mean(rows, lambda r: r.outcome.max_favourable_move),
            "average_mae": _mean(rows, lambda r: r.outcome.max_adverse_move),
            "small_sample": n < min_samples,
        }

    return {
        "schema": "AUREON_BIAS_EVIDENCE_V1",
        "total": len(examples),
        "min_samples": min_samples,
        "groups": {
            group: {key: stats(rows) for key, rows in sorted(values.items())}
            for group, values in cells.items()
        },
    }


def _rate(rows: list[Any], flag: Callable[[Any], bool]) -> float | None:
    return round(sum(1 for r in rows if flag(r)) / len(rows), 4) if rows else None


def _mean(rows: list[Any], value: Callable[[Any], float]) -> float | None:
    return round(sum(value(r) for r in rows) / len(rows), 3) if rows else None


def render_bias_evidence(report: dict[str, Any]) -> str:
    lines = [f"bias evidence: {report['total']} examples (min_samples {report['min_samples']})"]
    for group, values in report["groups"].items():
        lines.append(f"\n[{group}]")
        lines.append(
            f"{'bucket':<44}{'n':>6}{'clean10':>9}{'r20':>7}{'r30':>7}{'r40':>7}{'mfe':>7}{'mae':>7}"
        )
        for key, s in values.items():
            flag = "*" if s["small_sample"] else " "

            def pct(v: float | None) -> str:
                return "  -  " if v is None else f"{v * 100:4.0f}%"

            lines.append(
                f"{key:<44}{s['sample_count']:>6}{pct(s['clean_10_rate']):>9}"
                f"{pct(s['reach_20_rate']):>7}{pct(s['reach_30_rate']):>7}{pct(s['reach_40_rate']):>7}"
                f"{(s['average_mfe'] or 0):>7.1f}{(s['average_mae'] or 0):>7.1f}{flag}"
            )
    lines.append("* = below min_samples; not evidence")
    return "\n".join(lines)


def persist_examples(training_memory: Any, examples: list[CanonicalTrainingExample]) -> int:
    """Write examples idempotently (the id is a content hash of setup and schemas)."""
    assert_canonical_examples(examples, what="foundation persist")
    written = 0
    for example in examples:
        training_memory.write_canonical(example)
        written += 1
    return written


def exam_id_for(symbol: str, period_from: str, period_to: str) -> str:
    digest = hashlib.sha256(f"{symbol.upper()}|{period_from}|{period_to}".encode())
    return f"exam_{symbol.lower()}_{period_from}_{period_to}_{digest.hexdigest()[:8]}"


def assert_no_exam_leakage(
    models: Any, symbol: str, examples: list[Any], *, what: str = "V1 training"
) -> None:
    """Refuse examples whose market_date lies inside an OPEN or SCORED (unreleased) exam."""
    reader = getattr(models, "unreleased_exams", None)
    if reader is None:
        return
    exams = list(reader(symbol))
    if not exams:
        return
    offenders: dict[str, int] = {}
    for example in examples:
        date = str(getattr(example, "market_date", ""))
        for exam in exams:
            if exam.period_from <= date <= exam.period_to:
                offenders[exam.exam_id] = offenders.get(exam.exam_id, 0) + 1
    if offenders:
        detail = ", ".join(f"{key}: {count} examples" for key, count in sorted(offenders.items()))
        raise LeakageError(
            f"{what} would learn from an unreleased exam period ({detail}); score and "
            "release the exam first"
        )


class UnseenMonthWorkflow:
    """Freeze -> exam -> score -> release, with leakage refused programmatically."""

    def __init__(self, *, models: Any, training_memory: Any, now: Any = utc_now) -> None:
        self.models = models
        self.training_memory = training_memory
        self._now = now

    def frozen_champion(self, symbol: str) -> Any:
        champion = self.models.champion(symbol)
        if champion is None:
            raise LookupError(f"{symbol} has no Champion to freeze")
        return champion

    def open_exam(
        self, symbol: str, *, period_from: str, period_to: str, model_id: str | None = None
    ) -> LearningExam:
        """Declare a held-out period. The frozen model must predate it entirely."""
        if period_to < period_from:
            raise ValueError("period_to must not precede period_from")
        model = self.models.get_model(model_id) if model_id else self.frozen_champion(symbol)
        if model is None:
            raise LookupError(f"no model {model_id}")
        if model.trained_through >= period_from:
            raise LeakageError(
                f"model {model.model_id} was trained through {model.trained_through}, "
                f"which is not before the exam period starting {period_from}"
            )
        for existing in self.models.exams_for(symbol):
            if existing.status is not LearningExamStatus.RELEASED and not (
                period_to < existing.period_from or period_from > existing.period_to
            ):
                raise ValueError(
                    f"exam {existing.exam_id} already covers part of {period_from}..{period_to}"
                )
        exam = LearningExam(
            exam_id=exam_id_for(symbol, period_from, period_to),
            symbol=symbol.upper(),
            period_from=period_from,
            period_to=period_to,
            frozen_model_id=model.model_id,
            status=LearningExamStatus.OPEN,
            created_at=to_utc(self._now()),
        )
        return self.models.write_exam(exam)

    def score_exam(self, exam_id: str, *, clean_threshold: float = 0.55) -> LearningExam:
        """Predict every example in the period with the FROZEN model, then score.

        Predictions are written first (decision recorded), reconciled second, so the
        stored experience is "what the frozen model said before it knew".
        """
        exam = self.models.get_exam(exam_id)
        if exam is None:
            raise LookupError(f"no exam {exam_id}")
        if exam.status is LearningExamStatus.RELEASED:
            raise ValueError(f"exam {exam_id} was already released")
        model = self.models.get_model(exam.frozen_model_id) if exam.frozen_model_id else None
        if model is None:
            raise LookupError(f"exam {exam_id} names no frozen model")
        examples = [
            example
            for example in self.training_memory.canonical_between(
                exam.symbol, exam.period_from, _next_day(exam.period_to)
            )
        ]
        assert_canonical_examples(examples, what=f"exam {exam_id}")
        if not examples:
            raise ValueError(f"exam {exam_id} has no canonical examples in its period yet")

        labels: dict[str, list[int]] = {target: [] for target in V1_TARGETS}
        probabilities: dict[str, list[float]] = {target: [] for target in V1_TARGETS}
        classes = {"true_positive": 0, "false_positive": 0, "false_negative": 0, "true_negative": 0}
        scored_at = to_utc(self._now())
        for example in sorted(examples, key=lambda one: (one.features.timestamp, one.setup_id)):
            probs = predict_v1_artifact(model, example.features)
            clean = probs.get("clean_10")
            decision = "ENTER" if clean is not None and clean >= clean_threshold else "REJECT"
            existing = self.models.prediction_for(model.model_id, example.setup_id)
            if existing is None:
                prediction_id = hashlib.sha256(
                    f"{model.model_id}|{example.setup_id}|{exam.exam_id}".encode()
                ).hexdigest()
                self.models.write_prediction(
                    ModelPrediction(
                        prediction_id=prediction_id,
                        model_id=model.model_id,
                        setup_id=example.setup_id,
                        event_id=exam.exam_id,
                        symbol=example.symbol,
                        timeframe=example.timeframe,
                        predicted_at=example.features.timestamp,
                        feature_schema_version=model.feature_schema_version,
                        label_schema_version=model.label_schema_version,
                        probabilities=probs,
                        feature_snapshot=example.features.model_dump(mode="json"),
                        decision=decision,
                    )
                )
            outcomes = {
                "clean_10": example.outcome.clean_10,
                "reach_5": example.outcome.reached_5,
                "reach_10": example.outcome.reached_10,
                "reach_20": example.outcome.reached_20,
                "reach_30": example.outcome.reached_30,
                "reach_40": example.outcome.reached_40,
                "mae_before_10": example.outcome.mae_before_10,
                "max_favourable_move": example.outcome.max_favourable_move,
                "max_adverse_move": example.outcome.max_adverse_move,
            }
            self.models.reconcile_prediction(
                model.model_id, example.setup_id, outcomes=outcomes, at=scored_at
            )
            actual_clean = bool(example.outcome.clean_10)
            entered = decision == "ENTER"
            key = (
                "true_positive"
                if entered and actual_clean
                else "false_positive"
                if entered
                else "false_negative"
                if actual_clean
                else "true_negative"
            )
            classes[key] += 1
            for target in V1_TARGETS:
                labels[target].append(1 if target_value(example, target) else 0)
                probabilities[target].append(float(probs[target]))

        metrics = {
            target: binary_metrics(labels[target], probabilities[target])
            for target in V1_TARGETS
            if labels[target]
        }
        scored = exam.model_copy(
            update={
                "status": LearningExamStatus.SCORED,
                "scored_at": scored_at,
                "metrics": {
                    "samples": len(examples),
                    "clean_threshold": clean_threshold,
                    "by_target": metrics,
                    "experience": classes,
                },
            }
        )
        return self.models.write_exam(scored)

    def release_exam(self, exam_id: str) -> LearningExam:
        """Let the scored period enter Challenger training. Never before scoring."""
        exam = self.models.get_exam(exam_id)
        if exam is None:
            raise LookupError(f"no exam {exam_id}")
        if exam.status is not LearningExamStatus.SCORED:
            raise ValueError(
                f"exam {exam_id} is {exam.status.value}; only a SCORED exam may be released"
            )
        released = exam.model_copy(
            update={"status": LearningExamStatus.RELEASED, "released_at": to_utc(self._now())}
        )
        return self.models.write_exam(released)


def _next_day(date_text: str) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(date_text) + timedelta(days=1)).isoformat()


__all__ = [
    "FoundationBuildResult",
    "LeakageError",
    "UnseenMonthWorkflow",
    "assert_no_exam_leakage",
    "build_canonical_examples",
    "exam_id_for",
    "persist_examples",
]
