"""Phase-2 unseen-learning and exit-calibration evidence for Aureon V1.

The acceptance layer is deliberately read-only. It proves that the stored lifecycle
matches the required sequence and combines the frozen-model exam evidence with an exit
comparison produced from the same replay entries.

No policy is auto-tuned here: Phase 2 reports evidence. Changing live exit defaults is a
separate explicit decision after the report passes.
"""

from __future__ import annotations

from typing import Any

from aureon.models.learning_v1 import LearningExamStatus

PHASE2_REPORT_SCHEMA = "AUREON_PHASE2_UNSEEN_EXIT_REPORT_V1"


def _exam(models: Any, exam_id: str | None) -> Any | None:
    return models.get_exam(exam_id) if exam_id else None


def build_phase2_acceptance(
    *,
    storage: Any,
    symbol: str,
    feb_exam_id: str,
    march_exam_id: str,
    challenger_model_id: str,
    exit_summaries: list[dict[str, Any]],
    feb_from: str = "2026-02-01",
    feb_to: str = "2026-02-28",
    march_from: str = "2026-03-01",
    march_to: str = "2026-03-31",
) -> dict[str, Any]:
    symbol = symbol.upper()
    feb = _exam(storage.models, feb_exam_id)
    march = _exam(storage.models, march_exam_id)
    challenger = storage.models.get_model(challenger_model_id)
    champion = storage.models.champion(symbol)

    checks: dict[str, dict[str, Any]] = {}

    def add(name: str, passed: bool, **detail: Any) -> None:
        checks[name] = {"passed": bool(passed), **detail}

    add(
        "february_unseen_scored_and_released",
        feb is not None
        and feb.symbol == symbol
        and feb.period_from == feb_from
        and feb.period_to == feb_to
        and feb.status is LearningExamStatus.RELEASED
        and bool(feb.metrics),
        exam_id=feb_exam_id,
        frozen_model_id=None if feb is None else feb.frozen_model_id,
        status=None if feb is None else feb.status.value,
        metrics={} if feb is None else feb.metrics,
    )
    feb_model = (
        None
        if feb is None or not feb.frozen_model_id
        else storage.models.get_model(feb.frozen_model_id)
    )
    add(
        "january_champion_was_frozen_before_february",
        feb_model is not None and feb_model.trained_through < feb_from,
        model_id=None if feb_model is None else feb_model.model_id,
        trained_through=None if feb_model is None else feb_model.trained_through,
    )
    add(
        "challenger_learned_released_february",
        challenger is not None
        and feb is not None
        and challenger.trained_through >= feb.period_to
        and challenger.model_id != feb.frozen_model_id,
        challenger_model_id=challenger_model_id,
        trained_through=None if challenger is None else challenger.trained_through,
        parent_model_id=None if challenger is None else challenger.parent_model_id,
    )
    add(
        "march_is_blind_exam",
        march is not None
        and march.symbol == symbol
        and march.period_from == march_from
        and march.period_to == march_to
        and march.status in {LearningExamStatus.OPEN, LearningExamStatus.SCORED}
        and march.frozen_model_id == challenger_model_id
        and challenger is not None
        and challenger.trained_through < march_from,
        exam_id=march_exam_id,
        frozen_model_id=None if march is None else march.frozen_model_id,
        status=None if march is None else march.status.value,
    )
    fixed = next(
        (s for s in exit_summaries if str(s.get("label", "")).startswith("fixed_tp_10")),
        None,
    )
    managed = [
        s for s in exit_summaries if not str(s.get("label", "")).startswith("fixed_tp_10")
    ]
    same_samples = bool(fixed) and bool(managed) and all(
        int(s.get("samples", -1)) == int(fixed.get("samples", -2)) for s in managed
    )
    add(
        "same_entry_exit_comparison",
        bool(fixed) and same_samples and int(fixed.get("samples", 0)) > 0,
        fixed=fixed or {},
        managed=managed,
    )
    required_metrics = {
        "net_move", "win_rate", "mfe_captured", "given_back",
        "premature_exits", "reached_20", "reached_30", "reached_40", "max_drawdown",
    }
    add(
        "exit_metrics_complete",
        bool(exit_summaries)
        and all(required_metrics <= set(s) for s in exit_summaries if s.get("samples", 0)),
        required=sorted(required_metrics),
    )

    passed = all(item["passed"] for item in checks.values())
    return {
        "schema": PHASE2_REPORT_SCHEMA,
        "symbol": symbol,
        "passed": passed,
        "checks": checks,
        "february_exam_id": feb_exam_id,
        "march_exam_id": march_exam_id,
        "challenger_model_id": challenger_model_id,
        "current_champion_model_id": None if champion is None else champion.model_id,
        "exit_comparison": exit_summaries,
        "note": (
            "Evidence report only. Exit defaults are not changed automatically; "
            "calibration requires explicit adoption after unseen/shadow evidence."
        ),
    }
