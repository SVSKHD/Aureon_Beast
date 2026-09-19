"""Firestore paths (decisions 9, 10, 11)."""

from __future__ import annotations

import pytest

from aureon.storage import paths


def test_there_is_no_pending_orders_collection() -> None:
    """Decision 9: a pending order IS the PENDING trade request.

    A second collection would be a second source of truth about the same order,
    and the two would eventually disagree.
    """
    assert "pending_orders" not in paths.ALL_COLLECTIONS


def test_heartbeats_are_per_service_documents() -> None:
    """Decision 10: heartbeats/{service} is the source of truth."""
    assert paths.heartbeat_path("observer") == "heartbeats/observer"
    assert set(paths.SERVICES) == {"observer", "executor", "monitor", "discord"}


def test_execution_settings_live_at_settings_execution() -> None:
    """Decision 11: the runtime override lives at a fixed, single path."""
    assert paths.execution_settings_path() == "settings/execution"


def test_system_state_is_a_single_document() -> None:
    assert paths.system_state_path() == "system_state/current"


def test_evaluation_path_pairs_detection_with_rule() -> None:
    assert (
        paths.detection_evaluation_path("d1", "EMA_OUTCOME_V1")
        == "detection_evaluations/d1__EMA_OUTCOME_V1"
    )


def test_reviews_are_keyed_so_a_regenerated_period_overwrites() -> None:
    """Phase 7 requires re-running a period to be idempotent."""
    assert paths.daily_review_path("2026-09-18") == "daily_reviews/2026-09-18"
    assert paths.weekly_review_path(2026, 38) == "weekly_reviews/2026-W38"
    assert paths.weekly_review_path(2026, 3) == "weekly_reviews/2026-W03"


@pytest.mark.parametrize("bad", ["", "a/b"])
def test_empty_or_nested_ids_are_rejected(bad: str) -> None:
    """An empty id makes Firestore auto-generate one, silently duplicating a
    document whose whole purpose is to be idempotent."""
    with pytest.raises(ValueError):
        paths.detection_path(bad)


def test_every_collection_is_listed() -> None:
    assert len(paths.ALL_COLLECTIONS) == len(set(paths.ALL_COLLECTIONS))
    assert paths.DETECTIONS in paths.ALL_COLLECTIONS
