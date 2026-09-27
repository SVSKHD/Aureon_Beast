"""Phase-2 acceptance report tests."""

from datetime import UTC, datetime

from aureon.models.learning_v1 import LearningExam, LearningExamStatus
from aureon.services.phase2_acceptance import build_phase2_acceptance
from tests.unit.v1_fixtures import constant_model


def _exit_rows():
    base = {
        "samples": 20, "net_move": 100.0, "win_rate": 0.6, "mfe_captured": 0.5,
        "given_back": 4.0, "premature_exits": 1, "reached_20": 5,
        "reached_30": 3, "reached_40": 1, "max_drawdown": -14.0,
    }
    return [
        {**base, "label": "fixed_tp_10_sl_7"},
        {**base, "label": "exit_manager_act5_lock4_trail0.55", "net_move": 130.0},
    ]


def test_phase2_acceptance_proves_release_learning_and_next_blind_exam(storage) -> None:
    jan = constant_model("jan", trained_through="2026-01-31")
    storage.models.write_model(jan)
    feb = LearningExam(
        exam_id="feb", symbol="XAUUSD", period_from="2026-02-01", period_to="2026-02-28",
        frozen_model_id="jan", status=LearningExamStatus.RELEASED,
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        scored_at=datetime(2026, 3, 1, tzinfo=UTC),
        released_at=datetime(2026, 3, 1, tzinfo=UTC),
        metrics={"samples": 30, "by_target": {"clean_10": {"precision": 0.6}}},
    )
    storage.models.write_exam(feb)
    challenger = constant_model(
        "feb-challenger", status="shadow", trained_through="2026-02-28", generation=1
    ).model_copy(update={"trained_from": "2023-01-01", "parent_model_id": "jan"})
    storage.models.write_model(challenger)
    march = LearningExam(
        exam_id="march", symbol="XAUUSD", period_from="2026-03-01", period_to="2026-03-31",
        frozen_model_id="feb-challenger", status=LearningExamStatus.OPEN,
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    storage.models.write_exam(march)

    report = build_phase2_acceptance(
        storage=storage, symbol="XAUUSD", feb_exam_id="feb", march_exam_id="march",
        challenger_model_id="feb-challenger", exit_summaries=_exit_rows(),
    )
    assert report["passed"] is True
    assert all(check["passed"] for check in report["checks"].values())


def test_phase2_fails_if_march_uses_january_model(storage) -> None:
    jan = constant_model("jan", trained_through="2026-01-31")
    storage.models.write_model(jan)
    feb = LearningExam(
        exam_id="feb", symbol="XAUUSD", period_from="2026-02-01", period_to="2026-02-28",
        frozen_model_id="jan", status=LearningExamStatus.RELEASED,
        created_at=datetime(2026, 2, 1, tzinfo=UTC), metrics={"samples": 20},
    )
    storage.models.write_exam(feb)
    challenger = constant_model(
        "new", status="shadow", trained_through="2026-02-28", generation=1
    ).model_copy(update={"parent_model_id": "jan"})
    storage.models.write_model(challenger)
    storage.models.write_exam(LearningExam(
        exam_id="march", symbol="XAUUSD", period_from="2026-03-01", period_to="2026-03-31",
        frozen_model_id="jan", status=LearningExamStatus.OPEN,
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    ))
    report = build_phase2_acceptance(
        storage=storage, symbol="XAUUSD", feb_exam_id="feb", march_exam_id="march",
        challenger_model_id="new", exit_summaries=_exit_rows(),
    )
    assert report["passed"] is False
    assert report["checks"]["march_is_blind_exam"]["passed"] is False


def test_phase2_requires_same_exit_sample_set(storage) -> None:
    rows = _exit_rows()
    rows[1]["samples"] = 19
    report = build_phase2_acceptance(
        storage=storage, symbol="XAUUSD", feb_exam_id="missing-feb",
        march_exam_id="missing-march", challenger_model_id="missing",
        exit_summaries=rows,
    )
    assert report["checks"]["same_entry_exit_comparison"]["passed"] is False
