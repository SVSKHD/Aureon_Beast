"""Champion/challenger lifecycle and fail-closed V1 prediction tests."""

from datetime import UTC, datetime

import pytest

from aureon.models.learning_v1 import (
    FEATURE_SCHEMA_V1, LABEL_SCHEMA_V1, MODEL_SCHEMA_V1, ModelLifecycleStatus,
)
from aureon.models.ml import ModelRegistryEntry, TargetMetrics
from aureon.storage.local_database import LocalDatabase
from aureon.storage.postgres.repositories.models import ModelRepository


def _model(model_id: str, status: str) -> ModelRegistryEntry:
    return ModelRegistryEntry(
        model_id=model_id, symbol="XAUUSD", algorithm="logistic_regression_v1",
        status=status, feature_schema_version=FEATURE_SCHEMA_V1,
        label_schema_version=LABEL_SCHEMA_V1, model_schema_version=MODEL_SCHEMA_V1,
        trained_from="2026-01-01", trained_through="2026-02-28", training_samples=50,
        target_metrics={
            "clean_10": TargetMetrics(
                samples=20, positives=12, negatives=8, precision=0.7, recall=0.7,
                brier=0.2, roc_auc=0.7, false_positive_rate=0.25,
            )
        },
        artifact={}, created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )


def test_champion_cannot_be_set_directly_or_skip_shadow(tmp_path) -> None:
    db = LocalDatabase(tmp_path / "aureon.db")
    db.ensure_schema()
    repo = ModelRepository(db)
    candidate = _model("candidate-1", ModelLifecycleStatus.CANDIDATE.value)
    repo.write_model(candidate)
    with pytest.raises(ValueError, match="promote_champion"):
        repo.set_status(candidate.model_id, ModelLifecycleStatus.CHAMPION)
    with pytest.raises(ValueError, match="only shadow"):
        repo.promote_champion(candidate.model_id, at=datetime.now(UTC), reason="bad")


def test_shadow_promotion_retires_previous_champion_atomically(tmp_path) -> None:
    db = LocalDatabase(tmp_path / "aureon.db")
    db.ensure_schema()
    repo = ModelRepository(db)
    old = _model("champion-old", ModelLifecycleStatus.CHAMPION.value)
    shadow = _model("shadow-new", ModelLifecycleStatus.SHADOW.value)
    repo.write_model(old)
    repo.write_model(shadow)
    promoted = repo.promote_champion(
        shadow.model_id, at=datetime.now(UTC), reason="validated shadow"
    )
    assert promoted.status == ModelLifecycleStatus.CHAMPION.value
    assert repo.get_model(old.model_id).status == ModelLifecycleStatus.RETIRED.value
    assert repo.champion("XAUUSD").model_id == shadow.model_id
